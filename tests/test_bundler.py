"""Unit tests for the bundler's pure, deterministic logic (no network/LLM).

Run: python -m unittest discover -s tests
"""

import io
import unittest
import zipfile

import bundler
from llm import extract_json


SAMPLE_RAW = {
    "name": "Code Review Swarm",
    "description": "A small code-review system.",
    "system_prompt": "You are a code review swarm.",
    "agents": [
        {"name": "Linter", "role": "lint", "persona": "You lint.", "tools": ["grep"]},
        {"name": "Auditor", "role": "security", "persona": "You audit."},
    ],
    "skills": [
        {"name": "Lint Check", "description": "runs lint", "instructions": "Run the linter.", "example": "lint src/"},
    ],
    "examples": [{"name": "basic", "input": "review this", "expected": "findings"}],
}


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(bundler.slugify("Hello World!"), "hello-world")

    def test_collapses_and_trims(self):
        self.assertEqual(bundler.slugify("  A__B  "), "a-b")

    def test_empty_falls_back(self):
        self.assertEqual(bundler.slugify(""), "bundle")


class NormalizeBlueprintTests(unittest.TestCase):
    def test_empty_gets_defaults(self):
        bp = bundler.normalize_blueprint({}, "do a thing")
        self.assertEqual(bp["name"], "Custom AI Bundle")
        self.assertTrue(bp["agents"])          # always at least one agent
        self.assertIn("claude-code", bp["targets"])
        self.assertEqual(bp["version"], "0.1.0")

    def test_preserves_and_slugifies(self):
        bp = bundler.normalize_blueprint(SAMPLE_RAW, "spec")
        self.assertEqual(bp["slug"], "code-review-swarm")
        self.assertEqual(len(bp["agents"]), 2)
        self.assertEqual(bp["skills"][0]["slug"], "lint-check")

    def test_unknown_targets_dropped(self):
        raw = dict(SAMPLE_RAW, targets=["claude-code", "bogus-target"])
        bp = bundler.normalize_blueprint(raw, "spec")
        self.assertIn("claude-code", bp["targets"])
        self.assertNotIn("bogus-target", bp["targets"])


class GenerateFilesTests(unittest.TestCase):
    def setUp(self):
        self.bp = bundler.normalize_blueprint(
            dict(SAMPLE_RAW, targets=["claude-code", "cursor", "windsurf", "langgraph", "config"]),
            "spec",
        )
        self.files = bundler.generate_files(self.bp)

    def test_core_files_present(self):
        for path in ("README.md", "manifest.json", "bundle.yaml",
                     "prompts/system_prompt.md", "examples/test_cases.json"):
            self.assertIn(path, self.files)

    def test_all_target_dirs_present(self):
        self.assertIn("targets/claude-code/CLAUDE.md", self.files)
        self.assertIn("targets/cursor/.cursor/rules/000-overview.mdc", self.files)
        self.assertIn("targets/windsurf/.windsurfrules", self.files)
        self.assertIn("targets/langgraph/graph.py", self.files)
        self.assertIn("targets/config/agents.json", self.files)

    def test_claude_skill_file_generated(self):
        self.assertIn("targets/claude-code/.claude/skills/lint-check/SKILL.md", self.files)

    def test_langgraph_scaffold_is_valid_python(self):
        compile(self.files["targets/langgraph/graph.py"], "graph.py", "exec")

    def test_zip_is_valid_and_prefixed(self):
        data = bundler.make_zip(self.bp, self.files)
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(zf.testzip())
        self.assertTrue(all(n.startswith("code-review-swarm/") for n in zf.namelist()))
        self.assertEqual(len(zf.namelist()), len(self.files))


class DeliberationPlanTests(unittest.TestCase):
    def test_full_uses_whole_hive(self):
        pods, reserve, orch, prompt = bundler.deliberation_plan("full")
        agents = sum(len(a) for _, a in pods) + 1  # + Phoebe
        self.assertEqual(agents, 20)
        self.assertEqual(len(pods), 5)
        self.assertEqual(orch[0], "Phoebe")

    def test_lite_uses_original_five(self):
        pods, reserve, orch, prompt = bundler.deliberation_plan("lite")
        agents = sum(len(a) for _, a in pods) + 1
        self.assertEqual(agents, 5)
        self.assertEqual(len(pods), 1)
        # Lite reserve must stay within the original core (no specialists pulled in).
        self.assertEqual(set(reserve), set(bundler.CORE_CRITICS))


class ExtractJsonTests(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(extract_json('{"ok": true}'), {"ok": True})

    def test_fenced(self):
        self.assertEqual(extract_json('pre\n```json\n{"a": 1}\n```\npost'), {"a": 1})

    def test_embedded(self):
        self.assertEqual(extract_json('here is {"x": 5} yes'), {"x": 5})

    def test_junk_returns_none(self):
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()
