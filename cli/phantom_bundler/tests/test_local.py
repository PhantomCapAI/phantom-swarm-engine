"""Local helpers: run_bundle_dir + detect_local_engine (no real network)."""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import httpx

from phantom_bundler.errors import PhantomError
from phantom_bundler.local import detect_local_engine, run_bundle_dir


class RunBundleDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write_run_py(self, at: Path):
        at.mkdir(parents=True, exist_ok=True)
        (at / "run.py").write_text(textwrap.dedent("""
            import sys
            print("TASK:" + " ".join(sys.argv[1:]))
        """))

    def test_runs_root_run_py(self):
        self._write_run_py(self.dir)
        code = run_bundle_dir(self.dir, "hello world")
        self.assertEqual(code, 0)

    def test_finds_run_py_in_single_subdir(self):
        self._write_run_py(self.dir / "my-slug")
        code = run_bundle_dir(self.dir, "task")
        self.assertEqual(code, 0)

    def test_missing_dir_raises(self):
        with self.assertRaises(PhantomError):
            run_bundle_dir(self.dir / "nope", "task")

    def test_no_run_py_raises(self):
        (self.dir / "README.md").write_text("not a bundle")
        with self.assertRaises(PhantomError):
            run_bundle_dir(self.dir, "task")


class DetectEngineTests(unittest.TestCase):
    def test_detects_phantom_engine(self):
        def handler(request):
            return httpx.Response(200, json={"engine": "phantom-swarm", "crew_size": 20})

        transport = httpx.MockTransport(handler)

        def fake_get(url, **kwargs):
            with httpx.Client(transport=transport) as c:
                return c.get(url)

        with mock.patch("phantom_bundler.local.httpx.get", side_effect=fake_get):
            self.assertEqual(detect_local_engine(["http://localhost:8500"]), "http://localhost:8500")

    def test_ignores_non_phantom_server(self):
        def handler(request):
            return httpx.Response(200, json={"app": "something else"})

        transport = httpx.MockTransport(handler)

        def fake_get(url, **kwargs):
            with httpx.Client(transport=transport) as c:
                return c.get(url)

        with mock.patch("phantom_bundler.local.httpx.get", side_effect=fake_get):
            self.assertIsNone(detect_local_engine(["http://localhost:9999"]))

    def test_none_when_unreachable(self):
        def fake_get(url, **kwargs):
            raise httpx.ConnectError("refused")

        with mock.patch("phantom_bundler.local.httpx.get", side_effect=fake_get):
            self.assertIsNone(detect_local_engine(["http://localhost:1"]))


if __name__ == "__main__":
    unittest.main()
