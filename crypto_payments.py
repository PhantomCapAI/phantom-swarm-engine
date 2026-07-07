"""Optional crypto payment gate (Solana) for bundle creation.

Same model as before: it charges people to run a bundle job on **your hosted
server**. The receiving wallet is operator-configured via ``CRYPTO_PAY_TO`` — it
is deliberately NOT hardcoded, so a fork of this repo never silently pays someone
else. Payment is a real on-chain transfer, verified against a Solana RPC before
any work starts, and each transaction signature is single-use.

Multi-asset: the operator can accept several assets (e.g. SOL and USDC), each
with its own price. The payer sends whichever they like; the server fetches the
transaction once and accepts it if it paid at least the price of ANY accepted
asset to ``CRYPTO_PAY_TO``.

Disabled by default. When enabled, ``POST /bundle/create`` requires either:
  * an admin bypass via the ``X-Phantom-Internal`` secret, or
  * an ``X-Payment-Tx`` header with a confirmed Solana signature.

Config (env, read at call time):

    CRYPTO_PAYMENTS_ENABLED            "1" / "true" to enable
    CRYPTO_PAY_TO                      receiving wallet (operator sets this)
    CRYPTO_ACCEPT                      accepted assets, e.g. "SOL:0.5,USDC:75"
                                       (each "ASSET:price"; custom SPL as
                                       "MINT:price:decimals:LABEL")
    CRYPTO_ASSET / CRYPTO_PRICE        legacy single-asset fallback (default SOL 0.5)
    USDC_MINT                          override USDC mint (e.g. for devnet)
    CRYPTO_NETWORK                     label, default "solana-mainnet"
    SOLANA_RPC_URL                     RPC endpoint (default public mainnet)
    CRYPTO_PAYMENTS_DEV_ACCEPT_TOKEN   testing-only: accept this exact signature
"""

import os

import httpx

# Well-known assets so operators can just say "SOL" / "USDC".
USDC_MAINNET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def enabled() -> bool:
    """Active only when the flag is on AND a receiving wallet is configured."""
    flag = _env("CRYPTO_PAYMENTS_ENABLED").lower() in ("1", "true", "yes", "on")
    return flag and bool(_env("CRYPTO_PAY_TO"))


def _rpc_url() -> str:
    return _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def _known_asset(name: str) -> dict | None:
    """Resolve a friendly asset name to a payment option (sans price)."""
    a = name.strip().upper()
    if a == "SOL":
        return {"asset": "SOL", "kind": "native", "mint": None, "decimals": 9}
    if a == "USDC":
        return {"asset": "USDC", "kind": "spl", "mint": _env("USDC_MINT", USDC_MAINNET_MINT), "decimals": 6}
    return None


def _accepted_options() -> list[dict]:
    """Parse the accepted assets into [{asset, kind, mint, decimals, price}]."""
    options: list[dict] = []
    raw = _env("CRYPTO_ACCEPT")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            bits = [b.strip() for b in part.split(":")]
            name, price = bits[0], (bits[1] if len(bits) > 1 else _env("CRYPTO_PRICE", "0.5"))
            known = _known_asset(name)
            if known:
                opt = dict(known)
                opt["price"] = price
            elif len(bits) >= 3:  # custom SPL: MINT:price:decimals[:LABEL]
                opt = {
                    "asset": bits[3] if len(bits) > 3 else name[:6],
                    "kind": "spl",
                    "mint": name,
                    "decimals": int(bits[2]),
                    "price": price,
                }
            else:
                continue
            options.append(opt)

    if not options:  # legacy single-asset fallback
        asset = _env("CRYPTO_ASSET", "SOL")
        price = _env("CRYPTO_PRICE", "0.5")
        known = _known_asset(asset)
        if known:
            opt = dict(known)
            opt["price"] = price
        else:  # raw SPL mint
            opt = {
                "asset": asset[:6],
                "kind": "spl",
                "mint": asset,
                "decimals": int(_env("CRYPTO_ASSET_DECIMALS", "6") or "6"),
                "price": price,
            }
        options.append(opt)
    return options


def _required(opt: dict) -> int:
    """Price in the asset's smallest unit."""
    try:
        return int(round(float(opt["price"]) * (10 ** opt["decimals"])))
    except Exception:
        return 0


def pricing() -> dict:
    """Pricing + payment instructions (for UIs / the /bundle/pricing endpoint)."""
    options = _accepted_options()
    public = [{"asset": o["asset"], "price": o["price"]} for o in options]
    return {
        "enabled": enabled(),
        "provider": "crypto",
        "network": _env("CRYPTO_NETWORK", "solana-mainnet"),
        "pay_to": _env("CRYPTO_PAY_TO"),
        "options": public,
        # Convenience for simple UIs — the first accepted asset.
        "asset": public[0]["asset"] if public else "SOL",
        "price": public[0]["price"] if public else "0.5",
        "instructions": (
            "Send any accepted asset to pay_to from your wallet, then POST "
            "/bundle/create with header 'X-Payment-Tx: <transaction signature>'."
        ),
    }


async def _fetch_tx(sig: str) -> dict | None:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            _rpc_url(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            },
        )
        return r.json().get("result")


def _check_sol(result: dict, pay_to: str, required: int) -> tuple[bool, str]:
    """Native SOL transfer to pay_to, via preBalances/postBalances delta."""
    meta = result.get("meta") or {}
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
        return False, f"insufficient: {delta} lamports, need {required}"
    return True, "verified"


def _check_spl(result: dict, pay_to: str, mint: str, required: int) -> tuple[bool, str]:
    """SPL-token transfer to pay_to, via token-balance deltas."""
    meta = result.get("meta") or {}
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


async def verify(sig: str) -> tuple[bool, str, str | None]:
    """Verify a payment. Returns (ok, reason, asset_paid).

    Fetches the transaction once, then checks each accepted asset until one
    satisfies its price. Accepts payment in whichever asset the payer chose.
    """
    if sig in _consumed:
        return False, "signature already used", None

    result = await _fetch_tx(sig)
    if not result:
        return False, "tx not found or not yet confirmed", None
    if (result.get("meta") or {}).get("err") is not None:
        return False, "tx failed on-chain", None

    pay_to = _env("CRYPTO_PAY_TO")
    reasons = []
    for opt in _accepted_options():
        required = _required(opt)
        if opt["kind"] == "native":
            ok, reason = _check_sol(result, pay_to, required)
        else:
            ok, reason = _check_spl(result, pay_to, opt["mint"], required)
        if ok:
            return True, "verified", opt["asset"]
        reasons.append(f"{opt['asset']}: {reason}")
    return False, "; ".join(reasons) or "no accepted payment found", None


def consume(sig: str) -> None:
    if sig:
        _consumed.add(sig)


async def check(headers) -> dict:
    """Authorize a bundle-create request. Returns {"ok","via","tx"?,"asset"?,"reason"?}."""
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

    ok, reason, asset = await verify(sig)
    if ok:
        consume(sig)
        return {"ok": True, "via": "crypto", "tx": sig, "asset": asset}
    return {"ok": False, "via": "crypto", "reason": reason}
