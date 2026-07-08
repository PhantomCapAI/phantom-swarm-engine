"""Typed errors that carry a human-friendly message and an optional hint.

Every command surfaces failures through :class:`PhantomError` so the top-level
handler can print one consistent, helpful block (red message + dim suggestion)
instead of a raw traceback. The goal: the user always knows what went wrong and
what to try next.
"""

from __future__ import annotations


class PhantomError(Exception):
    """A user-facing error. ``hint`` is an optional next-step suggestion."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class RemoteUnreachable(PhantomError):
    """Could not reach the engine at the configured/auto-detected URL."""

    def __init__(self, url: str, detail: str | None = None):
        msg = f"Could not reach the Phantom engine at {url}"
        if detail:
            msg += f" ({detail})"
        super().__init__(
            msg,
            hint=(
                "Is the engine running?  Start it with\n"
                "    uvicorn main:app --host 0.0.0.0 --port 8500\n"
                "or point at a hosted instance with  --remote https://host:port"
            ),
        )


class PaymentRequired(PhantomError):
    """The remote paywall rejected (or requires) a payment."""

    def __init__(self, reason: str, pricing: dict | None = None):
        hint = "Pay the accepted asset, then retry with  --payment-tx <signature>"
        if pricing and pricing.get("pay_to"):
            assets = ", ".join(
                f"{o['asset']} {o['price']}" for o in (pricing.get("options") or [])
            )
            hint = (
                f"Send {assets or 'payment'} to {pricing['pay_to']} "
                f"({pricing.get('network', 'solana')}), then retry with "
                "--payment-tx <signature>"
            )
        super().__init__(f"Payment required: {reason}", hint=hint)


class NotFound(PhantomError):
    """A session/bundle id was not found on the remote or on disk."""


class ApiError(PhantomError):
    """The engine returned an unexpected error response."""
