"""Optional crypto payment gate (Solana) for bundle creation.

Same model as the Stripe gate: it charges people to run a bundle job on **your
hosted server**. The receiving wallet is set by the operator via ``CRYPTO_PAY_TO``
— it is deliberately NOT hardcoded, so a fork of this repo never silently pays
someone else's address. Payment is a real on-chain transfer, verified against a
Solana RPC before any work starts, and each transaction signature is single-use.

Disabled by default. When enabled, ``POST /bundle/create`` requires either:
  * an admin bypass via the ``X-Phantom-Internal`` secret, or
  * an ``X-Payment-Tx`` header with a confirmed Solana signature that paid at
    least the price to ``CRYPTO_PAY_TO``.

Flow:
  1. client calls ``GET /bundle/pricing`` -> sees pay-to wallet + amount
  2. client sends the payment from their own wallet
  3. client calls ``POST /bundle/create`` with ``X-Payment-Tx: <signature>``
  4. server verifies the transfer on-chain, then runs the job

Config (env, read at call time):

    CRYPTO_PAYMENTS_ENABLED            "1" / "true" to enable
    CRYPTO_PAY_TO                      receiving wallet (operator sets this)
    CRYPTO_PRICE                       amount, e.g. "0.5"
    CRYPTO_ASSET                       "SOL" (native) or an SPL mint address
    CRYPTO_ASSET_DECIMALS              atomic decimals (default 9)
    CRYPTO_NETWORK                     label, default "solana-mainnet"
    SOLANA_RPC_URL                     RPC endpoint (default public mainnet)
    CRYPTO_PAYMENTS_DEV_ACCEPT_TOKEN   testing-only: accept this exact signature
"""

import os

import httpx


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def enabled() -> bool:
    """Active only when the flag is on AND a receiving wallet is configured."""
    flag = _env("CRYPTO_PAYMENTS_ENABLED").lower() in ("1", "true", "yes", "on")
    return flag and bool(_env("CRYPTO_PAY_TO"))


def _rpc_url() -> str:
    return _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def pricing() -> dict:
    """Pricing + payment instructions (for UIs / the /bundle/pricing endpoint)."""
    return {
        "enabled": enabled(),
        "provider": "crypto",
        "network": _env("CRYPTO_NETWORK", "solana-mainnet"),
        "price": _env("CRYPTO_PRICE", "0.5"),
        "asset": _env("CRYPTO_ASSET", "SOL"),
        "pay_to": _env("CRYPTO_PAY_TO"),
        "instructions": (
            "Send the amount to pay_to from your wallet, then POST /bundle/create "
            "with header 'X-Payment-Tx: <transaction signature>'."
        ),
    }


def _required_atomic() -> int:
    """Price expressed in the asset's smallest unit (lamports for SOL)."""
    try:
        decimals = int(_env("CRYPTO_ASSET_DECIMALS", "9") or "9")
        return int(round(float(_env("CRYPTO_PRICE", "0.5")) * (10 ** decimals)))
    except Exception:
        return 0


async def _rpc(method: str, params: list) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            _rpc_url(),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        return r.json()


async def _verify_sol(sig: str, pay_to: str, required: int) -> tuple[bool, str]:
    """Verify a native SOL transfer to pay_to via preBalances/postBalances delta."""
    data = await _rpc(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    result = data.get("result")
    if not result:
        return False, "tx not found or not yet confirmed"
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return False, "tx failed on-chain"

    msg = (result.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == pay_to:
            idx = i
            break
    if idx is None:
        return False, "recipient wallet not in transaction"

    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return False, "balance data missing"
    delta = post[idx] - pre[idx]
    if delta < required:
        return False, f"insufficient: received {delta} lamports, need {required}"
    return True, "verified"


async def _verify_spl(sig: str, pay_to: str, mint: str, required: int) -> tuple[bool, str]:
    """Verify an SPL-token transfer to pay_to via token-balance deltas."""
    data = await _rpc(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    result = data.get("result")
    if not result:
        return False, "tx not found or not yet confirmed"
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return False, "tx failed on-chain"

    pre = {
        (b.get("owner"), b.get("mint")): int((b.get("uiTokenAmount") or {}).get("amount") or 0)
        for b in (meta.get("preTokenBalances") or [])
    }
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == pay_to and b.get("mint") == mint:
            post_amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            delta = post_amt - pre.get((pay_to, mint), 0)
            if delta >= required:
                return True, "verified"
            return False, f"insufficient token transfer ({delta} < {required})"
    return False, "no matching token transfer to recipient"


# Consumed signatures (replay protection). In-memory: resets on restart, which
# only fails closed — a spent signature is never falsely accepted twice.
_consumed: set = set()


async def verify(sig: str) -> tuple[bool, str]:
    if sig in _consumed:
        return False, "signature already used"
    pay_to = _env("CRYPTO_PAY_TO")
    asset = _env("CRYPTO_ASSET", "SOL")
    required = _required_atomic()
    if asset.upper() == "SOL":
        return await _verify_sol(sig, pay_to, required)
    return await _verify_spl(sig, pay_to, asset, required)


def consume(sig: str) -> None:
    if sig:
        _consumed.add(sig)


async def check(headers) -> dict:
    """Authorize a bundle-create request. Returns {"ok","via","tx"?,"reason"?}."""
    if not enabled():
        return {"ok": True, "via": "disabled"}

    secret = _env("PHANTOM_INTERNAL_SECRET")
    if secret and headers.get("X-Phantom-Internal") == secret:
        return {"ok": True, "via": "internal"}

    sig = headers.get("X-Payment-Tx")
    if not sig:
        return {"ok": False, "via": "none", "reason": "payment required"}

    dev = _env("CRYPTO_PAYMENTS_DEV_ACCEPT_TOKEN")
    if dev and sig == dev:
        return {"ok": True, "via": "dev-accept", "tx": sig}

    ok, reason = await verify(sig)
    if ok:
        consume(sig)
        return {"ok": True, "via": "crypto", "tx": sig}
    return {"ok": False, "via": "crypto", "reason": reason}
