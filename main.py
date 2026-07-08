import envload  # noqa: F401  — loads .env before anything reads config

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette import EventSourceResponse

import config
from logging_setup import configure_logging, get_logger, new_request_id, request_id_var
import security

# Configure logging before anything else logs.
log = configure_logging()

from agents import AGENTS, AGENT_MAP, CREW
from llm import agent_turn, provider_name, configured, check_llm, LLMError
from art import generate_art, store_image, get_image
from twitter import post_tweet, post_thread
from cron import run_daily_report, daily_report
from bundler import run_bundle, estimate_bundle_cost, BUNDLER_VERSION, DEFAULT_TARGETS
from ui import BUNDLER_UI
import store
import crypto_payments

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --------------------------------------------------------------------------- #
# Optional rate limiting (slowapi). Degrades to a no-op if not installed so the
# service still boots — but requirements.txt pins it for production.
# --------------------------------------------------------------------------- #
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded

    _HAS_SLOWAPI = True
    limiter = Limiter(key_func=get_remote_address, default_limits=[config.RATE_LIMIT_DEFAULT])
except ImportError:  # pragma: no cover - production installs slowapi
    _HAS_SLOWAPI = False

    class _NoopLimiter:
        def limit(self, *_a, **_k):
            def deco(fn):
                return fn

            return deco

    limiter = _NoopLimiter()
    log.warning("slowapi not installed; rate limiting disabled")


PHANTOM_INTERNAL_SECRET = os.environ.get("PHANTOM_INTERNAL_SECRET", "")

# In-memory stores.
sessions: dict[str, dict] = {}
scheduled_sessions: list[dict] = []
# Background tasks tracked for graceful shutdown.
_background_tasks: set[asyncio.Task] = set()

scheduler = AsyncIOScheduler()


def _track(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    report = config.validate_environment()
    for w in report.warnings:
        log.warning("config: %s", w)
    for e in report.errors:
        log.error("config: %s", e)
    if not report.ok:
        log.error("Startup config has errors; some features will be unavailable.")

    scheduler.add_job(run_daily_report, "cron", hour=14, minute=0)  # 14:00 UTC = 9AM EST
    scheduler.start()
    log.info(
        "phantom-swarm-engine up — provider=%s configured=%s storage=%s rate_limit=%s",
        provider_name(), configured(), store.backend(), _HAS_SLOWAPI,
    )
    yield
    # --- shutdown ---
    log.info("shutting down: cancelling %d background task(s)", len(_background_tasks))
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    scheduler.shutdown(wait=False)
    log.info("shutdown complete")


app = FastAPI(title="phantom-swarm-engine", docs_url=None, redoc_url=None, lifespan=lifespan)

if _HAS_SLOWAPI:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Assign a request id (honoring an inbound X-Request-ID), bind it for logs,
    time the request, and echo the id back on the response."""
    rid = request.headers.get("X-Request-ID") or new_request_id()
    token = request_id_var.set(rid)
    start = datetime.now(timezone.utc)
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(status_code=500, content={"error": "internal error", "request_id": rid})
    finally:
        request_id_var.reset(token)
    elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    response.headers["X-Request-ID"] = rid
    log.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def _internal_ok(request: Request) -> bool:
    """True when the internal secret is unset (open) or the header matches."""
    if not PHANTOM_INTERNAL_SECRET:
        return True
    return request.headers.get("X-Phantom-Internal") == PHANTOM_INTERNAL_SECRET


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "unauthorized"})


def _active_gate():
    """The active payment gate (crypto), or None when no paywall is configured."""
    return crypto_payments if crypto_payments.enabled() else None


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health(deep: bool = False):
    """Liveness + config. ``?deep=1`` also probes real LLM connectivity."""
    body = {
        "status": "alive",
        "engine": "phantom-swarm",
        "crew_size": len(CREW),
        "llm_provider": provider_name(),
        "llm_configured": configured(),
        "storage": store.backend(),
        "paywall": crypto_payments.enabled(),
        "bundler_version": BUNDLER_VERSION,
    }
    if deep:
        body["llm"] = await check_llm()
        body["status"] = "alive" if body["llm"].get("ok") else "degraded"
    return body


@app.get("/health/llm")
async def health_llm():
    """Dedicated deep health check: performs a real (tiny) LLM completion."""
    result = await check_llm()
    return JSONResponse(status_code=200 if result.get("ok") else 503, content=result)


# --------------------------------------------------------------------------- #
# Legacy swarm endpoints
# --------------------------------------------------------------------------- #
@app.post("/report")
async def trigger_report(request: Request):
    """Manually trigger daily report."""
    if not _internal_ok(request):
        return _unauthorized()
    await daily_report()
    return {"status": "report_sent"}


@app.post("/swarm/schedule")
async def swarm_schedule(request: Request):
    """Schedule a deliberation for a future time."""
    if not _internal_ok(request):
        return _unauthorized()

    body = await request.json()
    topic = body.get("topic", "")
    run_at = body.get("run_at", "")  # ISO format datetime
    free_mode = body.get("free_mode", True)

    if not topic or not run_at:
        return JSONResponse(status_code=400, content={"error": "topic and run_at (ISO datetime) required"})
    try:
        topic = security.sanitize_text(topic, config.MAX_TOPIC_LENGTH)
    except security.InputError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    from dateutil.parser import isoparse

    try:
        trigger_time = isoparse(run_at)
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"error": "run_at must be valid ISO datetime"})

    job_id = str(uuid.uuid4())[:8]

    async def _run_scheduled():
        sid = str(uuid.uuid4())[:8]
        sessions[sid] = _new_swarm_session(topic, free_mode)
        scheduled_sessions[:] = [s for s in scheduled_sessions if s["job_id"] != job_id]
        await _run_deliberation(sid)

    scheduler.add_job(_run_scheduled, "date", run_date=trigger_time, id=job_id)
    entry = {"job_id": job_id, "topic": topic, "run_at": run_at, "free_mode": free_mode}
    scheduled_sessions.append(entry)
    return {"job_id": job_id, "topic": topic, "run_at": run_at, "status": "scheduled"}


@app.get("/swarm/scheduled")
async def swarm_scheduled():
    """List all upcoming scheduled deliberations."""
    return {"scheduled": scheduled_sessions, "count": len(scheduled_sessions)}


@app.post("/swarm/art")
@limiter.limit(config.RATE_LIMIT_ART)
async def swarm_art(request: Request):
    if not _internal_ok(request):
        return _unauthorized()

    body = await request.json()
    prompt = body.get("prompt", "")
    style = body.get("style", "default")
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt required"})

    result = await generate_art(prompt=prompt, style=style, aspect="1:1")
    if result.get("image_b64"):
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
        result["image_url"] = store_image(prompt_hash, result["image_b64"])
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
    if not _internal_ok(request):
        return _unauthorized()
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse(status_code=400, content={"error": "text required"})
    return post_tweet(text)


@app.post("/swarm/post")
async def swarm_post(request: Request):
    """Draft and post a thread from a completed deliberation."""
    if not _internal_ok(request):
        return _unauthorized()

    body = await request.json()
    session_id = body.get("session_id", "")
    session = sessions.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "session not found"})
    if session["status"] != "completed":
        return JSONResponse(status_code=400, content={"error": "deliberation not completed"})

    msgs = session["messages"]
    topic = session["topic"]
    consensus = [m for m in msgs if m["type"] == "consensus"]
    decisions = [m for m in msgs if m["type"] == "decision"]
    summary_text = consensus[-1]["text"] if consensus else decisions[-1]["text"] if decisions else "No consensus reached."
    agent_count = len(set(m["agent"] for m in msgs))
    msg_count = len(msgs)

    draft_prompt = f"""You are Claire, content agent for @phantomcap_ai. Draft a 3-tweet thread about this swarm deliberation.

Topic: {topic}
Agents participated: {agent_count}
Messages: {msg_count}
Final decision: {summary_text[:300]}

Rules:
- Tweet 1: hook — what just happened, make it compelling
- Tweet 2: the interesting detail — what surprised, who disagreed, what Cipher flagged
- Tweet 3: the link — direct to genesis.phantomcapital.live
- Each tweet max 270 chars
- Anti-slop: raw, specific, no corporate speak
- Include agent names when referencing their takes
- Format: return exactly 3 lines, one tweet per line, no numbering"""

    try:
        thread_text = await agent_turn(
            AGENT_MAP["Claire"]["system"],
            [{"role": "user", "content": draft_prompt}],
            agent_name="Claire",
        )
    except LLMError as e:
        return JSONResponse(status_code=502, content={"error": f"LLM error: {e}"})

    tweets = [t.strip() for t in thread_text.strip().split("\n") if t.strip()][:3]
    if not tweets:
        return JSONResponse(status_code=500, content={"error": "Claire failed to draft thread"})

    result = post_thread(tweets)
    result["tweets"] = tweets
    result["session_id"] = session_id
    return result


def _new_swarm_session(topic: str, free_mode: bool) -> dict:
    return {
        "topic": topic,
        "free_mode": free_mode,
        "status": "started",
        "messages": [],
        "current_round": 0,
        "events": asyncio.Queue(),
    }


@app.post("/swarm/start")
@limiter.limit(config.RATE_LIMIT_SWARM_START)
async def swarm_start(request: Request):
    if not _internal_ok(request):
        return _unauthorized()

    body = await request.json()
    topic = body.get("topic", "")
    free_mode = body.get("free_mode", False)
    if not topic:
        return JSONResponse(status_code=400, content={"error": "topic required"})
    try:
        topic = security.sanitize_text(topic, config.MAX_TOPIC_LENGTH)
    except security.InputError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    session_id = str(uuid.uuid4())[:8]
    sessions[session_id] = _new_swarm_session(topic, free_mode)
    _track(asyncio.create_task(_run_deliberation(session_id)))
    return {"session_id": session_id, "status": "started"}


def _session_event_stream(request: Request, session: dict):
    """Shared SSE generator: replay past messages, then stream new ones.

    Used by both swarm deliberations and bundle jobs — they share the same
    session shape (a ``messages`` list + an ``events`` asyncio.Queue closed by a
    ``None`` sentinel). Pings keep proxies from timing the connection out.
    """

    async def event_generator():
        for msg in session["messages"]:  # replay for late joiners
            yield {"data": json.dumps(msg)}
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(session["events"].get(), timeout=config.SSE_PING_INTERVAL_S)
                if msg is None:  # end signal
                    yield {"event": "done", "data": json.dumps({"type": "done"})}
                    break
                yield {"data": json.dumps(msg)}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"type": "ping"})}

    return EventSourceResponse(event_generator())


@app.get("/swarm/stream/{session_id}")
async def swarm_stream(request: Request, session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "session not found"})
    return _session_event_stream(request, session)


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


# --------------------------------------------------------------------------- #
# Automated AI Bundler endpoints
# --------------------------------------------------------------------------- #
@app.get("/bundle/pricing")
async def bundle_pricing():
    """Pricing summary for the crypto gate (or disabled)."""
    if crypto_payments.enabled():
        return crypto_payments.pricing()
    return {"enabled": False, "provider": None}


@app.get("/bundle/estimate")
async def bundle_estimate(mode: str = "full", agents: Optional[int] = None, tier: str = "standard"):
    """Approximate LLM cost of a run before starting it (see bundler.estimate_bundle_cost)."""
    return estimate_bundle_cost(mode=mode, size=agents, tier=tier)


@app.post("/bundle/create")
@limiter.limit(config.RATE_LIMIT_BUNDLE_CREATE)
async def bundle_create(request: Request):
    """Start a bundling job from a natural-language or JSON spec.

    Auth: when the crypto paywall is enabled, a verified on-chain payment (or the
    internal secret) is required. Otherwise the legacy internal-secret check
    applies — so existing deployments behave as before.
    """
    gate = _active_gate()
    if gate is not None:
        g = await gate.check(request.headers)
        if not g["ok"]:
            return JSONResponse(
                status_code=402,
                content={"error": g.get("reason", "payment required"), "pricing": gate.pricing()},
            )
    elif not _internal_ok(request):
        return _unauthorized()

    body = await request.json()
    spec = body.get("spec", "")
    if not spec and isinstance(body, dict):
        structured = {k: v for k, v in body.items() if k not in ("targets", "mode", "tier", "agents", "size")}
        if structured:
            spec = json.dumps(structured)
    if not spec:
        return JSONResponse(
            status_code=400,
            content={"error": "spec required (natural-language string or structured JSON)"},
        )
    if not isinstance(spec, str):
        spec = json.dumps(spec)

    # Sanitize / cap the spec before it reaches any prompt.
    try:
        spec = security.sanitize_spec(spec)
    except security.InputError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    mode = str(body.get("mode", "full")).lower()
    if mode not in ("full", "lite"):
        mode = "full"

    tier = str(body.get("tier", "standard")).lower()
    if tier not in ("economy", "standard", "premium"):
        tier = "standard"

    size = body.get("agents", body.get("size"))
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None

    from bundler import TARGET_BUILDERS

    targets = body.get("targets")
    targets = [t for t in targets if t in TARGET_BUILDERS] if isinstance(targets, list) else None

    session_id = str(uuid.uuid4())[:8]
    sessions[session_id] = {
        "kind": "bundle",
        "spec": spec,
        "mode": mode,
        "tier": tier,
        "size": size,
        "targets": targets,
        "status": "started",
        "messages": [],
        "events": asyncio.Queue(),
    }

    _track(asyncio.create_task(run_bundle(session_id, sessions)))
    log.info("bundle %s started (mode=%s tier=%s size=%s)", session_id, mode, tier, size)

    return {
        "session_id": session_id,
        "status": "started",
        "mode": mode,
        "tier": tier,
        "size": size,
        "estimate": estimate_bundle_cost(mode=mode, size=size, tier=tier),
        "stream": f"/bundle/stream/{session_id}",
        "download": f"/bundle/{session_id}/download",
    }


@app.get("/bundle/stream/{session_id}")
async def bundle_stream(request: Request, session_id: str):
    """SSE stream of a bundling job (deliberation + file generation)."""
    session = sessions.get(session_id)
    if not session or session.get("kind") != "bundle":
        return JSONResponse(status_code=404, content={"error": "bundle session not found"})
    return _session_event_stream(request, session)


@app.get("/bundle/status/{session_id}")
async def bundle_status(session_id: str):
    """Current status + summary of a bundling job (falls back to disk)."""
    session = sessions.get(session_id)
    if session and session.get("kind") == "bundle":
        bp = session.get("blueprint") or {}
        res = session.get("resilience") or {}
        return {
            "session_id": session_id,
            "status": session["status"],
            "mode": session.get("mode", "full"),
            "tier": session.get("tier", "standard"),
            "message_count": len(session["messages"]),
            "name": bp.get("name"),
            "version": bp.get("version"),
            "file_count": len(session.get("files") or {}),
            "files": sorted((session.get("files") or {}).keys()),
            "targets": bp.get("targets"),
            "usage": session.get("usage"),
            "error": session.get("error"),
            "resilience": {"down": sorted(res.get("down", [])), "covered": res.get("covered", [])},
            "download": f"/bundle/{session_id}/download" if session["status"] == "completed" else None,
        }

    meta = store.load_meta(session_id)
    if meta:
        bp = meta.get("blueprint") or {}
        return {
            "session_id": session_id,
            "status": "completed",
            "message_count": 0,
            "name": bp.get("name"),
            "version": bp.get("version"),
            "file_count": meta.get("file_count", 0),
            "files": sorted((meta.get("files") or {}).keys()),
            "targets": bp.get("targets"),
            "error": None,
            "download": f"/bundle/{session_id}/download",
            "source": "disk",
        }

    return JSONResponse(status_code=404, content={"error": "bundle session not found"})


@app.get("/bundle/list")
async def bundle_list():
    """List all persisted bundles (newest first)."""
    return {"bundles": store.list_bundles()}


@app.delete("/bundle/{session_id}")
async def bundle_delete(session_id: str, request: Request):
    """Delete a bundle (from disk and memory). Admin-only via internal secret."""
    if not _internal_ok(request):
        return _unauthorized()
    in_mem = sessions.pop(session_id, None) is not None
    on_disk = store.delete_bundle(session_id)
    if not (in_mem or on_disk):
        return JSONResponse(status_code=404, content={"error": "bundle not found"})
    return {"deleted": True, "session_id": session_id}


@app.get("/bundle/{session_id}/download")
async def bundle_download(session_id: str, format: str = "zip"):
    """Download the generated bundle as a zip, or its manifest as JSON.

    ``?format=manifest`` returns the raw { path: content } file map instead of the
    binary zip. Falls back to disk if the live session has been reclaimed.
    """
    session = sessions.get(session_id)
    in_memory = bool(session and session.get("kind") == "bundle")

    if in_memory and session["status"] != "completed":
        return JSONResponse(
            status_code=409, content={"error": f"bundle not ready (status: {session['status']})"}
        )

    if in_memory and session["status"] == "completed":
        blueprint = session.get("blueprint")
        files = session.get("files")
        zip_bytes = session.get("bundle_zip")
    else:
        meta = store.load_meta(session_id)
        if not meta:
            return JSONResponse(status_code=404, content={"error": "bundle session not found"})
        blueprint = meta.get("blueprint")
        files = meta.get("files")
        zip_bytes = store.load_zip(session_id)

    if format == "manifest":
        return {"blueprint": blueprint, "files": files}
    if not zip_bytes:
        return JSONResponse(status_code=500, content={"error": "bundle zip missing"})

    slug = (blueprint or {}).get("slug", "bundle")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


@app.get("/bundle/targets")
async def bundle_targets():
    """List supported output targets (for building UIs)."""
    from bundler import TARGET_BUILDERS

    return {
        "targets": DEFAULT_TARGETS,
        "available": sorted(TARGET_BUILDERS.keys()),
        "bundler_version": BUNDLER_VERSION,
    }


@app.get("/bundle/ui", response_class=HTMLResponse)
async def bundle_ui():
    """Minimal web UI: submit a spec, watch the hive stream, download the zip."""
    return HTMLResponse(content=BUNDLER_UI)


# --------------------------------------------------------------------------- #
# Legacy swarm deliberation runner
# --------------------------------------------------------------------------- #
async def _swarm_turn(system: str, prompt: str, agent_name: str) -> str:
    """A resilient agent turn for the deliberation: an LLM failure becomes a
    short in-character note rather than aborting the whole deliberation."""
    try:
        return await agent_turn(system, [{"role": "user", "content": prompt}], agent_name=agent_name)
    except LLMError as e:
        log.warning("swarm: %s turn failed (%s)", agent_name, str(e)[:80])
        return f"({agent_name} is momentarily unavailable.)"


async def _run_deliberation(session_id: str):
    session = sessions[session_id]
    topic = session["topic"]
    conversation: list[dict] = []
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

    rounds = config.SWARM_ROUNDS
    delay = config.SWARM_TURN_DELAY_S
    try:
        session["status"] = "deliberating"

        opening = await _swarm_turn(
            AGENT_MAP["Phoebe"]["system"],
            f"Open a swarm deliberation on this topic: {topic}. Set the frame for the other agents. Be concise.",
            "Phoebe",
        )
        conversation.append({"role": "assistant", "content": f"Phoebe: {opening}"})
        await emit("Phoebe", opening, 0, "decision")
        await asyncio.sleep(delay)

        for round_num in range(1, rounds + 1):
            session["current_round"] = round_num
            for agent in AGENTS:
                name = agent["name"]
                if name == "Phoebe" and round_num < rounds:
                    continue  # Phoebe only speaks at end of each round
                prompt = (
                    f"Topic: {topic}\n\nConversation so far:\n"
                    + "\n".join(m["content"] for m in conversation[-10:])
                    + f"\n\nRespond as {name} in round {round_num}. React to what others said. Be concise (2-3 sentences)."
                )
                text = await _swarm_turn(agent["system"], prompt, name)
                conversation.append({"role": "assistant", "content": f"{name}: {text}"})
                await emit(name, text, round_num)
                await asyncio.sleep(delay)

            if round_num < rounds:
                synth_prompt = (
                    f"Topic: {topic}\n\nRound {round_num} discussion:\n"
                    + "\n".join(m["content"] for m in conversation[-5:])
                    + "\n\nSynthesize this round. What's the emerging consensus? What needs more debate? Be concise."
                )
                synth = await _swarm_turn(AGENT_MAP["Phoebe"]["system"], synth_prompt, "Phoebe")
                conversation.append({"role": "assistant", "content": f"Phoebe: {synth}"})
                await emit("Phoebe", synth, round_num, "decision")
                await asyncio.sleep(delay)

        final_prompt = (
            f"Topic: {topic}\n\nFull deliberation:\n"
            + "\n".join(m["content"] for m in conversation[-15:])
            + "\n\nAnnounce the swarm's final decision. This is the consensus. Be definitive."
        )
        final = await _swarm_turn(AGENT_MAP["Phoebe"]["system"], final_prompt, "Phoebe")
        conversation.append({"role": "assistant", "content": f"Phoebe: {final}"})
        await emit("Phoebe", final, rounds, "consensus")

        art_round = rounds + 1
        if any(kw in topic.lower() for kw in ["token", "launch", "name", "$", "ticker"]):
            await emit("Claire", "Generating visual direction for the token...", art_round, "tool_call")
            await asyncio.sleep(1)
            art_prompt_req = (
                f"Based on this deliberation:\n{final}\n\nWrite a single concise image generation prompt "
                "(under 100 words) for a token logo. Style: minimalist, gold on black, geometric, "
                "crypto-native. No text in the image."
            )
            art_prompt = await _swarm_turn(AGENT_MAP["Claire"]["system"], art_prompt_req, "Claire")
            conversation.append({"role": "assistant", "content": f"Claire: {art_prompt}"})
            await emit("Claire", f"Art prompt: {art_prompt}", art_round, "tool_call")
            await asyncio.sleep(delay)

            art_result = await generate_art(prompt=art_prompt, style="geometric", aspect="1:1")
            if art_result.get("image_b64"):
                prompt_hash = hashlib.md5(art_prompt.encode()).hexdigest()[:12]
                session["image_url"] = store_image(prompt_hash, art_result["image_b64"])
                await emit(
                    "Nova",
                    f"Token art generated. Model: {art_result['model_used']}, Cost: ${art_result['cost']}",
                    art_round, "tool_call",
                )
            elif art_result.get("error"):
                await emit("Nova", f"Art generation skipped: {art_result['error'][:100]}", art_round, "message")
            else:
                await emit("Nova", "Art generation unavailable — SEGMIND_API_KEY not configured.", art_round, "message")

        session["status"] = "completed"
        await _auto_post_thread(session, topic, rounds, emit)

    except Exception as e:
        session["status"] = "error"
        log.exception("deliberation %s failed", session_id)
        await emit("Phoebe", f"Deliberation error: {str(e)[:100]}", 0, "message")

    await session["events"].put(None)


async def _auto_post_thread(session: dict, topic: str, rounds: int, emit) -> None:
    """Best-effort: Claire drafts + posts a recap thread. Never fails the run."""
    try:
        msgs = session["messages"]
        consensus = [m for m in msgs if m["type"] == "consensus"]
        summary = consensus[-1]["text"][:300] if consensus else ""
        agent_count = len(set(m["agent"] for m in msgs))
        draft_prompt = (
            f"Draft a 3-tweet thread for @phantomcap_ai about this swarm deliberation.\n"
            f"Topic: {topic}\nAgents: {agent_count}, Messages: {len(msgs)}\nDecision: {summary}\n"
            "Rules: tweet 1 = hook, tweet 2 = interesting detail, tweet 3 = link to "
            "genesis.phantomcapital.live. Max 270 chars each. Anti-slop. Return exactly 3 lines."
        )
        thread_text = await _swarm_turn(AGENT_MAP["Claire"]["system"], draft_prompt, "Claire")
        tweets = [t.strip() for t in thread_text.strip().split("\n") if t.strip()][:3]
        if tweets:
            result = post_thread(tweets)
            if result.get("tweet_ids"):
                await emit("Claire", f"Thread posted to @phantomcap_ai ({len(result['tweet_ids'])} tweets)", rounds + 1, "tool_call")
            elif result.get("error"):
                await emit("Claire", f"Twitter post skipped: {result['error'][:80]}", rounds + 1, "message")
    except Exception:
        log.debug("auto-post thread skipped", exc_info=True)
