"""Optional Stripe payment gate for bundle creation.

Monetizes the **hosted service**: it charges people to run a bundle job on your
server, via Stripe Checkout. Transparent (price shown before paying), enforceable
(payment verified with Stripe before any work starts), and the money lands in
your own Stripe account — nothing is hardcoded.

Disabled by default, so existing deployments are unaffected. When enabled,
``POST /bundle/create`` requires either:
  * an admin bypass via the ``X-Phantom-Internal`` secret, or
  * a paid Stripe Checkout Session id in the ``X-Payment-Session`` header.

Flow:
  1. client calls ``POST /bundle/checkout`` -> gets a Stripe Checkout URL
  2. client pays on Stripe; Stripe redirects back with the session id
  3. client calls ``POST /bundle/create`` with ``X-Payment-Session: <id>``
  4. server verifies the session is ``paid`` (and unused), then runs the job

Config (read from the environment at call time):

    BUNDLE_PAYMENTS_ENABLED           "1" / "true" to turn the gate on
    STRIPE_SECRET_KEY                 sk_live_... / sk_test_...
    BUNDLE_PRICE                      human price, e.g. "100"
    STRIPE_CURRENCY                   ISO currency, default "usd"
    STRIPE_PRODUCT_NAME               line-item label, default "AI Bundle Generation"
    PUBLIC_BASE_URL                   base URL for Stripe success/cancel redirects
    BUNDLE_PAYMENTS_DEV_ACCEPT_TOKEN  testing-only: accept this exact session id
"""

import os

try:  # optional dependency — only needed when the gate is enabled
    import stripe
except Exception:  # pragma: no cover
    stripe = None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def enabled() -> bool:
    """Whether the Stripe gate is active (flag on + key present + lib available)."""
    flag = _env("BUNDLE_PAYMENTS_ENABLED").lower() in ("1", "true", "yes", "on")
    return flag and bool(_env("STRIPE_SECRET_KEY")) and stripe is not None


def _configure() -> None:
    stripe.api_key = _env("STRIPE_SECRET_KEY")


def pricing() -> dict:
    """Human-readable pricing summary (for UIs / the /bundle/pricing endpoint)."""
    return {
        "enabled": enabled(),
        "price": _env("BUNDLE_PRICE", "100"),
        "currency": _env("STRIPE_CURRENCY", "usd"),
        "provider": "stripe",
        "checkout": "/bundle/checkout",
    }


def _unit_amount() -> int:
    """Price in the smallest currency unit (Stripe wants cents for USD)."""
    try:
        return int(round(float(_env("BUNDLE_PRICE", "100")) * 100))
    except Exception:
        return 10000


def create_checkout(base_url: str) -> dict:
    """Create a Stripe Checkout Session. Returns {'url', 'id'} or {'error'}."""
    if not enabled():
        return {"error": "payments disabled"}
    _configure()
    ui = (_env("PUBLIC_BASE_URL", base_url).rstrip("/")) + "/bundle/ui"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": _env("STRIPE_CURRENCY", "usd"),
                        "product_data": {"name": _env("STRIPE_PRODUCT_NAME", "AI Bundle Generation")},
                        "unit_amount": _unit_amount(),
                    },
                    "quantity": 1,
                }
            ],
            # Stripe substitutes the real id into {CHECKOUT_SESSION_ID}.
            success_url=f"{ui}?paid={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{ui}?canceled=1",
        )
        return {"url": session.url, "id": session.id}
    except Exception as e:
        return {"error": str(e)[:200]}


# Consumed session ids (replay protection). In-memory: resets on restart, which
# only ever fails closed (a paid session can't be reused, never falsely reused).
_consumed: set = set()


def _verify_session(session_id: str) -> tuple[bool, str]:
    """Check a Checkout Session is paid and not already consumed."""
    if session_id in _consumed:
        return False, "session already used"
    _configure()
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return False, f"retrieve error: {str(e)[:100]}"
    if getattr(session, "payment_status", None) == "paid":
        return True, "paid"
    return False, f"payment_status={getattr(session, 'payment_status', 'unknown')}"


def consume(session_id: str) -> None:
    """Mark a session id as used so it cannot pay for a second bundle."""
    if session_id:
        _consumed.add(session_id)


async def check(headers) -> dict:
    """Authorize a bundle-create request.

    ``headers`` is any case-insensitive mapping with ``.get`` (e.g. Starlette's
    request.headers). Returns ``{"ok", "via", "session"?, "reason"?}``. On a
    verified Stripe payment the session is consumed here to prevent replay.
    """
    if not enabled():
        return {"ok": True, "via": "disabled"}

    # Admin bypass — the operator's own automation.
    secret = _env("PHANTOM_INTERNAL_SECRET")
    if secret and headers.get("X-Phantom-Internal") == secret:
        return {"ok": True, "via": "internal"}

    session_id = headers.get("X-Payment-Session")
    if not session_id:
        return {"ok": False, "via": "none", "reason": "payment required"}

    # Testing-only static accept (never set this in production).
    dev_token = _env("BUNDLE_PAYMENTS_DEV_ACCEPT_TOKEN")
    if dev_token and session_id == dev_token:
        return {"ok": True, "via": "dev-accept", "session": session_id}

    ok, reason = _verify_session(session_id)
    if ok:
        consume(session_id)
        return {"ok": True, "via": "stripe", "session": session_id}
    return {"ok": False, "via": "stripe", "reason": reason}
