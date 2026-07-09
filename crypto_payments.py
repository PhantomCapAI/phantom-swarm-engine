"""Optional crypto payment gate (Solana) for bundle creation.

It charges people to run a bundle job on **your hosted server**. The receiving
wallet is operator-configured via ``CRYPTO_PAY_TO`` — deliberately NOT hardcoded,
so a fork of this repo never silently pays someone else. Payment is a real
on-chain transfer, verified against a Solana RPC before any work starts, and
each transaction signature is single-use (persisted, so a redeploy can't reset
replay protection).

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
    CRYPTO_COMMITMENT                  "confirmed" (default) or "finalized"
    CRYPTO_PAYMENTS_DEV_ACCEPT_TOKEN   testing-only: accept this exact signature
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

import config
import store
from logging_setup import get_logger

log = get_logger("phantom.crypto")

# Well-known assets so operators can just say "SOL" / "USDC".
USDC_MAINNET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# getTransaction only accepts these commitment levels.
_VALID_COMMITMENTS = ("confirmed", "finalized")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def enabled() -> bool:
    """Active only when the flag is on AND a receiving wallet is configured."""
    flag = _env("CRYPTO_PAYMENTS_ENABLED").lower() in ("1", "true", "yes", "on")
    return flag and bool(_env("CRYPTO_PAY_TO"))


def _rpc_url() -> str:
    return _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def _commitment() -> str:
    c = _env("CRYPTO_COMMITMENT", "confirmed").lower()
    return c if c in _VALID_COMMITMENTS else "confirmed"


def _known_asset(name: str) -> Optional[dict]:
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
                try:
                    decimals = int(bits[2])
                except ValueError:
                    log.warning("crypto: bad decimals in CRYPTO_ACCEPT entry %r; skipping", part)
                    continue
                opt = {
                    "asset": bits[3] if len(bits) > 3 else name[:6],
                    "kind": "spl",
                    "mint": name,
                    "decimals": decimals,
                    "price": price,
                }
            else:
                log.warning("crypto: unrecognized CRYPTO_ACCEPT entry %r; skipping", part)
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
    except (TypeError, ValueError, KeyError):
        return 0


def pricing() -> dict:
    """Pricing + payment instructions (for UIs / the /bundle/pricing endpoint)."""
    options = _accepted_options()
    public = [{"asset": o["asset"], "price": o["price"]} for o in options]
    return {
        "enabled": enabled(),
        "provider": "crypto",
        "network": _env("CRYPTO_NETWORK", "solana-mainnet"),
        "commitment": _commitment(),
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


async def _fetch_tx(sig: str) -> tuple[Optional[dict], Optional[str]]:
    """Fetch a transaction by signature.

    Returns ``(result, error)``. ``result`` is the RPC ``result`` object (or None
    if the tx isn't found yet); ``error`` is a human message for RPC/transport
    failures so the caller can distinguish "not found" from "RPC down".
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            sig,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": _commitment(),
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=config.RPC_TIMEOUT_S) as c:
            r = await c.post(_rpc_url(), json=payload)
    except httpx.TimeoutException:
        return None, "RPC timeout — the Solana endpoint did not respond in time"
    except httpx.HTTPError as e:
        return None, f"RPC transport error: {str(e)[:120]}"

    if r.status_code != 200:
        return None, f"RPC returned HTTP {r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return None, "RPC returned non-JSON response"
    if isinstance(body, dict) and body.get("error"):
        msg = (body["error"] or {}).get("message", "unknown RPC error")
        return None, f"RPC error: {msg}"
    return body.get("result"), None


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
        return False, f"insufficient: received {delta} lamports, need {required}"
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


# In-memory mirror of consumed signatures for the current process. The durable
# record lives in ``store`` (Redis or disk) so replay protection survives a
# restart; this set just avoids a store round-trip on the hot path.
_consumed: set[str] = set()


def _is_consumed(sig: str) -> bool:
    return sig in _consumed or store.is_signature_used(sig)


async def verify(sig: str) -> tuple[bool, str, Optional[str]]:
    """Verify a payment. Returns (ok, reason, asset_paid).

    Fetches the transaction once, confirms it succeeded on-chain (``meta.err``
    is None), then checks each accepted asset until one satisfies its price.
    """
    if not sig or not isinstance(sig, str) or len(sig) > 128:
        return False, "invalid signature", None
    if _is_consumed(sig):
        return False, "signature already used", None

    result, err = await _fetch_tx(sig)
    if err:
        return False, err, None
    if not result:
        return False, "tx not found or not yet confirmed at the configured commitment level", None
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return False, "tx failed on-chain (meta.err is set)", None

    pay_to = _env("CRYPTO_PAY_TO")
    if not pay_to:
        return False, "server misconfigured: no receiving wallet", None

    reasons: list[str] = []
    for opt in _accepted_options():
        required = _required(opt)
        if required <= 0:
            reasons.append(f"{opt['asset']}: invalid price configured")
            continue
        if opt["kind"] == "native":
            ok, reason = _check_sol(result, pay_to, required)
        else:
            ok, reason = _check_spl(result, pay_to, opt["mint"], required)
        if ok:
            return True, "verified", opt["asset"]
        reasons.append(f"{opt['asset']}: {reason}")
    return False, "; ".join(reasons) or "no accepted payment found", None


def consume(sig: str) -> None:
    """Mark a signature spent, in-process and durably."""
    if sig:
        _consumed.add(sig)
        try:
            store.mark_signature_used(sig)
        except Exception as e:  # never fail a paid request over a storage hiccup
            log.warning("crypto: could not persist consumed signature (%s)", str(e)[:120])


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
        log.info("crypto: payment verified (asset=%s)", asset)
        return {"ok": True, "via": "crypto", "tx": sig, "asset": asset}
    log.info("crypto: payment rejected (%s)", reason)
    return {"ok": False, "via": "crypto", "reason": reason}
