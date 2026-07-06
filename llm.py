import json
import os
import re

import httpx

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Phoebe gets the premium model for synthesis/decisions
# Fleet agents use free-tier Llama to preserve credits
PHOEBE_MODEL = "anthropic/claude-sonnet-4-6"
FLEET_MODEL = "meta-llama/llama-3.3-70b-instruct"

# Alias used by the bundler for generation-heavy calls (blueprints, prompts).
# These need the stronger model and larger token budgets than a fleet turn.
PREMIUM_MODEL = PHOEBE_MODEL


async def agent_turn(
    system_prompt: str,
    conversation: list[dict],
    agent_name: str = "",
    max_tokens: int = 200,
    temperature: float = 0.8,
    model: str | None = None,
) -> str:
    """Call OpenRouter and return the agent's response text.

    Backward-compatible: existing callers pass only the first three args and
    get the original behavior (Phoebe → premium model, everyone else → fleet,
    200 tokens, temp 0.8). The bundler passes explicit ``max_tokens`` / ``model``
    for longer, deterministic generation.
    """
    if model is None:
        model = PHOEBE_MODEL if agent_name == "Phoebe" else FLEET_MODEL
    messages = [{"role": "system", "content": system_prompt}] + conversation

    # Longer generations (blueprints, file bodies) need a bigger read timeout.
    timeout = 30.0 if max_tokens <= 400 else 120.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            BASE_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        data = resp.json()

    choice = data.get("choices", [{}])[0]
    return choice.get("message", {}).get("content", "").strip()


def extract_json(text: str):
    """Best-effort extraction of a JSON object/array from a model response.

    Models often wrap JSON in ```json fences or add prose. This strips fences
    and, failing that, grabs the outermost {...} or [...] block. Returns the
    parsed value, or ``None`` if nothing parseable is found.
    """
    if not text:
        return None

    # 1) Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) Fenced code block ```json ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except Exception:
            pass

    # 3) Outermost brace/bracket span
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue

    return None
