"""Dependency-free .env loader.

Imported first by main.py so a local .env populates the environment before any
other module reads config. Real env vars always win (a value already set is
never overwritten), so this is safe in production where vars come from the host.
No third-party dependency — just parses simple KEY=VALUE lines.
"""

import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:   # don't override real env
                    os.environ[key] = value
    except Exception:
        pass  # never let config loading crash startup


load_dotenv()
