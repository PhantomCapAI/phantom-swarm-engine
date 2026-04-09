"""Daily 9AM EST health report to Telegram."""

import os
import asyncio
from datetime import datetime, timezone, timedelta

import httpx

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1516882079")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

SERVICES = [
    ("phoebe-hermes", "https://phantom-swarm-engine.zeabur.app/health"),
    ("x402-gate", "https://phantom-x402-gate.zeabur.app/health"),
    ("swarm-engine", "https://phantom-swarm-engine.zeabur.app/health"),
    ("pump-launch", "https://phantom-pump-launch.zeabur.app/health"),
    ("bags-launch", "https://phantom-bags-launch.zeabur.app/health"),
]


async def _ping(name: str, url: str) -> tuple[str, bool]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(url)
            return name, r.status_code == 200
    except Exception:
        return name, False


async def _openrouter_balance() -> str:
    if not OPENROUTER_API_KEY:
        return "key not set"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            )
            data = r.json()
            # Check for limit/usage info
            limit = data.get("data", {}).get("limit")
            usage = data.get("data", {}).get("usage", 0)
            if limit:
                remaining = limit - usage
                return f"${remaining:.2f} remaining"
            else:
                return f"${usage:.4f} used (no limit)"
    except Exception as e:
        return f"error: {str(e)[:50]}"


async def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            )
    except Exception:
        pass


async def daily_report():
    """Run the daily health report."""
    # Ping all services
    results = await asyncio.gather(*[_ping(n, u) for n, u in SERVICES])
    alive = sum(1 for _, ok in results if ok)
    total = len(SERVICES)

    # OpenRouter balance
    balance = await _openrouter_balance()

    # Date
    est = timezone(timedelta(hours=-5))
    now = datetime.now(est)
    date_str = now.strftime("%B %d, %Y")

    # Service details
    svc_lines = []
    for name, ok in results:
        svc_lines.append(f"  {'✅' if ok else '❌'} {name}")

    report = (
        f"☀️ DAILY REPORT — {date_str}\n\n"
        f"Services: {alive}/{total} alive\n"
        + "\n".join(svc_lines)
        + f"\n\nOpenRouter: {balance}\n"
        f"Revenue: $0.00 (x402 pre-launch)"
    )

    await _send_telegram(report)
    print(f"[cron] Daily report sent at {now.isoformat()}")


def run_daily_report():
    """Sync wrapper for APScheduler."""
    asyncio.get_event_loop().create_task(daily_report())
