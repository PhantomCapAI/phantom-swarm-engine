"""Tests for the pump.fun / Solana domain pack (pure, no network/LLM).

Run: python -m unittest discover -s tests
"""

import json
import unittest

import bundler
import pumpfun


class DetectionTests(unittest.TestCase):
    def test_matches_launch_specs(self):
        for spec in [
            "Launch a token on pump.fun with a dev buy",
            "A memecoin sniper agent using PumpPortal",
            "bundle buy across wallets on the bonding curve",
        ]:
            self.assertTrue(pumpfun.matches(spec), spec)

    def test_ignores_unrelated_specs(self):
        for spec in ["A code review swarm", "A tweet-writing assistant", "A recipe generator"]:
            self.assertFalse(pumpfun.matches(spec), spec)

    def test_matches_via_blueprint_fields(self):
        bp = {"name": "PumpPortal Launcher", "description": "launches coins", "tagline": ""}
        self.assertTrue(pumpfun.matches("do a thing", bp))


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.spec = "An autonomous pump.fun token launcher agent."
        self.bp = bundler.normalize_blueprint(
            {"name": "Launcher", "system_prompt": "You launch tokens.",
             "agents": [{"name": "L", "role": "exec", "persona": "x"}],
             "targets": ["claude-code", "config"]},
            self.spec,
        )

    def test_adds_skills_examples_target(self):
        pumpfun.enrich_blueprint(self.bp, self.spec)
        slugs = {s["slug"] for s in self.bp["skills"]}
        for expected in ("launch-token", "upload-metadata", "wallet-signing",
                         "launch-strategy", "post-launch-monitor", "risk-controls"):
            self.assertIn(expected, slugs)
        self.assertIn("solana-launch", self.bp["targets"])
        self.assertEqual(self.bp["domain"], "pumpfun")
        self.assertTrue(any(e["name"] == "launch-a-token" for e in self.bp["examples"]))

    def test_enrichment_is_idempotent(self):
        pumpfun.enrich_blueprint(self.bp, self.spec)
        n_skills = len(self.bp["skills"])
        n_targets = len(self.bp["targets"])
        pumpfun.enrich_blueprint(self.bp, self.spec)  # again
        self.assertEqual(len(self.bp["skills"]), n_skills)
        self.assertEqual(len(self.bp["targets"]), n_targets)


class SolanaTargetTests(unittest.TestCase):
    def setUp(self):
        self.bp = bundler.normalize_blueprint(
            {"name": "Launcher", "system_prompt": "sp", "targets": ["solana-launch"]},
            "pump.fun launcher",
        )
        self.files = bundler.generate_files(self.bp)

    def test_all_tool_modules_present_and_compile(self):
        expected = [
            "risk_controls.py", "wallet.py", "ipfs_metadata.py", "pumpportal_client.py",
            "launch_strategy.py", "monitor.py", "example_launch.py",
        ]
        for name in expected:
            path = f"targets/solana-launch/{name}"
            self.assertIn(path, self.files)
            compile(self.files[path], path, "exec")  # valid Python

    def test_tools_json_is_valid_and_maps_to_modules(self):
        data = json.loads(self.files["targets/solana-launch/tools.json"])
        names = {t["name"] for t in data["tools"]}
        self.assertIn("create_token", names)
        self.assertIn("buy", names)
        self.assertIn("preflight", names)
        for t in data["tools"]:  # each tool names a module + function
            self.assertIn("module", t)
            self.assertIn("function", t)

    def test_readme_has_safety_warning(self):
        readme = self.files["targets/solana-launch/README.md"]
        self.assertIn("DRY_RUN", readme)
        self.assertIn("MAX_SPEND_SOL", readme)


class RiskControlsBehaviourTests(unittest.TestCase):
    """Exercise the generated risk_controls module by importing its source."""

    def _load(self):
        bp = bundler.normalize_blueprint({"name": "L", "targets": ["solana-launch"]}, "pump.fun")
        files = bundler.generate_files(bp)
        ns: dict = {}
        exec(files["targets/solana-launch/risk_controls.py"], ns)
        return ns

    def test_over_cap_is_refused(self):
        ns = self._load()
        ns["MAX_SPEND_SOL"] = 1.0
        with self.assertRaises(ns["RiskError"]):
            ns["preflight"]("buy", 5.0)

    def test_within_cap_passes(self):
        ns = self._load()
        ns["MAX_SPEND_SOL"] = 10.0
        ns["DAILY_SPEND_SOL"] = 100.0
        ns["preflight"]("buy", 0.5)  # should not raise


if __name__ == "__main__":
    unittest.main()
