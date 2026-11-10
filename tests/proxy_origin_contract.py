"""Run in the resolver image to verify its actual Uvicorn proxy middleware."""
import asyncio
import unittest


from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
import resolver.admin_security as admin_security


class TestProxyOriginContract(unittest.TestCase):
    def authorized(self, peer, origin, forwarded_host="adult.example.com"):
        async def run():
            result = []
            async def app(scope, receive, send):
                result.append(admin_security.write_authorized(
                    Request(scope), blocked=False, public=True, token=""))
            middleware = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1,172.29.0.1")
            scope = {"type": "http", "http_version": "1.1", "method": "POST",
                     "scheme": "http", "path": "/admin/api/cache/clear", "raw_path": b"/admin/api/cache/clear",
                     "query_string": b"", "root_path": "", "client": (peer, 12345),
                     "server": ("172.29.0.2", 8090),
                     "headers": [(b"host", b"adult.example.com"), (b"origin", origin.encode()),
                                 (b"x-forwarded-proto", b"https"),
                                 (b"x-forwarded-host", forwarded_host.encode())]}
            await middleware(scope, None, None)
            return result[0]
        return asyncio.run(run())

    def test_trusted_proxy_preserves_https_origin(self):
        self.assertTrue(self.authorized("172.29.0.1", "https://adult.example.com"))

    def test_untrusted_peer_cannot_spoof_https_scheme(self):
        self.assertFalse(self.authorized("172.29.0.3", "https://adult.example.com"))

    def test_trusted_proxy_still_requires_exact_origin(self):
        for origin in ("https://attacker.example", "https://adult.example.com:8090", "http://adult.example.com"):
            with self.subTest(origin=origin):
                self.assertFalse(self.authorized("172.29.0.1", origin, "attacker.example"))


if __name__ == "__main__":
    unittest.main()
