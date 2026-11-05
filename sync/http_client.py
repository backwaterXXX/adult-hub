"""HTTP transport and retry helpers used by the sync process."""

import json
import errno
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional
import logging
from common.http_rate_limit import wait_for_host_slot

logger = logging.getLogger(__name__)


def get_http_client(httpx_module, default_headers: Dict[str, str], current_client):
    """Return the cached client, creating it when httpx is available."""
    if httpx_module is None:
        return None
    if current_client is None or getattr(current_client, "is_closed", False):
        current_client = httpx_module.Client(
            headers=default_headers,
            timeout=httpx_module.Timeout(20.0, connect=10.0),
            limits=httpx_module.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            follow_redirects=True,
        )
    return current_client


def http_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    *,
    get_client: Callable,
    default_headers: Dict[str, str],
) -> bytes:
    client = get_client()
    if client:
        request_headers = dict(default_headers)
        if headers:
            request_headers.update(headers)
        wait_for_host_slot(url)
        response = client.get(url, headers=request_headers, timeout=timeout)
        response.raise_for_status()
        return response.content

    from urllib.request import Request, urlopen

    request = Request(url, headers=headers or default_headers)
    wait_for_host_slot(url)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_retry_after(value: Any, default: float, cap: float = 60.0) -> float:
    """Parse Retry-After delta-seconds or HTTP-date, returning a capped fallback."""
    try:
        raw = str(value).strip()
        try:
            seconds = int(raw)
        except ValueError:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        if seconds < 0:
            return default
        return min(float(seconds), cap)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return default


def retry_after_from_error(
    error: Exception,
    default: float,
    *,
    parse_retry_after_fn: Callable = parse_retry_after,
) -> float:
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        try:
            response = getattr(current, "response", None)
            headers = getattr(response, "headers", None) if response is not None else getattr(current, "headers", None)
            if headers is not None:
                value = headers.get("retry-after")
                if value is None:
                    value = headers.get("Retry-After")
                if value is not None:
                    return parse_retry_after_fn(value, default)
        except Exception:
            logger.debug("Ignored exception in retry_after_from_error", exc_info=True)
        current = current.__cause__ or current.__context__
    return default


def is_retryable_error(error: Exception) -> bool:
    message = str(error)
    if any(token in message for token in (
        "429", "502", "503", "504", "Too Many Requests",
        "Timeout", "ConnectError", "RemoteProtocolError", "ReadError",
    )):
        return True

    # HTTP clients commonly put the useful exception type (ConnectError) in
    # the class name while its message contains only the OS error, e.g.
    # "[Errno 111] Connection refused". Inspect the exception chain so a
    # transient resolver outage is retried instead of failing every source.
    transient_errnos = {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.EAGAIN,
    }
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {
            "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
            "PoolTimeout", "ReadError", "WriteError", "RemoteProtocolError",
        }:
            return True
        if getattr(current, "errno", None) in transient_errnos:
            return True
        current = current.__cause__ or current.__context__
    return False


def fetch_json_with_retry(
    url: str,
    timeout: float = 20.0,
    max_retries: int = 3,
    desc: str = "request",
    *,
    http_get_fn: Callable,
    logger,
    sleep_fn: Callable = time.sleep,
    is_retryable_fn: Callable = is_retryable_error,
    on_error: Optional[Callable] = None,
    retry_after_fn: Callable = retry_after_from_error,
):
    """Fetch JSON and retry transient failures with exponential backoff."""
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            return json.loads(http_get_fn(url, timeout=timeout).decode("utf-8"))
        except Exception as error:
            last = error
            retrying = attempt < max_retries and is_retryable_fn(error)
            backoff = min(3 * (2 ** (attempt - 1)), 30) if retrying else 0
            if retrying:
                backoff = retry_after_fn(error, backoff)
            if on_error:
                try:
                    on_error(error, attempt, retrying, backoff)
                except Exception:
                    logger.debug("Could not record HTTP retry telemetry", exc_info=True)
            if retrying:
                logger.warning(
                    f"{desc}: attempt {attempt}/{max_retries} failed ({error}), retrying in {backoff}s..."
                )
                sleep_fn(backoff)
                continue
            raise
    assert last is not None
    raise last
