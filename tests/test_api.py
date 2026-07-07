"""Integration tests that drive the real FastAPI app over HTTP (ASGI).

No network / no LLM: the bundle job is replaced with a fast fake that exercises
the real generation + persistence + endpoints. Run:
    python -m unittest discover -s tests
"""

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("BUNDLE_STORE_DIR", tempfile.mkdtemp())

import httpx

import bundler
import store
import main


async def _fake_run(session_id, sessions):
    """Stand-in for run_bundle: build a real bundle fast, no LLM, no sleeps."""
    s = sessions[session_id]
    bp = bundler.normalize_blueprint(
        {"name": "IT Bundle", "system_prompt": "You are IT.",
         "agents": [{"name": "A", "role": "r"}]},
        s["spec"],
    )
    files = bundler.generate_files(bp)
    zip_bytes = bundler.make_zip(bp, files)
    store.save_bundle(session_id, bp, files, zip_bytes)
    s.update(blueprint=bp, files=files, bundle_zip=zip_bytes, status="completed")
    await s["events"].put(None)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.PHANTOM_INTERNAL_SECRET = ""
        main.RATE_LIMIT_PER_MIN = 60
        main._rate_hits.clear()
        main.sessions.clear()
        self._orig_run = main.run_bundle
        main.run_bundle = _fake_run
        transport = httpx.ASGITransport(app=main.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://t")

    async def asyncTearDown(self):
        main.run_bundle = self._orig_run
        await self.client.aclose()

    async def _wait_completed(self, sid, tries=40):
        for _ in range(tries):
            r = await self.client.get(f"/bundle/status/{sid}")
            if r.json().get("status") == "completed":
                return r.json()
            await asyncio.sleep(0.02)
        return None

    async def test_health(self):
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "alive")
        self.assertIn("crew_size", body)
        self.assertIn("llm_provider", body)

    async def test_targets_and_pricing(self):
        r = await self.client.get("/bundle/targets")
        self.assertIn("claude-code", r.json()["targets"])
        p = await self.client.get("/bundle/pricing")
        self.assertIn("enabled", p.json())

    async def test_create_stream_download_delete(self):
        r = await self.client.post("/bundle/create", json={"spec": "a demo", "mode": "lite"})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["session_id"]
        self.assertEqual(r.json()["mode"], "lite")

        done = await self._wait_completed(sid)
        self.assertIsNotNone(done)
        self.assertGreater(done["file_count"], 0)

        z = await self.client.get(f"/bundle/{sid}/download")
        self.assertEqual(z.status_code, 200)
        self.assertEqual(z.headers["content-type"], "application/zip")
        self.assertTrue(z.content[:2] == b"PK")  # zip magic

        m = await self.client.get(f"/bundle/{sid}/download?format=manifest")
        self.assertIn("run.py", m.json()["files"])

        lst = await self.client.get("/bundle/list")
        self.assertIn(sid, [b["session_id"] for b in lst.json()["bundles"]])

        d = await self.client.delete(f"/bundle/{sid}")
        self.assertEqual(d.status_code, 200)
        gone = await self.client.get("/bundle/list")
        self.assertNotIn(sid, [b["session_id"] for b in gone.json()["bundles"]])

    async def test_spec_required_and_too_long(self):
        r = await self.client.post("/bundle/create", json={})
        self.assertEqual(r.status_code, 400)
        main.MAX_SPEC_CHARS = 50
        r2 = await self.client.post("/bundle/create", json={"spec": "x" * 100})
        self.assertEqual(r2.status_code, 400)
        main.MAX_SPEC_CHARS = 20000

    async def test_auth_required_when_secret_set(self):
        main.PHANTOM_INTERNAL_SECRET = "s3cret"
        r = await self.client.post("/bundle/create", json={"spec": "x"})
        self.assertEqual(r.status_code, 403)
        r2 = await self.client.post("/bundle/create", json={"spec": "x"},
                                    headers={"X-Phantom-Internal": "s3cret"})
        self.assertEqual(r2.status_code, 200)

    async def test_rate_limit(self):
        main.RATE_LIMIT_PER_MIN = 3
        codes = []
        for _ in range(5):
            codes.append((await self.client.post("/bundle/create", json={"spec": "x"})).status_code)
        self.assertIn(429, codes)                 # limiter kicks in
        self.assertEqual(codes[:3], [200, 200, 200])

    async def test_unknown_session_404(self):
        self.assertEqual((await self.client.get("/bundle/status/nope")).status_code, 404)
        self.assertEqual((await self.client.get("/bundle/nope/download")).status_code, 404)
        self.assertEqual((await self.client.delete("/bundle/nope")).status_code, 404)

    async def test_paywall_402_when_enabled(self):
        import crypto_payments
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="WALLET")
        try:
            r = await self.client.post("/bundle/create", json={"spec": "x"})
            self.assertEqual(r.status_code, 402)
            self.assertEqual(r.json()["pricing"]["provider"], "crypto")
        finally:
            os.environ.pop("CRYPTO_PAYMENTS_ENABLED", None)
            os.environ.pop("CRYPTO_PAY_TO", None)


if __name__ == "__main__":
    unittest.main()
