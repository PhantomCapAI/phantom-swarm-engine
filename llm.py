"""LLM client — provider-agnostic.

Works with **OpenRouter** or **DeepSeek** (both expose an OpenAI-compatible
chat-completions API, so switching is just config). Select with ``LLM_PROVIDER``:

    LLM_PROVIDER=openrouter   OPENROUTER_API_KEY=...     (default)
    LLM_PROVIDER=deepseek     DEEPSEEK_API_KEY=...

Per-tier models are overridable per provider (see PROVIDERS below). Two tiers:
"premium" (orchestration / structured JSON) and "fleet" (everyone else).
"""

import json
import os
import re

import httpx

# provider -> endpoint, key env var, and default premium/fleet models.
PROVIDERS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "premium": os.getenv("OPENROUTER_PREMIUM_MODEL", "anthropic/claude-sonnet-4-6"),
        "fleet": os.getenv("OPENROUTER_FLEET_MODEL", "meta-llama/llama-3.3-70b-instruct"),
    },
    "deepseek": {
        "url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "premium": os.getenv("DEEPSEEK_PREMIUM_MODEL", "deepseek-reasoner"),
        "fleet": os.getenv("DEEPSEEK_FLEET_MODEL", "deepseek-chat"),
    },
}


def provider_name() -> str:
    name = os.getenv("LLM_PROVIDER", "openrouter").lower()
    return name if name in PROVIDERS else "openrouter"


def _provider() -> dict:
    return PROVIDERS[provider_name()]


def configured() -> bool:
    """True if the active provider has an API key set (i.e. the crew can run)."""
    return bool(os.getenv(_provider()["key_env"], ""))


# Backward-compatible aliases (OpenRouter defaults) for any external importers.
PHOEBE_MODEL = PROVIDERS["openrouter"]["premium"]
FLEET_MODEL = PROVIDERS["openrouter"]["fleet"]
PREMIUM_MODEL = PHOEBE_MODEL


def _resolve(agent_name: str, model: str | None, premium: bool) -> tuple[str, str, str]:
    """Pick (url, api_key, model) for this call based on the active provider."""
    prov = _provider()
    key = os.getenv(prov["key_env"], "")
    if model is None:
        want_premium = premium or agent_name == "Phoebe"
        model = prov["premium"] if want_premium else prov["fleet"]
    return prov["url"], key, model


async def agent_turn(
    system_prompt: str,
    conversation: list[dict],
    agent_name: str = "",
    max_tokens: int = 200,
    temperature: float = 0.8,
    model: str | None = None,
    premium: bool = False,
) -> str:
    """Call the active LLM provider and return the agent's response text.

    Backward-compatible: existing callers pass only the first few args. ``premium``
    requests the premium tier without hardcoding a provider-specific model name
    (so it works on OpenRouter or DeepSeek); an explicit ``model`` still wins.
    """
    url, api_key, model = _resolve(agent_name, model, premium)
    messages = [{"role": "system", "content": system_prompt}] + conversation

    # Longer generations (blueprints, file bodies) need a bigger read timeout.
    timeout = 30.0 if max_tokens <= 400 else 120.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
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
