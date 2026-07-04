"""Solana wallet for the swarm — keypair custody + transaction broadcast.

The private key lives only in the PHANTOM_WALLET_PRIVATE_KEY env var and is
loaded into a solders Keypair at runtime. PumpPortal's local API returns an
*unsigned* transaction; we sign it here, so the key never leaves this process.

Safety rails (this is real money on mainnet):
  * PUMP_LIVE must be "true" to actually broadcast. Otherwise every launch /
    trade returns a dry-run plan and touches no chain. Default off so a stray
    request can never burn SOL.
  * MAX_DEV_BUY_SOL / MAX_TRADE_SOL cap any single spend, enforced by callers.
"""

import base64
import os

import httpx

# ── config ──────────────────────────────────────────────────────────
WALLET_PRIVATE_KEY = os.environ.get("PHANTOM_WALLET_PRIVATE_KEY", "")
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
PUMP_LIVE = os.environ.get("PUMP_LIVE", "").lower() == "true"

# hard spend caps (SOL) — callers must respect these
MAX_DEV_BUY_SOL = float(os.environ.get("MAX_DEV_BUY_SOL", "0.5"))
MAX_TRADE_SOL = float(os.environ.get("MAX_TRADE_SOL", "0.25"))

_LAMPORTS_PER_SOL = 1_000_000_000


def _load_keypair():
    """Load the signing keypair. Returns None if not configured."""
    if not WALLET_PRIVATE_KEY:
        return None
    from solders.keypair import Keypair  # imported lazily so the app boots without solders

    return Keypair.from_base58_string(WALLET_PRIVATE_KEY)


def wallet_pubkey() -> str | None:
    kp = _load_keypair()
    return str(kp.pubkey()) if kp else None


def is_configured() -> bool:
    return bool(WALLET_PRIVATE_KEY)


async def get_balance_sol() -> float | None:
    """Return the wallet's SOL balance, or None if unavailable."""
    pubkey = wallet_pubkey()
    if not pubkey:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [pubkey],
                },
            )
            data = resp.json()
        lamports = data.get("result", {}).get("value")
        return lamports / _LAMPORTS_PER_SOL if lamports is not None else None
    except Exception:
        return None


def sign_transaction(unsigned_tx_bytes: bytes, extra_signers: list | None = None):
    """Re-sign a PumpPortal local transaction with the wallet (+ any extra
    signers, e.g. a fresh mint keypair for token creation).

    Returns a signed solders VersionedTransaction.
    """
    from solders.transaction import VersionedTransaction

    kp = _load_keypair()
    if not kp:
        raise RuntimeError("PHANTOM_WALLET_PRIVATE_KEY not configured")

    tx = VersionedTransaction.from_bytes(unsigned_tx_bytes)
    signers = list(extra_signers or []) + [kp]
    return VersionedTransaction(tx.message, signers)


async def broadcast(signed_tx) -> dict:
    """Send a signed transaction to the RPC. Returns { signature, error }."""
    serialized = base64.b64encode(bytes(signed_tx)).decode()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        serialized,
                        {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
                    ],
                },
            )
            data = resp.json()
        if "error" in data:
            return {"signature": None, "error": str(data["error"])[:300]}
        return {"signature": data.get("result"), "error": None}
    except Exception as e:
        return {"signature": None, "error": str(e)[:300]}
