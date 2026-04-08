import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette import EventSourceResponse

from agents import AGENTS, AGENT_MAP
from llm import agent_turn

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

# In-memory session store
sessions: dict[str, dict] = {}

ROUNDS = 3


@app.get("/health")
async def health():
    return {"status": "alive", "engine": "phantom-swarm"}


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
        )
        conversation.append({"role": "assistant", "content": f"Phoebe: {opening}"})
        await emit("Phoebe", opening, 0, "decision")
        await asyncio.sleep(2)

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

                text = await agent_turn(agent["system"], [{"role": "user", "content": prompt}])
                conversation.append({"role": "assistant", "content": f"{name}: {text}"})
                await emit(name, text, round_num)
                await asyncio.sleep(2)

            # Phoebe synthesizes at end of each round
            if round_num < ROUNDS:
                synth_prompt = f"Topic: {topic}\n\nRound {round_num} discussion:\n" + "\n".join(
                    m["content"] for m in conversation[-5:]
                ) + "\n\nSynthesize this round. What's the emerging consensus? What needs more debate? Be concise."

                synth = await agent_turn(AGENT_MAP["Phoebe"]["system"], [{"role": "user", "content": synth_prompt}])
                conversation.append({"role": "assistant", "content": f"Phoebe: {synth}"})
                await emit("Phoebe", synth, round_num, "decision")
                await asyncio.sleep(2)

        # Final decision
        final_prompt = f"Topic: {topic}\n\nFull deliberation:\n" + "\n".join(
            m["content"] for m in conversation[-15:]
        ) + "\n\nAnnounce the swarm's final decision. This is the consensus. Be definitive."

        final = await agent_turn(AGENT_MAP["Phoebe"]["system"], [{"role": "user", "content": final_prompt}])
        conversation.append({"role": "assistant", "content": f"Phoebe: {final}"})
        await emit("Phoebe", final, ROUNDS, "consensus")

        session["status"] = "completed"

    except Exception as e:
        session["status"] = "error"
        await emit("Phoebe", f"Deliberation error: {str(e)[:100]}", 0, "message")

    # Signal stream end
    await session["events"].put(None)
