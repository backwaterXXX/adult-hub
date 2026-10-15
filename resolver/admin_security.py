"""Shared access policy for management routes, separate from playback routes."""
import hmac
from urllib.parse import urlsplit


def hostname(value: str) -> str:
    """Parse a host or URL exactly; never classify domains by substring."""
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        return (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def via_public_domain(request, public_domain: str) -> bool:
    domain = hostname(public_domain)
    if not domain:
        return False
    # Treat forwarded host only as an additional denial signal. It can never
    # override Host or grant access, even when supplied by an untrusted client.
    values = [request.headers.get("host", "")]
    values.extend(request.headers.get("x-forwarded-host", "").split(","))
    return any(hostname(value.strip()) == domain for value in values)


def capabilities(*, enabled: bool, public: bool, token: str) -> dict:
    reason = "admin_disabled" if not enabled else None
    return {"actions_enabled": reason is None, "read_only": reason is not None,
            "read_only_reason": reason, "token_required": bool(token)}


def write_authorized(request, *, blocked: bool, public: bool, token: str) -> bool:
    if blocked:
        return False
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        # Request.url reflects the application's proxy trust configuration.
        # Raw forwarded headers are never used to grant write access.
        request_url = getattr(request, "url", None)
        target = urlsplit(str(request_url)) if request_url else urlsplit("http://" + request.headers.get("host", ""))
        if origin.scheme not in {"http", "https"} or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
            return False
        def authority(parsed):
            return ((parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80))
        if not origin.hostname or origin.scheme != target.scheme or authority(origin) != authority(target):
            return False
        if not token:
            return True
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(supplied.encode(), token.encode())
    except (ValueError, TypeError, AttributeError):
        return False
