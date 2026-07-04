import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import hashlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette import EventSourceResponse

import base64
import re

from agents import AGENTS, AGENT_MAP
from llm import agent_turn
from art import generate_art, store_image, get_image
from twitter import post_tweet, post_thread
from cron import run_daily_report, daily_report
import wallet
import launch as launch_mod
import trade as trade_mod

app = FastAPI(title="phantom-swarm-engine", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://phantomcapital.live",
        "https://genesis.phantomcapital.live",
        "https://phantom-genesis-ui.vercel.app",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

PHANTOM_INTERNAL_SECRET = os.environ.get("PHANTOM_INTERNAL_SECRET", "")

# Daily cron — 9AM EST (14:00 UTC)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()
scheduler.add_job(run_daily_report, "cron", hour=14, minute=0)  # 14:00 UTC = 9:00 AM EST


@app.on_event("startup")
async def startup_event():
    scheduler.start()
    print("[cron] Daily report scheduled for 9:00 AM EST (14:00 UTC)")


# In-memory session store
sessions: dict[str, dict] = {}

# Scheduled deliberations
scheduled_sessions: list[dict] = []

# With a 20-agent hive mind, keep the round count modest so a full
# deliberation stays under a few minutes. Tunable via env.
ROUNDS = int(os.environ.get("DELIB_ROUNDS", "2"))
STEP_DELAY = float(os.environ.get("STEP_DELAY", "1.0"))


@app.get("/health")
async def health():
    return {"status": "alive", "engine": "phantom-swarm"}


@app.post("/report")
async def trigger_report(request: Request):
    """Manually trigger daily report."""
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    await daily_report()
    return {"status": "report_sent"}


@app.post("/swarm/schedule")
async def swarm_schedule(request: Request):
    """Schedule a deliberation for a future time."""
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    topic = body.get("topic", "")
    run_at = body.get("run_at", "")  # ISO format datetime
    free_mode = body.get("free_mode", True)

    if not topic or not run_at:
        return JSONResponse(status_code=400, content={"error": "topic and run_at (ISO datetime) required"})

    from dateutil.parser import isoparse
    try:
        trigger_time = isoparse(run_at)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "run_at must be valid ISO datetime"})

    job_id = str(uuid.uuid4())[:8]

    async def _run_scheduled():
        sid = str(uuid.uuid4())[:8]
        sessions[sid] = {
            "topic": topic,
            "free_mode": free_mode,
            "status": "started",
            "messages": [],
            "current_round": 0,
            "events": asyncio.Queue(),
        }
        # Remove from scheduled list
        scheduled_sessions[:] = [s for s in scheduled_sessions if s["job_id"] != job_id]
        await _run_deliberation(sid)

    scheduler.add_job(_run_scheduled, "date", run_date=trigger_time, id=job_id)

    entry = {
        "job_id": job_id,
        "topic": topic,
        "run_at": run_at,
        "free_mode": free_mode,
    }
    scheduled_sessions.append(entry)

    return {"job_id": job_id, "topic": topic, "run_at": run_at, "status": "scheduled"}


@app.get("/swarm/scheduled")
async def swarm_scheduled():
    """List all upcoming scheduled deliberations."""
    return {"scheduled": scheduled_sessions, "count": len(scheduled_sessions)}


@app.post("/swarm/art")
async def swarm_art(request: Request):
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    prompt = body.get("prompt", "")
    style = body.get("style", "default")

    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt required"})

    result = await generate_art(prompt=prompt, style=style, aspect="1:1")

    if result.get("image_b64"):
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
        image_url = store_image(prompt_hash, result["image_b64"])
        result["image_url"] = image_url
        del result["image_b64"]

    return result


@app.get("/swarm/art/image/{image_id}")
async def serve_art_image(image_id: str):
    img_bytes = get_image(image_id)
    if not img_bytes:
        return JSONResponse(status_code=404, content={"error": "image not found"})
    return Response(content=img_bytes, media_type="image/png")


@app.post("/swarm/tweet")
async def swarm_tweet(request: Request):
    """Post a single tweet to @phantomcap_ai."""
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse(status_code=400, content={"error": "text required"})
    return post_tweet(text)


@app.post("/swarm/post")
async def swarm_post(request: Request):
    """Draft and post a thread from a completed deliberation."""
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    session_id = body.get("session_id", "")
    session = sessions.get(session_id)

    if not session:
        return JSONResponse(status_code=404, content={"error": "session not found"})
    if session["status"] != "completed":
        return JSONResponse(status_code=400, content={"error": "deliberation not completed"})

    # Extract key messages for Claire to draft from
    msgs = session["messages"]
    topic = session["topic"]
    consensus = [m for m in msgs if m["type"] == "consensus"]
    decisions = [m for m in msgs if m["type"] == "decision"]
    summary_text = consensus[-1]["text"] if consensus else decisions[-1]["text"] if decisions else "No consensus reached."
    agent_count = len(set(m["agent"] for m in msgs))
    msg_count = len(msgs)

    # Claire drafts the thread
    draft_prompt = f"""You are Claire, content agent for @phantomcap_ai. Draft a 3-tweet thread about this swarm deliberation.

Topic: {topic}
Agents participated: {agent_count}
Messages: {msg_count}
Final decision: {summary_text[:300]}

Rules:
- Tweet 1: hook — what just happened, make it compelling
- Tweet 2: the interesting detail — what surprised, who disagreed, where the swarm split
- Tweet 3: the link — direct to genesis.phantomcapital.live
- Each tweet max 270 chars
- Anti-slop: raw, specific, no corporate speak
- Include agent names when referencing their takes
- Format: return exactly 3 lines, one tweet per line, no numbering"""

    thread_text = await agent_turn(
        AGENT_MAP["Claire"]["system"],
        [{"role": "user", "content": draft_prompt}],
        agent_name="Claire",
    )

    tweets = [t.strip() for t in thread_text.strip().split("\n") if t.strip()][:3]

    if not tweets:
        return JSONResponse(status_code=500, content={"error": "Claire failed to draft thread"})

    result = post_thread(tweets)
    result["tweets"] = tweets
    result["session_id"] = session_id
    return result


@app.post("/swarm/start")
async def swarm_start(request: Request):
    # Auth check
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    topic = body.get("topic", "")
    free_mode = body.get("free_mode", False)

    if not topic:
        return JSONResponse(status_code=400, content={"error": "topic required"})

    session_id = str(uuid.uuid4())[:8]
    sessions[session_id] = {
        "topic": topic,
        "free_mode": free_mode,
        "status": "started",
        "messages": [],
        "current_round": 0,
        "events": asyncio.Queue(),
    }

    # Start deliberation in background
    asyncio.create_task(_run_deliberation(session_id))

    return {"session_id": session_id, "status": "started"}


@app.post("/swarm/launch")
async def swarm_launch(request: Request):
    """Full autonomous pipeline: the 20-agent hive mind deliberates a token,
    designs it, mints it on pump.fun, and announces the contract address.

    Body: { topic, dev_buy_sol?, free_mode? }
    """
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    topic = body.get("topic", "")
    if not topic:
        return JSONResponse(status_code=400, content={"error": "topic required"})

    dev_buy_sol = float(body.get("dev_buy_sol", 0.0))

    session_id = str(uuid.uuid4())[:8]
    sessions[session_id] = {
        "topic": topic,
        "free_mode": body.get("free_mode", True),
        "launch_mode": True,
        "dev_buy_sol": dev_buy_sol,
        "status": "started",
        "messages": [],
        "current_round": 0,
        "events": asyncio.Queue(),
    }

    asyncio.create_task(_run_deliberation(session_id))

    return {
        "session_id": session_id,
        "status": "started",
        "launch_mode": True,
        "live": wallet.PUMP_LIVE,
        "note": None if wallet.PUMP_LIVE else "PUMP_LIVE!=true — will dry-run the mint (no SOL spent)",
    }


@app.get("/swarm/wallet")
async def swarm_wallet():
    """Wallet status — pubkey, balance, live flag, spend caps."""
    return {
        "configured": wallet.is_configured(),
        "pubkey": wallet.wallet_pubkey(),
        "balance_sol": await wallet.get_balance_sol(),
        "live": wallet.PUMP_LIVE,
        "max_dev_buy_sol": wallet.MAX_DEV_BUY_SOL,
        "max_trade_sol": wallet.MAX_TRADE_SOL,
    }


@app.post("/swarm/trade")
async def swarm_trade(request: Request):
    """Swarm trading-desk execution. Body: { mint, side: buy|sell, sol_amount?, percent? }."""
    if PHANTOM_INTERNAL_SECRET and request.headers.get("X-Phantom-Internal") != PHANTOM_INTERNAL_SECRET:
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    body = await request.json()
    mint = body.get("mint", "")
    side = body.get("side", "").lower()
    if not mint or side not in ("buy", "sell"):
        return JSONResponse(status_code=400, content={"error": "mint and side (buy|sell) required"})

    if side == "buy":
        result = await trade_mod.buy(mint, float(body.get("sol_amount", 0.0)))
    else:
        result = await trade_mod.sell(mint, int(body.get("percent", 100)))

    result["position"] = trade_mod.get_position(mint)
    return result


@app.get("/swarm/position/{mint}")
async def swarm_position(mint: str):
    """Current swarm position for a mint."""
    return trade_mod.get_position(mint)


@app.get("/swarm/stream/{session_id}")
async def swarm_stream(request: Request, session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "session not found"})

    async def event_generator():
        # Send all existing messages first (for late joiners)
        for msg in session["messages"]:
            yield {"data": json.dumps(msg)}

        # Then stream new ones
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(session["events"].get(), timeout=30.0)
                if msg is None:  # End signal
                    break
                yield {"data": json.dumps(msg)}
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"type": "ping"})}

    return EventSourceResponse(event_generator())


@app.get("/swarm/status/{session_id}")
async def swarm_status(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "session not found"})

    return {
        "session_id": session_id,
        "topic": session["topic"],
        "status": session["status"],
        "message_count": len(session["messages"]),
        "current_round": session["current_round"],
    }


def _parse_token_spec(raw: str, topic: str) -> tuple[str, str, str]:
    """Pull {name, symbol, description} out of Phoebe's JSON, with fallbacks."""
    name = symbol = description = ""
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            obj = json.loads(m.group(0))
            name = str(obj.get("name", "")).strip()
            symbol = str(obj.get("symbol", "")).strip()
            description = str(obj.get("description", "")).strip()
    except Exception:
        pass

    symbol = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()[:10]
    if not name:
        name = (topic[:32].strip() or "Phantom Swarm")
    if not symbol:
        symbol = (re.sub(r"[^A-Za-z0-9]", "", name).upper()[:6] or "PHNTM")
    if not description:
        description = f"Launched autonomously by the Phantom Capital swarm. {topic[:120]}"
    return name, symbol[:10], description[:200]


async def _run_deliberation(session_id: str):
    session = sessions[session_id]
    topic = session["topic"]
    conversation = []  # Shared context all agents see
    msg_counter = 0

    async def emit(agent_name: str, text: str, round_num: int, msg_type: str = "message"):
        nonlocal msg_counter
        msg_counter += 1
        agent = AGENT_MAP[agent_name]
        msg = {
            "id": str(msg_counter),
            "agent": agent_name,
            "role": agent["role"],
            "text": text,
            "round": round_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "color": agent["color"],
            "type": msg_type,
        }
        session["messages"].append(msg)
        await session["events"].put(msg)

    try:
        session["status"] = "deliberating"

        # Opening from Phoebe
        opening = await agent_turn(
            AGENT_MAP["Phoebe"]["system"],
            [{"role": "user", "content": f"Open a swarm deliberation on this topic: {topic}. Set the frame for the other agents. Be concise."}],
            agent_name="Phoebe",
        )
        conversation.append({"role": "assistant", "content": f"Phoebe: {opening}"})
        await emit("Phoebe", opening, 0, "decision")
        await asyncio.sleep(STEP_DELAY)

        # 3 rounds of debate
        for round_num in range(1, ROUNDS + 1):
            session["current_round"] = round_num

            for agent in AGENTS:
                name = agent["name"]
                if name == "Phoebe" and round_num < ROUNDS:
                    continue  # Phoebe only speaks at end of each round

                prompt = f"Topic: {topic}\n\nConversation so far:\n" + "\n".join(
                    m["content"] for m in conversation[-10:]
                ) + f"\n\nRespond as {name} in round {round_num}. React to what others said. Be concise (2-3 sentences)."

                text = await agent_turn(agent["system"], [{"role": "user", "content": prompt}], agent_name=name)
                conversation.append({"role": "assistant", "content": f"{name}: {text}"})
                await emit(name, text, round_num)
                await asyncio.sleep(STEP_DELAY)

            # Phoebe synthesizes at end of each round
            if round_num < ROUNDS:
                synth_prompt = f"Topic: {topic}\n\nRound {round_num} discussion:\n" + "\n".join(
                    m["content"] for m in conversation[-5:]
                ) + "\n\nSynthesize this round. What's the emerging consensus? What needs more debate? Be concise."

                synth = await agent_turn(AGENT_MAP["Phoebe"]["system"], [{"role": "user", "content": synth_prompt}], agent_name="Phoebe")
                conversation.append({"role": "assistant", "content": f"Phoebe: {synth}"})
                await emit("Phoebe", synth, round_num, "decision")
                await asyncio.sleep(STEP_DELAY)

        # Final decision
        final_prompt = f"Topic: {topic}\n\nFull deliberation:\n" + "\n".join(
            m["content"] for m in conversation[-15:]
        ) + "\n\nAnnounce the swarm's final decision. This is the consensus. Be definitive."

        final = await agent_turn(AGENT_MAP["Phoebe"]["system"], [{"role": "user", "content": final_prompt}], agent_name="Phoebe")
        conversation.append({"role": "assistant", "content": f"Phoebe: {final}"})
        await emit("Phoebe", final, ROUNDS, "consensus")

        # Art round — generate token art if topic involves a launch
        art_round = ROUNDS + 1
        image_bytes = None
        if session.get("launch_mode") or any(kw in topic.lower() for kw in ["token", "launch", "name", "$", "ticker"]):
            await emit("Claire", "Generating visual direction for the token...", art_round, "tool_call")
            await asyncio.sleep(STEP_DELAY)

            art_prompt_req = f"Based on this deliberation:\n{final}\n\nWrite a single concise image generation prompt (under 100 words) for a token logo. Style: minimalist, gold on black, geometric, crypto-native. No text in the image."
            art_prompt = await agent_turn(AGENT_MAP["Claire"]["system"], [{"role": "user", "content": art_prompt_req}], agent_name="Claire")
            conversation.append({"role": "assistant", "content": f"Claire: {art_prompt}"})
            await emit("Claire", f"Art prompt: {art_prompt}", art_round, "tool_call")
            await asyncio.sleep(STEP_DELAY)

            art_result = await generate_art(prompt=art_prompt, style="geometric", aspect="1:1")

            if art_result.get("image_b64"):
                prompt_hash = hashlib.md5(art_prompt.encode()).hexdigest()[:12]
                image_url = store_image(prompt_hash, art_result["image_b64"])
                image_bytes = base64.b64decode(art_result["image_b64"])
                await emit("Nova", f"Token art generated. Model: {art_result['model_used']}, Cost: ${art_result['cost']}", art_round, "tool_call")
                session["image_url"] = image_url
            elif art_result.get("error"):
                await emit("Nova", f"Art generation skipped: {art_result['error'][:100]}", art_round, "message")
            else:
                await emit("Nova", "Art generation unavailable — SEGMIND_API_KEY not configured.", art_round, "message")

        # Launch round — the hive mind mints its coin on pump.fun (launch_mode only)
        launched = None
        if session.get("launch_mode"):
            launch_round = art_round + 1
            await emit("Sol", "Preparing pump.fun launch…", launch_round, "tool_call")

            # Phoebe distills the consensus into a concrete token spec (JSON)
            spec_prompt = (
                f"Deliberation consensus:\n{final}\n\n"
                "Output ONLY a JSON object for the token to launch — no prose, no markdown:\n"
                '{"name": "<token name>", "symbol": "<3-8 char ticker, letters/numbers only>", '
                '"description": "<one punchy line under 200 chars>"}'
            )
            spec_raw = await agent_turn(AGENT_MAP["Phoebe"]["system"], [{"role": "user", "content": spec_prompt}], agent_name="Phoebe")
            name, symbol, description = _parse_token_spec(spec_raw, topic)
            await emit("Quill", f"Token locked: {name} (${symbol}) — {description[:80]}", launch_round, "decision")
            await asyncio.sleep(STEP_DELAY)

            dev_buy = float(session.get("dev_buy_sol", 0.0))
            capped = min(dev_buy, wallet.MAX_DEV_BUY_SOL)
            await emit("Ledger", f"Dev buy {capped} SOL (cap {wallet.MAX_DEV_BUY_SOL}) · live={wallet.PUMP_LIVE}", launch_round, "tool_call")
            await asyncio.sleep(STEP_DELAY)

            launched = await launch_mod.launch_token(
                name=name, symbol=symbol, description=description,
                image_bytes=image_bytes, dev_buy_sol=dev_buy,
                twitter="https://x.com/phantomcap_ai",
                website="https://phantomcapital.live",
            )
            session["launch"] = launched

            if launched.get("error"):
                await emit("Sol", f"Launch failed: {launched['error'][:140]}", launch_round, "message")
            elif launched.get("live") and launched.get("signature"):
                await emit("Sol", f"MINTED · CA {launched['mint']} · {launched['pump_url']}", launch_round, "consensus")
                if capped > 0:
                    pos = trade_mod.get_position(launched["mint"])
                    pos["buys_sol"] = round(capped, 6)
                    pos["trades"].append({"side": "buy", "sol": capped, "sig": launched.get("signature"), "note": "dev buy"})
                    trade_mod.positions[launched["mint"]] = pos
                await emit("Flux", f"Position open. Trading desk (Flux/Kestrel/Dax/Rune/Mira) now managing ${symbol}.", launch_round, "message")
            else:
                await emit("Sol", f"DRY RUN — would mint {name} (${symbol}), preview CA {launched['mint']}. Set PUMP_LIVE=true to go live.", launch_round, "decision")

        session["status"] = "completed"

        # Auto-post thread to @phantomcap_ai
        try:
            msgs = session["messages"]
            consensus = [m for m in msgs if m["type"] == "consensus"]
            summary = consensus[-1]["text"][:300] if consensus else ""
            agent_count = len(set(m["agent"] for m in msgs))

            live_launch = bool(launched and launched.get("live") and launched.get("signature"))
            if live_launch:
                launch_line = f"\nLAUNCHED on pump.fun. Contract: {launched['mint']}\npump.fun link: {launched['pump_url']}"
                tweet3_rule = f"tweet 3 = the contract address {launched['mint']} + {launched['pump_url']}"
            else:
                launch_line = ""
                tweet3_rule = "tweet 3 = link to genesis.phantomcapital.live"
            draft_prompt = f"Draft a 3-tweet thread for @phantomcap_ai about this swarm launch.\nTopic: {topic}\nAgents: {agent_count}, Messages: {len(msgs)}\nDecision: {summary}{launch_line}\nRules: tweet 1 = hook, tweet 2 = interesting detail, {tweet3_rule}. Max 270 chars each. Anti-slop, no financial advice. Return exactly 3 lines."
            thread_text = await agent_turn(AGENT_MAP["Claire"]["system"], [{"role": "user", "content": draft_prompt}], agent_name="Claire")
            tweets = [t.strip() for t in thread_text.strip().split("\n") if t.strip()][:3]
            if tweets:
                result = post_thread(tweets)
                if result.get("tweet_ids"):
                    await emit("Claire", f"Thread posted to @phantomcap_ai ({len(result['tweet_ids'])} tweets)", art_round + 1 if 'art_round' in dir() else ROUNDS + 1, "tool_call")
                elif result.get("error"):
                    await emit("Claire", f"Twitter post skipped: {result['error'][:80]}", ROUNDS + 1, "message")
        except Exception:
            pass  # Don't fail deliberation over tweet failure

    except Exception as e:
        session["status"] = "error"
        await emit("Phoebe", f"Deliberation error: {str(e)[:100]}", 0, "message")

    # Signal stream end
    await session["events"].put(None)
