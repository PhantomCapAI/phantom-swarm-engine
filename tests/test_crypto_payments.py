"""Unit tests for the (opt-in) crypto payment gate. No network required.

Run: python -m unittest discover -s tests
"""

import os
import unittest

import crypto_payments as cp


class Headers(dict):
    """Case-insensitive header stand-in with a .get, like Starlette's."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _clear_env():
    for k in list(os.environ):
        if k.startswith("CRYPTO_") or k in ("PHANTOM_INTERNAL_SECRET", "USDC_MINT"):
            del os.environ[k]


class EnabledTests(unittest.TestCase):
    def setUp(self):
        _clear_env()
        self.addCleanup(_clear_env)

    def test_disabled_by_default(self):
        self.assertFalse(cp.enabled())

    def test_needs_flag_and_wallet(self):
        os.environ["CRYPTO_PAYMENTS_ENABLED"] = "1"
        self.assertFalse(cp.enabled())            # no wallet yet
        os.environ["CRYPTO_PAY_TO"] = "WALLET"
        self.assertTrue(cp.enabled())


class AcceptedOptionsTests(unittest.TestCase):
    def setUp(self):
        _clear_env()
        self.addCleanup(_clear_env)

    def test_multi_asset_parse(self):
        os.environ["CRYPTO_ACCEPT"] = "SOL:0.5,USDC:75"
        opts = cp._accepted_options()
        by = {o["asset"]: o for o in opts}
        self.assertEqual(by["SOL"]["kind"], "native")
        self.assertEqual(cp._required(by["SOL"]), 500_000_000)   # 0.5 * 1e9
        self.assertEqual(by["USDC"]["kind"], "spl")
        self.assertEqual(cp._required(by["USDC"]), 75_000_000)   # 75 * 1e6

    def test_legacy_single_asset_fallback(self):
        os.environ["CRYPTO_ASSET"] = "SOL"
        os.environ["CRYPTO_PRICE"] = "1"
        opts = cp._accepted_options()
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["asset"], "SOL")
        self.assertEqual(cp._required(opts[0]), 1_000_000_000)


class OnChainCheckTests(unittest.TestCase):
    def test_sol_exact_and_underpay(self):
        result = {
            "meta": {"err": None,
                     "preBalances": [10_000_000_000, 1_000_000_000],
                     "postBalances": [9_499_995_000, 1_500_000_000]},
            "transaction": {"message": {"accountKeys": [
                {"pubkey": "SENDER"}, {"pubkey": "WALLET"}]}},
        }
        ok, _ = cp._check_sol(result, "WALLET", 500_000_000)
        self.assertTrue(ok)
        ok, reason = cp._check_sol(result, "WALLET", 600_000_000)
        self.assertFalse(ok)
        self.assertIn("insufficient", reason)

    def test_sol_recipient_absent(self):
        result = {"meta": {"preBalances": [1], "postBalances": [1]},
                  "transaction": {"message": {"accountKeys": [{"pubkey": "OTHER"}]}}}
        ok, reason = cp._check_sol(result, "WALLET", 1)
        self.assertFalse(ok)
        self.assertIn("not in transaction", reason)

    def test_spl_transfer(self):
        result = {"meta": {"err": None,
                  "preTokenBalances": [{"owner": "WALLET", "mint": "MINT",
                                        "uiTokenAmount": {"amount": "0"}}],
                  "postTokenBalances": [{"owner": "WALLET", "mint": "MINT",
                                         "uiTokenAmount": {"amount": "75000000"}}]}}
        ok, _ = cp._check_spl(result, "WALLET", "MINT", 75_000_000)
        self.assertTrue(ok)
        ok, _ = cp._check_spl(result, "WALLET", "MINT", 80_000_000)
        self.assertFalse(ok)


class PricingTests(unittest.TestCase):
    def setUp(self):
        _clear_env()
        self.addCleanup(_clear_env)

    def test_pricing_lists_options(self):
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="W", CRYPTO_ACCEPT="SOL:0.5,USDC:75")
        p = cp.pricing()
        self.assertTrue(p["enabled"])
        self.assertEqual(p["provider"], "crypto")
        self.assertEqual([o["asset"] for o in p["options"]], ["SOL", "USDC"])
        self.assertEqual(p["pay_to"], "W")


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_env()
        self.addCleanup(_clear_env)

    async def test_disabled_allows(self):
        self.assertTrue((await cp.check(Headers()))["ok"])

    async def test_admin_bypass(self):
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="W", PHANTOM_INTERNAL_SECRET="s")
        r = await cp.check(Headers({"X-Phantom-Internal": "s"}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["via"], "internal")

    async def test_no_payment_blocked(self):
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="W")
        r = await cp.check(Headers())
        self.assertFalse(r["ok"])

    async def test_dev_accept_and_replay(self):
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="W",
                          CRYPTO_PAYMENTS_DEV_ACCEPT_TOKEN="devsig")
        r = await cp.check(Headers({"X-Payment-Tx": "devsig"}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["via"], "dev-accept")

    async def test_verified_signature_is_single_use(self):
        os.environ.update(CRYPTO_PAYMENTS_ENABLED="1", CRYPTO_PAY_TO="W")

        async def fake_verify(sig):
            if sig in cp._consumed:
                return False, "signature already used", None
            return True, "verified", "SOL"

        cp.verify = fake_verify
        self.addCleanup(cp._consumed.clear)
        first = await cp.check(Headers({"X-Payment-Tx": "sig1"}))
        second = await cp.check(Headers({"X-Payment-Tx": "sig1"}))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])


if __name__ == "__main__":
    unittest.main()
