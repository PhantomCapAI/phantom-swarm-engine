"""Config precedence + persistence tests (no network)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phantom_bundler import config as cfgmod


class ConfigPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # Redirect the config path into a temp dir for the duration of the test.
        self._patchers = [
            mock.patch.object(cfgmod, "CONFIG_DIR", self.dir),
            mock.patch.object(cfgmod, "CONFIG_PATH", self.dir / "config.toml"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def test_default_remote_when_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfgmod.Config().remote, cfgmod.DEFAULT_REMOTE)

    def test_file_value_used(self):
        cfgmod.save_file({"remote": "https://hosted.example.com/"})
        with mock.patch.dict(os.environ, {}, clear=True):
            # Trailing slash is stripped by the accessor.
            self.assertEqual(cfgmod.Config().remote, "https://hosted.example.com")

    def test_env_overrides_file(self):
        cfgmod.save_file({"remote": "https://file.example.com"})
        with mock.patch.dict(os.environ, {"PHANTOM_BUNDLER_REMOTE": "https://env.example.com"}, clear=True):
            self.assertEqual(cfgmod.Config().remote, "https://env.example.com")

    def test_agents_coerced_to_int(self):
        cfgmod.save_file({"agents": 12})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfgmod.Config().agents, 12)

    def test_targets_list(self):
        cfgmod.save_file({"targets": ["claude-code", "cursor"]})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfgmod.Config().targets, ["claude-code", "cursor"])

    def test_secret_masked_in_view(self):
        cfgmod.save_file({"internal_secret": "topsecret"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfgmod.Config().as_dict()["internal_secret"], "***")

    def test_corrupt_file_is_ignored(self):
        (self.dir / "config.toml").write_text("this is = = not toml ][")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfgmod.Config().remote, cfgmod.DEFAULT_REMOTE)

    def test_roundtrip_save_load(self):
        cfgmod.save_file({"remote": "https://x", "mode": "lite"})
        loaded = cfgmod.load_file()
        self.assertEqual(loaded["mode"], "lite")


if __name__ == "__main__":
    unittest.main()
