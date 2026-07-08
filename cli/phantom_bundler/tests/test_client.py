"""Client tests using httpx's MockTransport (no real network)."""

import json
import unittest
from unittest import mock

import httpx

from phantom_bundler.client import BundlerClient
from phantom_bundler.errors import NotFound, PaymentRequired, RemoteUnreachable


def _handler(routes):
    """Build a MockTransport that dispatches on (method, path)."""

    def handle(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key in routes:
            return routes[key](request)
        return httpx.Response(404, json={"error": "no route"})

    return httpx.MockTransport(handle)


class ClientTests(unittest.TestCase):
    def _client(self, routes, **kw):
        """Patch httpx module functions to use a mock transport."""
        transport = _handler(routes)

        def fake_get(url, **kwargs):
            with httpx.Client(transport=transport) as c:
                return c.get(url, **{k: v for k, v in kwargs.items() if k != "timeout"})

        def fake_post(url, **kwargs):
            with httpx.Client(transport=transport) as c:
                return c.post(url, **{k: v for k, v in kwargs.items() if k != "timeout"})

        self._get = mock.patch("phantom_bundler.client.httpx.get", side_effect=fake_get)
        self._post = mock.patch("phantom_bundler.client.httpx.post", side_effect=fake_post)
        self._get.start()
        self._post.start()
        self.addCleanup(self._get.stop)
        self.addCleanup(self._post.stop)
        return BundlerClient("http://localhost:8500", **kw)

    def test_health(self):
        client = self._client({
            ("GET", "/health"): lambda r: httpx.Response(200, json={"status": "alive", "engine": "phantom-swarm"}),
        })
        self.assertEqual(client.health()["status"], "alive")

    def test_create_sends_payload(self):
        seen = {}

        def create_route(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"session_id": "abc123", "status": "started"})

        client = self._client({("POST", "/bundle/create"): create_route})
        resp = client.create("a spec", mode="lite", agents=7, targets=["cursor"])
        self.assertEqual(resp["session_id"], "abc123")
        self.assertEqual(seen["mode"], "lite")
        self.assertEqual(seen["agents"], 7)
        self.assertEqual(seen["targets"], ["cursor"])

    def test_create_payment_required(self):
        client = self._client({
            ("POST", "/bundle/create"): lambda r: httpx.Response(
                402, json={"error": "payment required", "pricing": {"pay_to": "WALLET", "options": [{"asset": "SOL", "price": "0.5"}]}}
            ),
        })
        with self.assertRaises(PaymentRequired) as cm:
            client.create("x")
        self.assertIn("WALLET", cm.exception.hint)

    def test_auth_headers_forwarded(self):
        seen_headers = {}

        def route(request):
            seen_headers.update(request.headers)
            return httpx.Response(200, json={"bundles": []})

        client = self._client({("GET", "/bundle/list"): route},
                              payment_tx="SIG", internal_secret="SECRET")
        client.list_bundles()
        self.assertEqual(seen_headers["x-payment-tx"], "SIG")
        self.assertEqual(seen_headers["x-phantom-internal"], "SECRET")

    def test_status_not_found(self):
        client = self._client({
            ("GET", "/bundle/status/missing"): lambda r: httpx.Response(404, json={"error": "bundle session not found"}),
        })
        with self.assertRaises(NotFound):
            client.status("missing")

    def test_download_returns_bytes_and_name(self):
        client = self._client({
            ("GET", "/bundle/xyz/download"): lambda r: httpx.Response(
                200, content=b"PK\x03\x04zip",
                headers={"content-disposition": 'attachment; filename="my-slug.zip"'},
            ),
        })
        data, name = client.download("xyz")
        self.assertEqual(data, b"PK\x03\x04zip")
        self.assertEqual(name, "my-slug.zip")

    def test_connect_error_becomes_remote_unreachable(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        client = self._client({("GET", "/health"): boom})
        with self.assertRaises(RemoteUnreachable):
            client.health()


if __name__ == "__main__":
    unittest.main()
