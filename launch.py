"""pump.fun launch — the swarm mints its coin on-chain.

Flow:
  1. Upload metadata + logo to pump.fun's IPFS endpoint  -> metadata URI
  2. Ask PumpPortal's local API to build the `create` transaction (with an
     optional dev buy) for a fresh mint keypair
  3. Sign locally with [mint keypair, wallet keypair] and broadcast

When PUMP_LIVE is not "true" this runs as a dry-run: it still uploads metadata
(harmless) but returns the launch *plan* instead of broadcasting, so you can
verify the whole pipeline without spending SOL.
"""

import httpx

import wallet

PUMP_IPFS_URL = "https://pump.fun/api/ipfs"
PUMPPORTAL_LOCAL_URL = "https://pumpportal.fun/api/trade-local"


async def upload_metadata(
    name: str,
    symbol: str,
    description: str,
    image_bytes: bytes | None,
    twitter: str = "",
    telegram: str = "",
    website: str = "",
) -> dict:
    """Upload token metadata + image to pump.fun IPFS. Returns { uri, error }."""
    form = {
        "name": (None, name),
        "symbol": (None, symbol),
        "description": (None, description),
        "twitter": (None, twitter),
        "telegram": (None, telegram),
        "website": (None, website),
        "showName": (None, "true"),
    }
    if image_bytes:
        form["file"] = ("logo.png", image_bytes, "image/png")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(PUMP_IPFS_URL, files=form)
        if resp.status_code != 200:
            return {"uri": None, "error": f"ipfs {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        return {"uri": data.get("metadataUri"), "metadata": data.get("metadata"), "error": None}
    except Exception as e:
        return {"uri": None, "error": str(e)[:200]}


async def launch_token(
    name: str,
    symbol: str,
    description: str,
    image_bytes: bytes | None = None,
    dev_buy_sol: float = 0.0,
    twitter: str = "",
    telegram: str = "",
    website: str = "",
    slippage: int = 10,
    priority_fee: float = 0.0005,
) -> dict:
    """Launch a token on pump.fun.

    Returns {
        mint, signature, metadata_uri, pump_url, dev_buy_sol, live, error
    }.
    """
    if not wallet.is_configured():
        return {"error": "PHANTOM_WALLET_PRIVATE_KEY not configured", "live": False}

    # Enforce the dev-buy spend cap regardless of what the swarm asked for.
    dev_buy_sol = max(0.0, min(dev_buy_sol, wallet.MAX_DEV_BUY_SOL))

    # 1. metadata -> IPFS
    meta = await upload_metadata(name, symbol, description, image_bytes, twitter, telegram, website)
    if meta.get("error"):
        return {"error": f"metadata upload failed: {meta['error']}", "live": False}
    metadata_uri = meta["uri"]

    # 2. fresh mint keypair
    from solders.keypair import Keypair

    mint_kp = Keypair()
    mint_str = str(mint_kp.pubkey())
    pump_url = f"https://pump.fun/{mint_str}"

    plan = {
        "mint": mint_str,
        "metadata_uri": metadata_uri,
        "dev_buy_sol": dev_buy_sol,
        "pump_url": pump_url,
    }

    # Dry-run: prove the pipeline without broadcasting.
    if not wallet.PUMP_LIVE:
        return {**plan, "signature": None, "live": False, "error": None,
                "note": "PUMP_LIVE!=true — dry run, no transaction broadcast"}

    # 3. build the create transaction via PumpPortal (local, unsigned)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                PUMPPORTAL_LOCAL_URL,
                json={
                    "publicKey": wallet.wallet_pubkey(),
                    "action": "create",
                    "tokenMetadata": {"name": name, "symbol": symbol, "uri": metadata_uri},
                    "mint": mint_str,
                    "denominatedInSol": "true",
                    "amount": dev_buy_sol,
                    "slippage": slippage,
                    "priorityFee": priority_fee,
                    "pool": "pump",
                },
            )
        if resp.status_code != 200:
            return {**plan, "signature": None, "live": True,
                    "error": f"pumpportal {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {**plan, "signature": None, "live": True, "error": str(e)[:200]}

    # 4. sign (mint keypair first, then wallet) + broadcast
    try:
        signed = wallet.sign_transaction(resp.content, extra_signers=[mint_kp])
    except Exception as e:
        return {**plan, "signature": None, "live": True, "error": f"signing failed: {str(e)[:200]}"}

    result = await wallet.broadcast(signed)
    return {
        **plan,
        "signature": result.get("signature"),
        "solscan": f"https://solscan.io/tx/{result['signature']}" if result.get("signature") else None,
        "live": True,
        "error": result.get("error"),
    }
