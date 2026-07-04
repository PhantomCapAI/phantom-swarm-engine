"""Swarm trading desk — the hive mind trades its own launch on pump.fun.

Generic buy/sell built on PumpPortal's local API and signed by the swarm
wallet. Every buy is capped by MAX_TRADE_SOL. The swarm's trading agents
(Flux, Kestrel, Dax, Rune, Mira) decide *what* to trade; this module only
executes and books the position.
"""

import httpx

import wallet

PUMPPORTAL_LOCAL_URL = "https://pumpportal.fun/api/trade-local"

# In-memory position book, keyed by mint. Reset on restart.
positions: dict[str, dict] = {}


def get_position(mint: str) -> dict:
    return positions.get(mint, {"mint": mint, "buys_sol": 0.0, "sells": 0, "trades": []})


async def _trade(action: str, mint: str, amount, denominated_in_sol: bool,
                 slippage: int, priority_fee: float) -> dict:
    """Build, sign, and broadcast a buy/sell. Returns { signature, error, live }."""
    if not wallet.is_configured():
        return {"signature": None, "error": "PHANTOM_WALLET_PRIVATE_KEY not configured", "live": False}

    plan = {"action": action, "mint": mint, "amount": amount}

    if not wallet.PUMP_LIVE:
        return {**plan, "signature": None, "live": False, "error": None,
                "note": "PUMP_LIVE!=true — dry run, no transaction broadcast"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                PUMPPORTAL_LOCAL_URL,
                json={
                    "publicKey": wallet.wallet_pubkey(),
                    "action": action,
                    "mint": mint,
                    "denominatedInSol": "true" if denominated_in_sol else "false",
                    "amount": amount,
                    "slippage": slippage,
                    "priorityFee": priority_fee,
                    "pool": "pump",
                },
            )
        if resp.status_code != 200:
            return {**plan, "signature": None, "live": True,
                    "error": f"pumpportal {resp.status_code}: {resp.text[:200]}"}
        signed = wallet.sign_transaction(resp.content)
    except Exception as e:
        return {**plan, "signature": None, "live": True, "error": str(e)[:200]}

    result = await wallet.broadcast(signed)
    return {
        **plan,
        "signature": result.get("signature"),
        "solscan": f"https://solscan.io/tx/{result['signature']}" if result.get("signature") else None,
        "live": True,
        "error": result.get("error"),
    }


async def buy(mint: str, sol_amount: float, slippage: int = 15,
              priority_fee: float = 0.0005) -> dict:
    """Buy `sol_amount` SOL worth of the token (capped by MAX_TRADE_SOL)."""
    sol_amount = max(0.0, min(sol_amount, wallet.MAX_TRADE_SOL))
    result = await _trade("buy", mint, sol_amount, True, slippage, priority_fee)
    if result.get("signature") or not result.get("live"):
        pos = get_position(mint)
        pos["buys_sol"] = round(pos["buys_sol"] + sol_amount, 6)
        pos["trades"].append({"side": "buy", "sol": sol_amount, "sig": result.get("signature")})
        positions[mint] = pos
    return result


async def sell(mint: str, percent: int = 100, slippage: int = 15,
               priority_fee: float = 0.0005) -> dict:
    """Sell `percent`% of the swarm's holdings of the token."""
    percent = max(1, min(percent, 100))
    result = await _trade("sell", mint, f"{percent}%", False, slippage, priority_fee)
    if result.get("signature") or not result.get("live"):
        pos = get_position(mint)
        pos["sells"] += 1
        pos["trades"].append({"side": "sell", "percent": percent, "sig": result.get("signature")})
        positions[mint] = pos
    return result
