"""Unit tests for the dependency-free .env loader."""

import os
import tempfile
import unittest

import envload


class LoadDotenvTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        d = tempfile.mkdtemp()
        path = os.path.join(d, ".env")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_missing_file_is_noop(self):
        envload.load_dotenv("/nonexistent/.env")  # must not raise

    def test_parses_and_strips_quotes(self):
        path = self._write('FOO_A=bar\nFOO_B="quoted"\n# comment\n\nFOO_C=3\n')
        for k in ("FOO_A", "FOO_B", "FOO_C"):
            os.environ.pop(k, None)
        envload.load_dotenv(path)
        self.assertEqual(os.environ["FOO_A"], "bar")
        self.assertEqual(os.environ["FOO_B"], "quoted")
        self.assertEqual(os.environ["FOO_C"], "3")

    def test_does_not_override_real_env(self):
        path = self._write("FOO_D=from_file\n")
        os.environ["FOO_D"] = "from_env"
        envload.load_dotenv(path)
        self.assertEqual(os.environ["FOO_D"], "from_env")

    def test_ignores_malformed_lines(self):
        path = self._write("no_equals_here\nFOO_E=ok\n")
        os.environ.pop("FOO_E", None)
        envload.load_dotenv(path)
        self.assertEqual(os.environ["FOO_E"], "ok")


if __name__ == "__main__":
    unittest.main()
