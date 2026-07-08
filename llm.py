"""LLM client — provider-agnostic.

Works with **OpenRouter** or **DeepSeek** (both expose an OpenAI-compatible
chat-completions API, so switching is just config). Select with ``LLM_PROVIDER``:

    LLM_PROVIDER=openrouter   OPENROUTER_API_KEY=...     (default)
    LLM_PROVIDER=deepseek     DEEPSEEK_API_KEY=...

Per-tier models are overridable per provider (see PROVIDERS below). Two tiers:
"premium" (orchestration / structured JSON) and "fleet" (everyone else).

Adds retries with backoff, a real connectivity health check, and token/cost
estimation used for pre-run cost quotes and per-session usage tracking.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx

import config
from logging_setup import get_logger

log = get_logger("phantom.llm")

# provider -> endpoint, key env var, and default premium/fleet models.
PROVIDERS = {
    "openrouter": {
        "url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
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


class LLMError(RuntimeError):
    """Raised when a completion cannot be obtained after retries."""


def _resolve(agent_name: str, model: Optional[str], premium: bool) -> tuple[str, str, str, bool]:
    """Pick (url, api_key, model, is_premium) for this call."""
    prov = _provider()
    key = os.getenv(prov["key_env"], "")
    want_premium = premium or agent_name == "Phoebe"
    if model is None:
        model = prov["premium"] if want_premium else prov["fleet"]
    return prov["url"], key, model, want_premium


# --------------------------------------------------------------------------- #
# Token & cost estimation (approximate — for quotes and tracking, not billing)
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars/token). Good enough for budgeting."""
    return max(1, len(text or "") // 4)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int, premium: bool) -> float:
    """Indicative USD cost from token counts and the configured per-Mtok rates."""
    rate = config.COST_PREMIUM_PER_MTOK if premium else config.COST_FLEET_PER_MTOK
    return round((prompt_tokens + completion_tokens) / 1_000_000 * rate, 6)


async def agent_turn(
    system_prompt: str,
    conversation: list[dict],
    agent_name: str = "",
    max_tokens: int = 200,
    temperature: float = 0.8,
    model: Optional[str] = None,
    premium: bool = False,
    usage: Optional[dict] = None,
) -> str:
    """Call the active LLM provider and return the agent's response text.

    Retries transient failures (network errors, 429, 5xx) with exponential
    backoff, then raises :class:`LLMError`. If ``usage`` (a mutable dict) is
    passed, accumulates ``prompt_tokens``, ``completion_tokens``, ``calls`` and
    ``cost_usd`` into it for per-session cost tracking.
    """
    url, api_key, model, is_premium = _resolve(agent_name, model, premium)
    if not api_key:
        raise LLMError(f"no API key configured for provider '{provider_name()}'")

    messages = [{"role": "system", "content": system_prompt}] + conversation
    # Longer generations (blueprints, file bodies) need a bigger read timeout.
    timeout = config.LLM_TIMEOUT_SHORT_S if max_tokens <= 400 else config.LLM_TIMEOUT_LONG_S
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err = "unknown error"
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
            if resp.status_code == 200:
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                _record_usage(usage, data, messages, text, is_premium)
                return text.strip()
            # Retry on rate limit / server errors; fail fast on client errors.
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
            else:
                raise LLMError(f"LLM call failed: HTTP {resp.status_code} {resp.text[:160]}")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:100]}"

        if attempt < config.LLM_MAX_RETRIES:
            backoff = 2 ** attempt
            log.warning("llm: %s (attempt %d) — retrying in %ss", last_err, attempt + 1, backoff)
            await asyncio.sleep(backoff)

    raise LLMError(f"LLM call failed after {config.LLM_MAX_RETRIES + 1} attempts: {last_err}")


def _record_usage(usage: Optional[dict], data: dict, messages: list, text: str, premium: bool) -> None:
    if usage is None:
        return
    u = data.get("usage") or {}
    prompt_tokens = int(u.get("prompt_tokens") or estimate_tokens("".join(m["content"] for m in messages)))
    completion_tokens = int(u.get("completion_tokens") or estimate_tokens(text))
    usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + prompt_tokens
    usage["completion_tokens"] = usage.get("completion_tokens", 0) + completion_tokens
    usage["calls"] = usage.get("calls", 0) + 1
    usage["cost_usd"] = round(
        usage.get("cost_usd", 0.0) + estimate_cost_usd(prompt_tokens, completion_tokens, premium), 6
    )


async def check_llm() -> dict:
    """Live connectivity probe: a tiny completion against the active provider.

    Returns {ok, provider, model, latency_ms, error?}. Used by /health so an
    operator can tell configured-but-broken from actually-working.
    """
    prov = provider_name()
    if not configured():
        return {"ok": False, "provider": prov, "error": "no API key configured"}
    start = time.monotonic()
    try:
        await agent_turn(
            "You are a health probe.",
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
            temperature=0.0,
        )
        latency = int((time.monotonic() - start) * 1000)
        return {"ok": True, "provider": prov, "model": _provider()["fleet"], "latency_ms": latency}
    except Exception as e:
        return {"ok": False, "provider": prov, "error": str(e)[:160]}


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
