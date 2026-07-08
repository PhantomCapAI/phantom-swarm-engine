"""Central configuration, constants, and startup validation.

Everything that used to be a scattered magic number or an inline
``os.environ.get`` now has one home here. Values are read from the environment
at import time for tuning knobs that never change at runtime (timeouts, limits),
while secrets/feature flags that tests toggle (crypto, LLM keys) are still read
at call time by their own modules so this file stays side-effect free.

``validate_environment`` runs on startup and returns a structured report of
warnings and errors so operators learn about misconfiguration immediately
instead of at first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _flag(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Deliberation / bundler tunables (were magic numbers scattered across files)
# --------------------------------------------------------------------------- #
SWARM_ROUNDS = _int("SWARM_ROUNDS", 3)
# Pacing between agent turns in the legacy swarm (keeps the SSE stream readable).
SWARM_TURN_DELAY_S = _float("SWARM_TURN_DELAY_S", 2.0)

# LLM token budgets per pipeline step (bundler).
CRITIQUE_MAX_TOKENS = _int("CRITIQUE_MAX_TOKENS", 200)
DESIGN_MAX_TOKENS = _int("DESIGN_MAX_TOKENS", 1500)
REFINE_MAX_TOKENS = _int("REFINE_MAX_TOKENS", 1800)
OPTIMIZE_MAX_TOKENS = _int("OPTIMIZE_MAX_TOKENS", 800)

# Concurrency cap for the parallel critique fan-out. Protects the upstream LLM
# provider (and your rate limits) from a 20-way burst.
BUNDLER_MAX_CONCURRENCY = _int("BUNDLER_MAX_CONCURRENCY", 6)

# --------------------------------------------------------------------------- #
# HTTP / networking
# --------------------------------------------------------------------------- #
LLM_TIMEOUT_SHORT_S = _float("LLM_TIMEOUT_SHORT_S", 30.0)
LLM_TIMEOUT_LONG_S = _float("LLM_TIMEOUT_LONG_S", 120.0)
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 2)
SSE_PING_INTERVAL_S = _float("SSE_PING_INTERVAL_S", 15.0)
RPC_TIMEOUT_S = _float("RPC_TIMEOUT_S", 20.0)

# --------------------------------------------------------------------------- #
# Limits & safety
# --------------------------------------------------------------------------- #
MAX_SPEC_LENGTH = _int("MAX_SPEC_LENGTH", 8000)
MAX_TOPIC_LENGTH = _int("MAX_TOPIC_LENGTH", 2000)

# Rate limits (slowapi syntax). Applied to the heavy endpoints only.
RATE_LIMIT_BUNDLE_CREATE = os.environ.get("RATE_LIMIT_BUNDLE_CREATE", "10/minute")
RATE_LIMIT_SWARM_START = os.environ.get("RATE_LIMIT_SWARM_START", "10/minute")
RATE_LIMIT_ART = os.environ.get("RATE_LIMIT_ART", "20/minute")
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "120/minute")

# --------------------------------------------------------------------------- #
# Cost model — indicative USD per 1M tokens, used only for pre-run estimates.
# These are deliberately approximate; override via env for accuracy.
# --------------------------------------------------------------------------- #
COST_PREMIUM_PER_MTOK = _float("COST_PREMIUM_PER_MTOK", 3.0)
COST_FLEET_PER_MTOK = _float("COST_FLEET_PER_MTOK", 0.20)

# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
_DEFAULT_ORIGINS = [
    "https://phantomcapital.live",
    "https://genesis.phantomcapital.live",
    "https://phantom-genesis-ui.vercel.app",
    "http://localhost:3000",
]


def cors_origins() -> list[str]:
    """Allowed CORS origins. Comma-separated ``CORS_ORIGINS`` overrides defaults;
    ``*`` allows any (development only)."""
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if not raw:
        return list(_DEFAULT_ORIGINS)
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_JSON = _flag("LOG_JSON")


@dataclass
class ValidationReport:
    """Result of :func:`validate_environment`."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def validate_environment() -> ValidationReport:
    """Check config sanity on startup.

    Errors are conditions that will break a core flow (e.g. crypto paywall on
    without a wallet). Warnings are degraded-but-runnable states (e.g. no LLM
    key — the service boots but deliberations will be empty).
    """
    report = ValidationReport()

    # LLM provider / key.
    provider = os.environ.get("LLM_PROVIDER", "openrouter").lower()
    key_env = {"openrouter": "OPENROUTER_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(
        provider, "OPENROUTER_API_KEY"
    )
    if not os.environ.get(key_env):
        report.warnings.append(
            f"No {key_env} set for LLM_PROVIDER={provider}; agent turns will be empty until configured."
        )

    # Crypto paywall coherence.
    if _flag("CRYPTO_PAYMENTS_ENABLED"):
        if not os.environ.get("CRYPTO_PAY_TO"):
            report.errors.append(
                "CRYPTO_PAYMENTS_ENABLED is on but CRYPTO_PAY_TO (receiving wallet) is not set."
            )

    # Redis reachability is validated lazily by the store; here we only flag a
    # malformed URL scheme early.
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url and not redis_url.startswith(("redis://", "rediss://", "unix://")):
        report.warnings.append(
            "REDIS_URL is set but does not look like a redis URL; falling back to disk storage."
        )

    # Auth on a public write surface.
    if not os.environ.get("PHANTOM_INTERNAL_SECRET") and not _flag("CRYPTO_PAYMENTS_ENABLED"):
        report.warnings.append(
            "No PHANTOM_INTERNAL_SECRET and no paywall: write endpoints are unprotected."
        )

    return report
