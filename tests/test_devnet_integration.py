"""Opt-in integration tests against Solana **devnet**.

These make real network calls, so they are skipped unless ``RUN_DEVNET_TESTS=1``
is set — CI and the default ``python -m unittest`` run stay fully offline. They
exercise the *real* RPC round-trip and the ``crypto_payments`` verification path
that unit tests can only mock.

Run:

    RUN_DEVNET_TESTS=1 \
    SOLANA_RPC_URL=https://api.devnet.solana.com \
    python -m unittest tests.test_devnet_integration -v

To also test full payment verification end-to-end, make a real transfer on
devnet to a wallet you control and provide:

    CRYPTO_PAY_TO=<devnet wallet that received the transfer>
    DEVNET_TEST_SIGNATURE=<the transfer's signature>
    CRYPTO_ACCEPT=SOL:0.0001            # a price the transfer meets
    DEVNET_TEST_EXPECT_OK=1             # assert verification succeeds

Without those, the payment test still runs against the RPC but only asserts the
verification path returns a well-formed result (no unhandled errors).
"""

import os
import unittest

import crypto_payments as cp

_RUN = os.environ.get("RUN_DEVNET_TESTS", "").lower() in ("1", "true", "yes")
_DEFAULT_DEVNET_RPC = "https://api.devnet.solana.com"

# A syntactically valid base58 signature (88 chars) that will not exist on
# devnet — used to exercise the "not found" branch against a live RPC.
_NONEXISTENT_SIG = "5" * 88


def _ensure_devnet_rpc() -> None:
    # Default to the public devnet endpoint if the operator didn't pin one.
    os.environ.setdefault("SOLANA_RPC_URL", _DEFAULT_DEVNET_RPC)


@unittest.skipUnless(_RUN, "set RUN_DEVNET_TESTS=1 to run live devnet integration tests")
class DevnetRpcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ensure_devnet_rpc()

    async def test_fetch_nonexistent_tx_is_handled(self):
        """A valid-format but nonexistent signature: real RPC round-trip, no crash.

        The RPC returns a null result (not an error), so _fetch_tx should yield
        ``(None, None)`` — i.e. "not found", cleanly distinguished from RPC errors.
        """
        result, err = await cp._fetch_tx(_NONEXISTENT_SIG)
        self.assertIsNone(result)
        # Public devnet occasionally rate-limits; tolerate an RPC error string but
        # never an exception or a bogus result.
        if err is not None:
            self.assertIsInstance(err, str)

    async def test_malformed_signature_returns_error_not_crash(self):
        """A malformed signature should surface as an RPC error, gracefully."""
        result, err = await cp._fetch_tx("not-a-real-signature!!!")
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIsInstance(err, str)


@unittest.skipUnless(_RUN, "set RUN_DEVNET_TESTS=1 to run live devnet integration tests")
class DevnetVerifyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ensure_devnet_rpc()
        cp._consumed.clear()
        self.addCleanup(cp._consumed.clear)

    async def test_verify_path_is_well_formed(self):
        sig = os.environ.get("DEVNET_TEST_SIGNATURE")
        if not sig:
            # No real payment provided: verify a nonexistent sig returns a clean
            # negative result (ok=False) rather than raising.
            os.environ.setdefault("CRYPTO_PAY_TO", "So11111111111111111111111111111111111111112")
            ok, reason, asset = await cp.verify(_NONEXISTENT_SIG)
            self.assertFalse(ok)
            self.assertIsInstance(reason, str)
            self.assertIsNone(asset)
            return

        # A real devnet transfer was supplied — verify against it.
        self.assertTrue(os.environ.get("CRYPTO_PAY_TO"), "CRYPTO_PAY_TO required with DEVNET_TEST_SIGNATURE")
        ok, reason, asset = await cp.verify(sig)
        if os.environ.get("DEVNET_TEST_EXPECT_OK", "").lower() in ("1", "true", "yes"):
            self.assertTrue(ok, f"expected verification to pass, got: {reason}")
            self.assertIsNotNone(asset)
        else:
            self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()
