"""Pump.fun / Solana token-launch domain pack for the bundler.

When a spec is about launching (or trading) tokens on pump.fun, a generic bundle
of prompts isn't enough — the agent needs *real, callable tools*. This module
detects that domain and enriches the blueprint with:

* battle-tested skill definitions (PumpPortal launch/buy/sell, IPFS metadata,
  wallet signing, launch timing & bundling, post-launch monitoring & risk),
* a ``solana-launch`` output target that emits ready-to-run Python tool modules
  (``pumpportal_client.py``, ``ipfs_metadata.py``, ``wallet.py``,
  ``launch_strategy.py``, ``monitor.py``, ``risk_controls.py``) plus a
  machine-readable ``tools.json`` the agent can tool-call against,
* concrete pump.fun example usage and safety controls.

Kept separate from ``bundler.py`` so the core pipeline stays generic and adding a
new domain pack is a single import. The emitted code targets PumpPortal's
documented Local Transaction API (you sign locally; keys never leave your box)
and the pump.fun IPFS endpoint. Verify endpoints against current PumpPortal docs
before mainnet use — APIs move.
"""

from __future__ import annotations

import json
from typing import Optional

# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
_KEYWORDS = (
    "pump.fun", "pumpfun", "pumpportal", "pump portal", "token launch", "launch a token",
    "launch a coin", "memecoin", "meme coin", "meme-coin", "sniper", "snipe",
    "bundle buy", "bundled buy", "bonding curve", "dev wallet", "solana token",
    "spl token launch", "coin launcher", "rug", "initial buy",
)


def matches(spec_text: str, blueprint: Optional[dict] = None) -> bool:
    """True if the spec (or drafted blueprint) is about a pump.fun/Solana launch."""
    hay = (spec_text or "").lower()
    if blueprint:
        hay += " " + json.dumps(
            {k: blueprint.get(k) for k in ("name", "description", "tagline")}
        ).lower()
    return any(kw in hay for kw in _KEYWORDS)


# --------------------------------------------------------------------------- #
# Skills injected into the blueprint (so prompts/personas + skill docs mention
# the real capabilities and how to use them). These are copy-paste-ready.
# --------------------------------------------------------------------------- #
SKILLS: list[dict] = [
    {
        "name": "Launch Token (pump.fun)",
        "slug": "launch-token",
        "description": "Create a new pump.fun token via PumpPortal, signing the transaction locally.",
        "instructions": (
            "Call `pumpportal_client.create_token(...)` with name, symbol, a metadata URI "
            "(from `ipfs_metadata.upload`), and the dev buy amount in SOL. It builds the "
            "create transaction with PumpPortal's Local Transaction API, co-signs with the "
            "mint keypair and the dev wallet, and submits to the configured RPC. ALWAYS run "
            "`risk_controls.preflight()` first and respect the max-spend cap."
        ),
        "example": 'create_token(name="Phantom Cat", symbol="PCAT", uri=uri, dev_buy_sol=0.5)',
    },
    {
        "name": "Upload Metadata + Image (IPFS)",
        "slug": "upload-metadata",
        "description": "Pin token image + metadata to IPFS via the pump.fun endpoint and get a metadata URI.",
        "instructions": (
            "Call `ipfs_metadata.upload(image_path, name, symbol, description, twitter, telegram, website)`. "
            "Returns the `metadataUri` you pass to `launch-token`. Validate the image is < 5MB and a PNG/JPG/GIF."
        ),
        "example": 'uri = upload("logo.png", "Phantom Cat", "PCAT", "the first phantom cat")',
    },
    {
        "name": "Wallet Management & Signing",
        "slug": "wallet-signing",
        "description": "Load a Solana keypair from env/file, check balances, and sign transactions locally.",
        "instructions": (
            "Use `wallet.load_keypair()` (reads `SOLANA_PRIVATE_KEY`, base58) and `wallet.get_balance_sol()`. "
            "Keys NEVER leave the process — PumpPortal's Local API only returns an unsigned tx which you sign here. "
            "Never log or transmit the private key."
        ),
        "example": 'kp = load_keypair(); bal = get_balance_sol(kp.pubkey())',
    },
    {
        "name": "Launch Strategy & Bundling",
        "slug": "launch-strategy",
        "description": "Decide launch timing, dev buy size, and multi-wallet bundled opening buys.",
        "instructions": (
            "Use `launch_strategy.plan(...)` to compute a launch plan: dev buy, staggered/bundled buys across "
            "funded wallets, slippage and priority fee. Use `launch_strategy.execute_bundle(plan)` to fire the "
            "opening buys. Bundling concentrates the opening candle — size it within `risk_controls` limits and "
            "disclose that these are your own wallets. Do not use bundling to fake organic volume against others."
        ),
        "example": 'plan = plan(dev_buy_sol=0.5, bundle_wallets=3, per_wallet_sol=0.1); execute_bundle(plan)',
    },
    {
        "name": "Post-launch Monitoring & Risk",
        "slug": "post-launch-monitor",
        "description": "Watch price, market cap, holders and PnL; trigger take-profit / stop-loss / kill-switch.",
        "instructions": (
            "Run `monitor.watch(mint, targets)` after launch. It polls the token's stats and calls back on "
            "take-profit, stop-loss, or anomaly (sudden dev sell, LP pull). Wire it to `pumpportal_client.sell(...)` "
            "for automated exits, always bounded by `risk_controls`."
        ),
        "example": 'watch(mint, take_profit_mult=3.0, stop_loss_mult=0.5)',
    },
    {
        "name": "Risk Controls & Kill-switch",
        "slug": "risk-controls",
        "description": "Hard caps on spend, slippage, and wallet allow-listing; a global kill-switch.",
        "instructions": (
            "Every action routes through `risk_controls.preflight(action, amount_sol)`. It enforces "
            "`MAX_SPEND_SOL`, `MAX_SLIPPAGE_PCT`, a dry-run mode (`LAUNCH_DRY_RUN=1`), and a kill-switch file. "
            "Fail closed: if a limit would be exceeded, the action is refused."
        ),
        "example": 'risk_controls.preflight("buy", 0.2)  # raises RiskError if over limits',
    },
]


def examples() -> list[dict]:
    """Concrete pump.fun example usage / test cases for the bundle."""
    return [
        {
            "name": "launch-a-token",
            "input": "Launch a token named 'Phantom Cat' (PCAT) with a 0.5 SOL dev buy and logo.png.",
            "expected": (
                "Agent: upload-metadata(logo.png,...) -> metadataUri; risk_controls.preflight('create',0.5); "
                "launch-token(name='Phantom Cat', symbol='PCAT', uri=..., dev_buy_sol=0.5) -> mint address + tx sig."
            ),
        },
        {
            "name": "snipe-new-launch",
            "input": "Snipe mint <MINT> with 0.2 SOL at 15% slippage the moment it launches.",
            "expected": (
                "risk_controls.preflight('buy',0.2); pumpportal_client.buy(mint='<MINT>', amount_sol=0.2, slippage=15) "
                "-> confirmed buy tx; monitor.watch(mint, take_profit_mult=3, stop_loss_mult=0.5)."
            ),
        },
        {
            "name": "safe-exit",
            "input": "Take profit at 3x, stop loss at 0.5x for mint <MINT>.",
            "expected": "monitor.watch triggers sell(mint, percent=100) via pumpportal_client on either threshold.",
        },
    ]


def enrich_blueprint(bp: dict, spec_text: str) -> dict:
    """Add pump.fun skills, examples, and the solana-launch target to a blueprint.

    Idempotent and pure: safe to call on an already-enriched blueprint. Only adds
    skills/examples not already present (by slug/name) so a model that already
    proposed some launch skills isn't duplicated.
    """
    existing_skill_slugs = {s.get("slug") for s in bp.get("skills", [])}
    for skill in SKILLS:
        if skill["slug"] not in existing_skill_slugs:
            bp.setdefault("skills", []).append(dict(skill))

    existing_example_names = {e.get("name") for e in bp.get("examples", [])}
    for ex in examples():
        if ex["name"] not in existing_example_names:
            bp.setdefault("examples", []).append(dict(ex))

    if "solana-launch" not in bp.get("targets", []):
        bp.setdefault("targets", []).append("solana-launch")

    bp["domain"] = "pumpfun"
    return bp


# --------------------------------------------------------------------------- #
# Emitted tool modules (plain strings — real braces, no f-string escaping).
# These are the copy-paste-ready tools the generated agent can call.
# --------------------------------------------------------------------------- #
_RISK_CONTROLS_PY = '''"""Risk controls for pump.fun automation. Fail closed.

Every spend action must pass `preflight()`. Limits come from the environment so
they can be tuned per deployment without editing code:

    MAX_SPEND_SOL       hard cap on any single action's SOL amount (default 1.0)
    MAX_SLIPPAGE_PCT    reject buys/sells above this slippage (default 25)
    DAILY_SPEND_SOL     soft cap tracked in-process for the session (default 5.0)
    LAUNCH_DRY_RUN      "1" to simulate — no transaction is ever submitted
    KILL_SWITCH_FILE    if this file exists, ALL actions are refused
"""
import os

MAX_SPEND_SOL = float(os.environ.get("MAX_SPEND_SOL", "1.0"))
MAX_SLIPPAGE_PCT = float(os.environ.get("MAX_SLIPPAGE_PCT", "25"))
DAILY_SPEND_SOL = float(os.environ.get("DAILY_SPEND_SOL", "5.0"))
KILL_SWITCH_FILE = os.environ.get("KILL_SWITCH_FILE", ".kill")

_spent_this_session = 0.0


class RiskError(Exception):
    """Raised when an action would breach a risk limit."""


def dry_run() -> bool:
    return os.environ.get("LAUNCH_DRY_RUN", "").lower() in ("1", "true", "yes")


def preflight(action: str, amount_sol: float, slippage_pct: float = 0.0) -> None:
    """Validate an action before it spends anything. Raises RiskError if unsafe."""
    global _spent_this_session
    if os.path.exists(KILL_SWITCH_FILE):
        raise RiskError(f"kill-switch active ({KILL_SWITCH_FILE} present); refusing {action}")
    if amount_sol < 0:
        raise RiskError("amount must be non-negative")
    if amount_sol > MAX_SPEND_SOL:
        raise RiskError(f"{action}: {amount_sol} SOL exceeds MAX_SPEND_SOL={MAX_SPEND_SOL}")
    if slippage_pct and slippage_pct > MAX_SLIPPAGE_PCT:
        raise RiskError(f"{action}: slippage {slippage_pct}% exceeds MAX_SLIPPAGE_PCT={MAX_SLIPPAGE_PCT}")
    if _spent_this_session + amount_sol > DAILY_SPEND_SOL:
        raise RiskError(
            f"{action}: session spend {_spent_this_session + amount_sol} SOL exceeds DAILY_SPEND_SOL={DAILY_SPEND_SOL}"
        )
    _spent_this_session += amount_sol


def record_refund(amount_sol: float) -> None:
    """Return budget when an action is aborted after preflight but before spend."""
    global _spent_this_session
    _spent_this_session = max(0.0, _spent_this_session - amount_sol)
'''

_WALLET_PY = '''"""Wallet loading, balances, and local signing helpers.

Private keys never leave this process. PumpPortal's Local Transaction API returns
an *unsigned* transaction; we sign it here with solders and submit via RPC.
"""
import base64
import os

from solders.keypair import Keypair
from solana.rpc.api import Client

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def rpc() -> Client:
    return Client(RPC_URL)


def load_keypair(env_var: str = "SOLANA_PRIVATE_KEY") -> Keypair:
    """Load a keypair from a base58 secret key in the environment.

    Accepts a base58 string (Phantom export) or a JSON byte array. Never logs it.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise RuntimeError(f"{env_var} not set")
    if raw.startswith("["):
        import json
        return Keypair.from_bytes(bytes(json.loads(raw)))
    return Keypair.from_base58_string(raw)


def get_balance_sol(pubkey) -> float:
    lamports = rpc().get_balance(pubkey).value
    return lamports / 1_000_000_000
'''

_IPFS_METADATA_PY = '''"""Upload token image + metadata to IPFS via pump.fun's endpoint.

Returns a `metadataUri` you pass to pumpportal_client.create_token(). Verify the
endpoint against current pump.fun docs before mainnet use.
"""
import os

import requests

PUMP_IPFS_URL = os.environ.get("PUMP_IPFS_URL", "https://pump.fun/api/ipfs")
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def upload(
    image_path: str,
    name: str,
    symbol: str,
    description: str = "",
    twitter: str = "",
    telegram: str = "",
    website: str = "",
    timeout: float = 60.0,
) -> str:
    """Pin image + metadata and return the metadata URI."""
    if not name or not symbol:
        raise ValueError("upload requires name and symbol")
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"image not found: {image_path}")
    size = os.path.getsize(image_path)
    if size == 0:
        raise ValueError(f"image is empty: {image_path}")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large: {size} bytes (max {MAX_IMAGE_BYTES})")

    form = {
        "name": name,
        "symbol": symbol,
        "description": description,
        "twitter": twitter,
        "telegram": telegram,
        "website": website,
        "showName": "true",
    }
    try:
        with open(image_path, "rb") as fh:
            files = {"file": (os.path.basename(image_path), fh, "image/png")}
            resp = requests.post(PUMP_IPFS_URL, data=form, files=files, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"IPFS upload failed: {e}") from e
    except ValueError as e:  # non-JSON response
        raise RuntimeError(f"IPFS response was not JSON: {e}") from e
    uri = data.get("metadataUri") or data.get("metadata_uri")
    if not uri:
        raise RuntimeError(f"no metadataUri in IPFS response: {data}")
    return uri
'''

_PUMPPORTAL_CLIENT_PY = '''"""PumpPortal Local Transaction API client: create / buy / sell.

Local API = PumpPortal returns an *unsigned* serialized transaction; you sign it
with your own keypair(s) and submit to your RPC. Your keys never leave this
process and PumpPortal takes no trading fee on the local API (network/priority
fees still apply).

Docs: https://pumpportal.fun/local-trading-api/  (verify shapes before mainnet)
"""
import os

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

import risk_controls
import wallet

TRADE_LOCAL_URL = os.environ.get("PUMPPORTAL_TRADE_LOCAL_URL", "https://pumpportal.fun/api/trade-local")
DEFAULT_SLIPPAGE = float(os.environ.get("DEFAULT_SLIPPAGE_PCT", "10"))
DEFAULT_PRIORITY_FEE = float(os.environ.get("DEFAULT_PRIORITY_FEE_SOL", "0.0005"))
POOL = os.environ.get("PUMP_POOL", "pump")


class PumpPortalError(RuntimeError):
    """A PumpPortal request or transaction submission failed."""


def _build_local_tx(payload: dict, timeout: float = 30.0) -> bytes:
    """POST to the Local Transaction API and return the unsigned tx bytes.

    Surfaces the response body on failure (PumpPortal returns a JSON error there)
    so callers get an actionable message instead of a bare status code.
    """
    try:
        resp = requests.post(TRADE_LOCAL_URL, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise PumpPortalError(f"PumpPortal request failed: {e}") from e
    if resp.status_code != 200:
        raise PumpPortalError(f"PumpPortal returned HTTP {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        raise PumpPortalError("PumpPortal returned an empty transaction body")
    return resp.content  # raw serialized (unsigned) transaction bytes


def _sign_and_send(tx_bytes: bytes, signers: list) -> str:
    """Co-sign a PumpPortal transaction and submit it to the RPC. Returns the sig."""
    try:
        unsigned = VersionedTransaction.from_bytes(tx_bytes)
        signed = VersionedTransaction(unsigned.message, signers)
        result = wallet.rpc().send_raw_transaction(
            bytes(signed), opts=TxOpts(preflight_commitment=Confirmed)
        )
    except Exception as e:
        raise PumpPortalError(f"signing/submit failed: {e}") from e
    return str(result.value)


def create_token(
    name: str,
    symbol: str,
    uri: str,
    dev_buy_sol: float = 0.0,
    slippage_pct: float = DEFAULT_SLIPPAGE,
    priority_fee_sol: float = DEFAULT_PRIORITY_FEE,
) -> dict:
    """Create a new pump.fun token, optionally with a dev buy. Returns {mint, signature}."""
    if not name or not symbol or not uri:
        raise ValueError("create_token requires name, symbol, and a metadata uri")
    risk_controls.preflight("create", dev_buy_sol, slippage_pct)
    dev = wallet.load_keypair()
    mint = Keypair()  # the new token's mint address

    payload = {
        "publicKey": str(dev.pubkey()),
        "action": "create",
        "tokenMetadata": {"name": name, "symbol": symbol, "uri": uri},
        "mint": str(mint.pubkey()),
        "denominatedInSol": "true",
        "amount": dev_buy_sol,
        "slippage": slippage_pct,
        "priorityFee": priority_fee_sol,
        "pool": POOL,
    }
    if risk_controls.dry_run():
        return {"mint": str(mint.pubkey()), "signature": "DRY_RUN", "payload": payload}

    tx_bytes = _build_local_tx(payload)
    sig = _sign_and_send(tx_bytes, [mint, dev])  # mint signs first, then the dev wallet
    return {"mint": str(mint.pubkey()), "signature": sig}


def buy(mint: str, amount_sol: float, slippage_pct: float = DEFAULT_SLIPPAGE,
        priority_fee_sol: float = DEFAULT_PRIORITY_FEE) -> dict:
    """Buy `amount_sol` worth of `mint`. Returns {signature}."""
    if not mint:
        raise ValueError("buy requires a mint address")
    if amount_sol <= 0:
        raise ValueError("buy amount_sol must be positive")
    risk_controls.preflight("buy", amount_sol, slippage_pct)
    signer = wallet.load_keypair()
    payload = {
        "publicKey": str(signer.pubkey()),
        "action": "buy",
        "mint": mint,
        "denominatedInSol": "true",
        "amount": amount_sol,
        "slippage": slippage_pct,
        "priorityFee": priority_fee_sol,
        "pool": POOL,
    }
    if risk_controls.dry_run():
        return {"signature": "DRY_RUN", "payload": payload}
    sig = _sign_and_send(_build_local_tx(payload), [signer])
    return {"signature": sig}


def sell(mint: str, percent: float = 100.0, slippage_pct: float = DEFAULT_SLIPPAGE,
         priority_fee_sol: float = DEFAULT_PRIORITY_FEE) -> dict:
    """Sell `percent` of the held `mint` (amount is a token %, denominatedInSol=false)."""
    if not mint:
        raise ValueError("sell requires a mint address")
    if not 0 < percent <= 100:
        raise ValueError("sell percent must be in (0, 100]")
    risk_controls.preflight("sell", 0.0, slippage_pct)  # selling doesn't spend SOL principal
    signer = wallet.load_keypair()
    payload = {
        "publicKey": str(signer.pubkey()),
        "action": "sell",
        "mint": mint,
        "denominatedInSol": "false",
        "amount": f"{percent}%",
        "slippage": slippage_pct,
        "priorityFee": priority_fee_sol,
        "pool": POOL,
    }
    if risk_controls.dry_run():
        return {"signature": "DRY_RUN", "payload": payload}
    sig = _sign_and_send(_build_local_tx(payload), [signer])
    return {"signature": sig}
'''

_LAUNCH_STRATEGY_PY = '''"""Launch timing, initial-buy sizing, and multi-wallet bundled opening buys.

Bundling here means firing your OWN funded wallets' opening buys close together
to shape the opening candle. Keep it within risk limits and be transparent — do
not use it to fabricate organic volume or to harm other traders.
"""
import os
import time
from dataclasses import dataclass, field

import pumpportal_client
import risk_controls


@dataclass
class LaunchPlan:
    dev_buy_sol: float = 0.5
    bundle_wallets: int = 0
    per_wallet_sol: float = 0.1
    slippage_pct: float = 10.0
    priority_fee_sol: float = 0.0005
    stagger_seconds: float = 0.0  # 0 = as fast as possible
    notes: list = field(default_factory=list)

    @property
    def total_sol(self) -> float:
        return self.dev_buy_sol + self.bundle_wallets * self.per_wallet_sol


def plan(dev_buy_sol: float = 0.5, bundle_wallets: int = 0, per_wallet_sol: float = 0.1,
         slippage_pct: float = 10.0, stagger_seconds: float = 0.0) -> LaunchPlan:
    """Build a launch plan and validate the total against risk limits."""
    p = LaunchPlan(
        dev_buy_sol=dev_buy_sol, bundle_wallets=bundle_wallets, per_wallet_sol=per_wallet_sol,
        slippage_pct=slippage_pct, stagger_seconds=stagger_seconds,
    )
    # Dry preflight of the aggregate spend so an over-budget plan fails early.
    risk_controls.preflight("plan", p.total_sol, slippage_pct)
    risk_controls.record_refund(p.total_sol)  # only a validation; don't consume budget yet
    p.notes.append(f"total opening spend ~= {p.total_sol} SOL across {1 + bundle_wallets} wallet(s)")
    return p


def execute_bundle(p: LaunchPlan, mint: str) -> list:
    """Fire opening buys for a freshly-created `mint` across the bundle wallets.

    Each extra wallet's key is read from BUNDLE_WALLET_1..N env vars (base58).
    Returns a list of per-buy result dicts.
    """
    results = []
    for i in range(1, p.bundle_wallets + 1):
        env_var = f"BUNDLE_WALLET_{i}"
        if not os.environ.get(env_var):
            results.append({"wallet": env_var, "error": "key not set"})
            continue
        # pumpportal_client reads SOLANA_PRIVATE_KEY; point it at this wallet.
        prev = os.environ.get("SOLANA_PRIVATE_KEY")
        os.environ["SOLANA_PRIVATE_KEY"] = os.environ[env_var]
        try:
            results.append({"wallet": env_var, **pumpportal_client.buy(
                mint, p.per_wallet_sol, p.slippage_pct, p.priority_fee_sol)})
        except Exception as e:  # one wallet failing shouldn't abort the rest
            results.append({"wallet": env_var, "error": str(e)})
        finally:
            if prev is None:
                os.environ.pop("SOLANA_PRIVATE_KEY", None)
            else:
                os.environ["SOLANA_PRIVATE_KEY"] = prev
        if p.stagger_seconds:
            time.sleep(p.stagger_seconds)
    return results
'''

_MONITOR_PY = '''"""Post-launch monitoring + automated risk exits.

Polls a token's stats and triggers take-profit / stop-loss / anomaly callbacks.

The stats source is intentionally pluggable: `get_stats` hits a configurable
endpoint (`PUMP_DATA_URL`) and reads the first price-like field it finds. Point
it at your preferred indexer (Helius / Bitquery / Moralis / your own) by setting
`PUMP_DATA_URL` and, if needed, replacing the body of `get_stats` — the rest of
the monitor only depends on it returning a dict with a numeric price field.
"""
import os
import time
from typing import Callable, Optional

import requests

import pumpportal_client

DATA_URL = os.environ.get("PUMP_DATA_URL", "https://pumpportal.fun/api")
POLL_SECONDS = float(os.environ.get("MONITOR_POLL_SECONDS", "5"))
# Fields we accept as "price" from a stats provider, in priority order.
_PRICE_FIELDS = ("price", "priceUsd", "price_usd", "marketCapSol", "usd_market_cap")


def get_stats(mint: str, timeout: float = 15.0) -> dict:
    """Best-effort token stats. Shape depends on the data provider; guarded.

    Returns the provider's JSON dict, or ``{"error": "..."}`` on any failure so
    the caller never sees an exception.
    """
    try:
        resp = requests.get(f"{DATA_URL}/data/token/{mint}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"stats request failed: {e}"}
    except ValueError as e:  # non-JSON body
        return {"error": f"stats response not JSON: {e}"}


def _extract_price(stats: dict) -> Optional[float]:
    for field in _PRICE_FIELDS:
        val = stats.get(field)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return None


def watch(
    mint: str,
    entry_price: Optional[float] = None,
    take_profit_mult: float = 3.0,
    stop_loss_mult: float = 0.5,
    max_polls: int = 0,
    on_event: Optional[Callable[[str, dict], None]] = None,
    auto_sell: bool = True,
    max_consecutive_errors: int = 10,
) -> dict:
    """Watch `mint` until a threshold fires (or a stop condition is reached).

    Returns the terminal event dict — one of ``take_profit``, ``stop_loss``,
    ``timeout`` (max_polls reached), or ``error`` (the stats source failed
    ``max_consecutive_errors`` times in a row, so we stop rather than spin
    forever on a broken endpoint). If `auto_sell`, exits 100% on TP/SL via
    pumpportal_client.sell (bounded by risk_controls inside the client).
    """
    polls = 0
    consecutive_errors = 0
    baseline = entry_price
    while True:
        stats = get_stats(mint)
        if stats.get("error"):
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return {"type": "error", "reason": "stats source unavailable",
                        "detail": stats.get("error"), "polls": polls}
        else:
            consecutive_errors = 0
            price = _extract_price(stats)
            if price:
                if baseline is None:
                    baseline = price
                mult = price / baseline
                event = None
                if mult >= take_profit_mult:
                    event = {"type": "take_profit", "mult": mult, "price": price}
                elif mult <= stop_loss_mult:
                    event = {"type": "stop_loss", "mult": mult, "price": price}
                if event:
                    if on_event:
                        on_event(event["type"], event)
                    if auto_sell:
                        try:
                            event["exit"] = pumpportal_client.sell(mint, percent=100.0)
                        except Exception as e:  # surface, don't mask, the exit failure
                            event["exit_error"] = str(e)
                    return event
        polls += 1
        if max_polls and polls >= max_polls:
            return {"type": "timeout", "polls": polls, "last": stats}
        time.sleep(POLL_SECONDS)
'''

_REQUIREMENTS_TXT = """requests>=2.31
solders>=0.21
solana>=0.34
base58>=2.1
"""


def _readme(bp: dict) -> str:
    name = bp.get("name", "pump.fun agent")
    return (
        f"# {name} — Solana / pump.fun launch tools\n\n"
        "Runnable, copy-paste tools for launching and trading tokens on **pump.fun** via the\n"
        "**PumpPortal Local Transaction API** (you sign locally — keys never leave your machine).\n\n"
        "> ⚠️ **Money & risk.** These tools move real SOL on mainnet. Start with "
        "`LAUNCH_DRY_RUN=1`, keep `MAX_SPEND_SOL` small, and never commit a private key. "
        "You are responsible for legal/compliance in your jurisdiction. Bundled buys use your "
        "own wallets — do not use them to fake organic volume or harm other traders.\n\n"
        "## Setup\n\n"
        "```bash\npip install -r requirements.txt\n"
        "export SOLANA_PRIVATE_KEY=<base58 secret key>     # dev wallet\n"
        "export SOLANA_RPC_URL=https://your-rpc            # a private RPC is strongly recommended\n"
        "export LAUNCH_DRY_RUN=1                            # simulate first!\n"
        "export MAX_SPEND_SOL=1.0 MAX_SLIPPAGE_PCT=25\n```\n\n"
        "## Files\n\n"
        "- `risk_controls.py` — hard caps, dry-run, kill-switch (every action goes through `preflight()`)\n"
        "- `wallet.py` — load keypair, balances, local signing\n"
        "- `ipfs_metadata.py` — pin image + metadata, get a `metadataUri`\n"
        "- `pumpportal_client.py` — `create_token` / `buy` / `sell`\n"
        "- `launch_strategy.py` — timing, dev-buy sizing, multi-wallet bundled opening buys\n"
        "- `monitor.py` — post-launch watch with take-profit / stop-loss / kill-switch\n"
        "- `tools.json` — machine-readable tool schemas for agent tool-calling\n"
        "- `example_launch.py` — end-to-end launch example\n\n"
        "## Quickstart\n\n"
        "```bash\nLAUNCH_DRY_RUN=1 python example_launch.py\n```\n\n"
        "Wire these functions to your agent's tool-calling layer using `tools.json`. Each function name "
        "in that file maps 1:1 to a function in the modules above.\n"
    )


_EXAMPLE_LAUNCH_PY = '''"""End-to-end pump.fun launch example. Runs in DRY_RUN by default.

    LAUNCH_DRY_RUN=1 python example_launch.py
"""
import os

import ipfs_metadata
import pumpportal_client
import launch_strategy
import risk_controls


def main() -> None:
    if not risk_controls.dry_run():
        print("LIVE MODE — this will spend real SOL. Ctrl-C now to abort.")

    # 1) Metadata (skipped in dry-run if no image is present).
    image = os.environ.get("TOKEN_IMAGE", "logo.png")
    if os.path.exists(image):
        uri = ipfs_metadata.upload(image, "Phantom Cat", "PCAT", "the first phantom cat")
    else:
        uri = "https://example.com/metadata.json"  # placeholder for dry-run
        print("no image found; using placeholder metadata URI")

    # 2) Plan the opening (dev buy + optional bundled buys).
    plan = launch_strategy.plan(dev_buy_sol=0.5, bundle_wallets=0, slippage_pct=10)
    print("plan:", plan)

    # 3) Create the token (dev buy included).
    result = pumpportal_client.create_token(
        name="Phantom Cat", symbol="PCAT", uri=uri, dev_buy_sol=plan.dev_buy_sol,
        slippage_pct=plan.slippage_pct,
    )
    print("created:", result)

    # 4) Fire bundled opening buys, if any.
    if plan.bundle_wallets:
        print("bundle:", launch_strategy.execute_bundle(plan, result["mint"]))


if __name__ == "__main__":
    main()
'''


def _tools_json(bp: dict) -> str:
    """Machine-readable tool schemas the generated agent can tool-call against.

    Kept close to the OpenAI/Anthropic function-tool shape so it drops into most
    agent frameworks with a thin adapter.
    """
    tools = [
        {
            "name": "upload_metadata",
            "module": "ipfs_metadata",
            "function": "upload",
            "description": "Pin token image + metadata to IPFS; returns a metadataUri.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string"},
                    "name": {"type": "string"},
                    "symbol": {"type": "string"},
                    "description": {"type": "string"},
                    "twitter": {"type": "string"},
                    "telegram": {"type": "string"},
                    "website": {"type": "string"},
                },
                "required": ["image_path", "name", "symbol"],
            },
        },
        {
            "name": "create_token",
            "module": "pumpportal_client",
            "function": "create_token",
            "description": "Create a new pump.fun token with an optional dev buy. Returns {mint, signature}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "symbol": {"type": "string"},
                    "uri": {"type": "string", "description": "metadataUri from upload_metadata"},
                    "dev_buy_sol": {"type": "number", "default": 0.0},
                    "slippage_pct": {"type": "number", "default": 10},
                },
                "required": ["name", "symbol", "uri"],
            },
        },
        {
            "name": "buy",
            "module": "pumpportal_client",
            "function": "buy",
            "description": "Buy amount_sol worth of a mint. Returns {signature}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string"},
                    "amount_sol": {"type": "number"},
                    "slippage_pct": {"type": "number", "default": 10},
                },
                "required": ["mint", "amount_sol"],
            },
        },
        {
            "name": "sell",
            "module": "pumpportal_client",
            "function": "sell",
            "description": "Sell a percent of held mint. Returns {signature}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string"},
                    "percent": {"type": "number", "default": 100},
                    "slippage_pct": {"type": "number", "default": 10},
                },
                "required": ["mint"],
            },
        },
        {
            "name": "plan_launch",
            "module": "launch_strategy",
            "function": "plan",
            "description": "Build and risk-check a launch plan (dev buy + bundled opening buys).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dev_buy_sol": {"type": "number", "default": 0.5},
                    "bundle_wallets": {"type": "integer", "default": 0},
                    "per_wallet_sol": {"type": "number", "default": 0.1},
                    "slippage_pct": {"type": "number", "default": 10},
                },
            },
        },
        {
            "name": "watch",
            "module": "monitor",
            "function": "watch",
            "description": "Watch a mint and auto-exit on take-profit / stop-loss.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string"},
                    "take_profit_mult": {"type": "number", "default": 3.0},
                    "stop_loss_mult": {"type": "number", "default": 0.5},
                },
                "required": ["mint"],
            },
        },
        {
            "name": "preflight",
            "module": "risk_controls",
            "function": "preflight",
            "description": "Validate an action against spend/slippage/kill-switch limits before spending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "amount_sol": {"type": "number"},
                    "slippage_pct": {"type": "number", "default": 0},
                },
                "required": ["action", "amount_sol"],
            },
        },
    ]
    return json.dumps({"bundle": bp.get("name"), "tools": tools}, indent=2) + "\n"


def build_solana_launch(bp: dict) -> dict:
    """Target builder: emit the runnable pump.fun tool suite under targets/solana-launch/."""
    base = "targets/solana-launch"
    return {
        f"{base}/risk_controls.py": _RISK_CONTROLS_PY,
        f"{base}/wallet.py": _WALLET_PY,
        f"{base}/ipfs_metadata.py": _IPFS_METADATA_PY,
        f"{base}/pumpportal_client.py": _PUMPPORTAL_CLIENT_PY,
        f"{base}/launch_strategy.py": _LAUNCH_STRATEGY_PY,
        f"{base}/monitor.py": _MONITOR_PY,
        f"{base}/example_launch.py": _EXAMPLE_LAUNCH_PY,
        f"{base}/tools.json": _tools_json(bp),
        f"{base}/requirements.txt": _REQUIREMENTS_TXT,
        f"{base}/README.md": _readme(bp),
    }
