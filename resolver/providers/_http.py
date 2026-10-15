"""Shared async HTTP client for all providers (connection pooling).

Previously every provider method opened a new ``httpx.AsyncClient`` per
request (``async with ...``), which meant a fresh TCP+TLS handshake for
every browse/category/resolve call. This module keeps one process-wide
client with keep-alive so consecutive sync/resolver calls reuse
connections.
"""

import httpx

_shared_client: httpx.AsyncClient | None = None


async def get_shared_client() -> httpx.AsyncClient:
    """Return the process-wide shared AsyncClient (lazy init)."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            follow_redirects=True,
        )
    return _shared_client


async def aclose_shared_client() -> None:
    """Close the shared client (used on FastAPI shutdown)."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
