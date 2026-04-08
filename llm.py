import os
import httpx

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = "anthropic/claude-sonnet-4-6"
BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


async def agent_turn(system_prompt: str, conversation: list[dict]) -> str:
    """Call OpenRouter and return the agent's response text."""
    messages = [{"role": "system", "content": system_prompt}] + conversation

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            BASE_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0.8,
            },
        )
        data = resp.json()

    choice = data.get("choices", [{}])[0]
    return choice.get("message", {}).get("content", "").strip()
