"""Configuration: ``~/.phantom-bundler/config.toml`` layered under env vars.

Resolution order (first non-empty wins), so scripting always overrides a saved
default:

    1. explicit CLI flag        (handled by the caller, not here)
    2. environment variable     (PHANTOM_BUNDLER_* / PHANTOM_INTERNAL_SECRET)
    3. config file value        (~/.phantom-bundler/config.toml)
    4. built-in default

The file is plain TOML so it stays hand-editable:

    remote = "https://bundler.phantomcapital.live"
    payment_tx = ""
    internal_secret = ""
    mode = "full"
    agents = 20
    targets = ["claude-code", "cursor"]
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# TOML read: stdlib on 3.11+, tomli backport otherwise.
try:  # pragma: no cover - trivial import shim
    import tomllib as _toml_read
except ModuleNotFoundError:  # pragma: no cover
    import tomli as _toml_read  # type: ignore

import tomli_w as _toml_write

CONFIG_DIR = Path(os.environ.get("PHANTOM_BUNDLER_HOME", Path.home() / ".phantom-bundler"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Sensible default when nothing else is configured — the common local port.
DEFAULT_REMOTE = "http://localhost:8500"

# Maps a config key to the env var that overrides it.
_ENV_OVERRIDES = {
    "remote": "PHANTOM_BUNDLER_REMOTE",
    "payment_tx": "PHANTOM_BUNDLER_PAYMENT_TX",
    "internal_secret": "PHANTOM_INTERNAL_SECRET",
    "mode": "PHANTOM_BUNDLER_MODE",
    "agents": "PHANTOM_BUNDLER_AGENTS",
}


def load_file() -> dict[str, Any]:
    """Return the raw config dict from disk (empty if missing/unreadable)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return _toml_read.load(f)
    except Exception:
        # A corrupt config should never brick the CLI — fall back to defaults.
        return {}


def save_file(data: dict[str, Any]) -> Path:
    """Persist the config dict to disk, creating the directory as needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "wb") as f:
        _toml_write.dump({k: v for k, v in data.items() if v is not None}, f)
    return CONFIG_PATH


class Config:
    """Resolved configuration with env > file > default precedence."""

    def __init__(self) -> None:
        self._file = load_file()

    def _resolve(self, key: str, default: Any = None) -> Any:
        env = _ENV_OVERRIDES.get(key)
        if env and os.environ.get(env):
            return os.environ[env]
        if key in self._file and self._file[key] not in (None, ""):
            return self._file[key]
        return default

    # --- typed accessors ------------------------------------------------- #
    @property
    def remote(self) -> str:
        return str(self._resolve("remote", DEFAULT_REMOTE)).rstrip("/")

    @property
    def payment_tx(self) -> str | None:
        return self._resolve("payment_tx") or None

    @property
    def internal_secret(self) -> str | None:
        return self._resolve("internal_secret") or None

    @property
    def mode(self) -> str:
        return str(self._resolve("mode", "full"))

    @property
    def agents(self) -> int | None:
        val = self._resolve("agents")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def targets(self) -> list[str] | None:
        val = self._file.get("targets")
        return list(val) if isinstance(val, list) and val else None

    def as_dict(self) -> dict[str, Any]:
        """Fully-resolved view, for `config show` / --json."""
        return {
            "remote": self.remote,
            "payment_tx": self.payment_tx,
            "internal_secret": "***" if self.internal_secret else None,
            "mode": self.mode,
            "agents": self.agents,
            "targets": self.targets,
            "config_path": str(CONFIG_PATH),
        }
