"""Unit tests for the bundler's pure, deterministic logic (no network/LLM).

Run: python -m unittest discover -s tests
"""

import io
import json
import unittest
import zipfile

import agents
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

    def test_runnable_runtime_present_and_valid(self):
        # Every bundle ships a runnable app: run.py + agents.json at the root.
        self.assertIn("run.py", self.files)
        self.assertIn("agents.json", self.files)
        compile(self.files["run.py"], "run.py", "exec")  # run.py is valid Python
        cfg = json.loads(self.files["agents.json"])
        self.assertEqual(cfg["name"], self.bp["name"])
        self.assertTrue(cfg["agents"])
        self.assertIn("system_prompt", cfg)
        self.assertIn("default_model", cfg)

    def test_zip_is_valid_and_prefixed(self):
        data = bundler.make_zip(self.bp, self.files)
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(zf.testzip())
        self.assertTrue(all(n.startswith("code-review-swarm/") for n in zf.namelist()))
        self.assertEqual(len(zf.namelist()), len(self.files))


class RosterTests(unittest.TestCase):
    def test_full_default_is_whole_crew(self):
        pods, reserve, orch, prompt, total = agents.roster("full")
        self.assertEqual(total, agents.CREW_SIZE)   # 20
        self.assertEqual(orch[0], "Orchestrator")
        # Every critic maps to a known crew member with a single focus.
        for _, names in pods:
            for n in names:
                self.assertIn(n, agents.CREW_MAP)

    def test_full_custom_size_is_choosable(self):
        _, _, _, _, total = agents.roster("full", size=8)
        self.assertEqual(total, 8)

    def test_full_size_is_clamped(self):
        self.assertEqual(agents.roster("full", size=1)[4], 5)                 # floor
        self.assertEqual(agents.roster("full", size=999)[4], agents.CREW_SIZE)  # ceil

    def test_lite_is_fixed_small_set(self):
        pods, reserve, orch, prompt, total = agents.roster("lite")
        self.assertEqual(total, len(agents.LITE_CRITICS) + 1)
        self.assertEqual(set(reserve), set(agents.LITE_CRITICS))

    def test_single_focus_per_agent(self):
        # Orderly rebuild: no two crew agents share a name; each has one focus.
        names = [a["name"] for a in agents.CREW]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(a["focus"] for a in agents.CREW))


class StoreTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        import importlib
        os.environ["BUNDLE_STORE_DIR"] = tempfile.mkdtemp()
        import store
        importlib.reload(store)
        self.store = store
        self.bp = bundler.normalize_blueprint(SAMPLE_RAW, "spec")
        self.files = bundler.generate_files(self.bp)

    def test_save_load_delete_roundtrip(self):
        self.store.save_bundle("sid1", self.bp, self.files, bundler.make_zip(self.bp, self.files))
        self.assertIsNotNone(self.store.load_zip("sid1"))
        self.assertEqual([b["session_id"] for b in self.store.list_bundles()], ["sid1"])
        # delete removes zip + meta and drops it from the listing
        self.assertTrue(self.store.delete_bundle("sid1"))
        self.assertIsNone(self.store.load_zip("sid1"))
        self.assertIsNone(self.store.load_meta("sid1"))
        self.assertEqual(self.store.list_bundles(), [])

    def test_delete_missing_is_false(self):
        self.assertFalse(self.store.delete_bundle("nope"))


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
