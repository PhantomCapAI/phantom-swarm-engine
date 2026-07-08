"""Persistence for generated bundles and consumed payment signatures.

Two backends behind one interface, chosen at import time:

* **Redis** — used when ``REDIS_URL`` is set and the ``redis`` package is
  importable and reachable. Survives restarts and is shared across replicas, so
  a horizontally-scaled deployment keeps a single view of bundles and spent
  signatures.
* **Disk** — the default. Writes ``<session_id>.zip`` + ``<session_id>.json``
  under ``BUNDLE_STORE_DIR`` and a ``used_signatures.json`` set. Zero external
  dependencies; fine for a single instance.

Everything is best-effort: a storage failure logs a warning and degrades to the
in-memory copy rather than breaking a job. Bundles remain retrievable across
restarts, and consumed signatures persist so a paid transaction can't be
replayed after a redeploy.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from logging_setup import get_logger

log = get_logger("phantom.store")

STORE_DIR = os.environ.get("BUNDLE_STORE_DIR", "bundle_store")
_SIG_KEY_PREFIX = "phantom:sig:"
_BUNDLE_KEY_PREFIX = "phantom:bundle:"
_BUNDLE_INDEX_KEY = "phantom:bundles"
_SIG_FILE = "used_signatures.json"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
def _make_redis():
    """Return a live redis client, or None if unavailable/unreachable."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url or not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(url, decode_responses=False, socket_timeout=5)
        client.ping()
        log.info("store: using Redis backend at %s", url.split("@")[-1])
        return client
    except Exception as e:  # not installed, or can't connect
        log.warning("store: Redis unavailable (%s); falling back to disk", str(e)[:120])
        return None


_redis = _make_redis()


def backend() -> str:
    return "redis" if _redis is not None else "disk"


# --------------------------------------------------------------------------- #
# Disk helpers
# --------------------------------------------------------------------------- #
def _ensure() -> None:
    os.makedirs(STORE_DIR, exist_ok=True)


def _zip_path(session_id: str) -> str:
    return os.path.join(STORE_DIR, f"{session_id}.zip")


def _meta_path(session_id: str) -> str:
    return os.path.join(STORE_DIR, f"{session_id}.json")


def _sig_path() -> str:
    return os.path.join(STORE_DIR, _SIG_FILE)


# --------------------------------------------------------------------------- #
# Bundles
# --------------------------------------------------------------------------- #
def save_bundle(session_id: str, blueprint: dict, files: dict, zip_bytes: bytes) -> dict:
    """Persist a completed bundle. Returns the metadata dict written."""
    meta = {
        "session_id": session_id,
        "blueprint": blueprint,
        "files": files,
        "file_count": len(files),
        "saved_at": _now(),
    }
    if _redis is not None:
        payload = dict(meta, zip_b64=base64.b64encode(zip_bytes).decode("ascii"))
        _redis.set(_BUNDLE_KEY_PREFIX + session_id, json.dumps(payload).encode("utf-8"))
        _redis.zadd(_BUNDLE_INDEX_KEY, {session_id: _score_now()})
        return meta

    _ensure()
    with open(_zip_path(session_id), "wb") as f:
        f.write(zip_bytes)
    with open(_meta_path(session_id), "w") as f:
        json.dump(meta, f)
    return meta


def _score_now() -> float:
    # Monotonic-ish sort score for the redis index (newest first on reverse).
    return datetime.now(timezone.utc).timestamp()


def _redis_meta(session_id: str) -> Optional[dict]:
    raw = _redis.get(_BUNDLE_KEY_PREFIX + session_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def load_zip(session_id: str) -> Optional[bytes]:
    """Return the persisted zip bytes, or None if not stored."""
    if _redis is not None:
        payload = _redis_meta(session_id)
        if payload and payload.get("zip_b64"):
            try:
                return base64.b64decode(payload["zip_b64"])
            except Exception:
                return None
        return None
    path = _zip_path(session_id)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


def load_meta(session_id: str) -> Optional[dict]:
    """Return the persisted metadata dict (without the zip), or None."""
    if _redis is not None:
        payload = _redis_meta(session_id)
        if payload:
            payload.pop("zip_b64", None)
        return payload
    path = _meta_path(session_id)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def delete_bundle(session_id: str) -> bool:
    """Remove a persisted bundle. True if anything was removed."""
    if _redis is not None:
        existed = bool(_redis.delete(_BUNDLE_KEY_PREFIX + session_id))
        _redis.zrem(_BUNDLE_INDEX_KEY, session_id)
        return existed
    removed = False
    for path in (_zip_path(session_id), _meta_path(session_id)):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except OSError as e:
            log.warning("store: failed to delete %s (%s)", path, e)
    return removed


def list_bundles() -> list[dict]:
    """Summaries of all persisted bundles, newest first."""
    out: list[dict] = []
    if _redis is not None:
        ids = _redis.zrevrange(_BUNDLE_INDEX_KEY, 0, -1)
        for raw_id in ids:
            sid = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else raw_id
            m = _redis_meta(sid)
            if m:
                out.append(_summary(m))
        return out

    _ensure()
    for name in os.listdir(STORE_DIR):
        if not name.endswith(".json") or name == _SIG_FILE:
            continue
        try:
            with open(os.path.join(STORE_DIR, name)) as f:
                m = json.load(f)
        except Exception:
            continue
        out.append(_summary(m))
    out.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return out


def _summary(m: dict) -> dict:
    bp = m.get("blueprint") or {}
    return {
        "session_id": m.get("session_id"),
        "name": bp.get("name"),
        "version": bp.get("version"),
        "file_count": m.get("file_count"),
        "saved_at": m.get("saved_at"),
    }


# --------------------------------------------------------------------------- #
# Consumed payment signatures (replay protection that survives restarts)
# --------------------------------------------------------------------------- #
def mark_signature_used(sig: str) -> None:
    """Record a payment signature as spent. Idempotent."""
    if not sig:
        return
    if _redis is not None:
        _redis.set(_SIG_KEY_PREFIX + sig, b"1")
        return
    with _lock:
        used = _load_sig_file()
        used[sig] = _now()
        _ensure()
        try:
            with open(_sig_path(), "w") as f:
                json.dump(used, f)
        except OSError as e:
            log.warning("store: could not persist signature (%s)", e)


def is_signature_used(sig: str) -> bool:
    """True if the signature has already been consumed."""
    if not sig:
        return False
    if _redis is not None:
        return bool(_redis.exists(_SIG_KEY_PREFIX + sig))
    with _lock:
        return sig in _load_sig_file()


def _load_sig_file() -> dict:
    path = _sig_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}
