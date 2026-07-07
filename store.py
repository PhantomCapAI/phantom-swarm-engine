"""On-disk persistence for generated bundles.

In-memory sessions vanish on restart, which would take downloads with them.
This writes each completed bundle to ``BUNDLE_STORE_DIR`` as two files:

    <session_id>.zip    the packaged bundle
    <session_id>.json   metadata (blueprint + file map + timestamps)

Download/status endpoints fall back to disk when the live session is gone, so a
bundle stays retrievable across restarts. Storage is best-effort: a save failure
never breaks a job (the in-memory copy still serves the current process).
"""

import json
import os
from datetime import datetime, timezone

STORE_DIR = os.environ.get("BUNDLE_STORE_DIR", "bundle_store")


def _ensure() -> None:
    os.makedirs(STORE_DIR, exist_ok=True)


def _zip_path(session_id: str) -> str:
    return os.path.join(STORE_DIR, f"{session_id}.zip")


def _meta_path(session_id: str) -> str:
    return os.path.join(STORE_DIR, f"{session_id}.json")


def save_bundle(session_id: str, blueprint: dict, files: dict, zip_bytes: bytes) -> dict:
    """Persist a completed bundle. Returns the metadata dict written."""
    _ensure()
    with open(_zip_path(session_id), "wb") as f:
        f.write(zip_bytes)
    meta = {
        "session_id": session_id,
        "blueprint": blueprint,
        "files": files,
        "file_count": len(files),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_meta_path(session_id), "w") as f:
        json.dump(meta, f)
    return meta


def load_zip(session_id: str) -> bytes | None:
    """Return the persisted zip bytes, or None if not on disk."""
    path = _zip_path(session_id)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


def load_meta(session_id: str) -> dict | None:
    """Return the persisted metadata dict, or None if not on disk."""
    path = _meta_path(session_id)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def delete_bundle(session_id: str) -> bool:
    """Remove a persisted bundle's zip + metadata. Returns True if anything was
    removed (best-effort; a partial/missing file never raises)."""
    removed = False
    for path in (_zip_path(session_id), _meta_path(session_id)):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except Exception:
            pass
    return removed


def list_bundles() -> list[dict]:
    """Summaries of all persisted bundles, newest first."""
    _ensure()
    out = []
    for name in os.listdir(STORE_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(STORE_DIR, name)) as f:
                m = json.load(f)
        except Exception:
            continue
        bp = m.get("blueprint") or {}
        out.append(
            {
                "session_id": m.get("session_id"),
                "name": bp.get("name"),
                "version": bp.get("version"),
                "file_count": m.get("file_count"),
                "saved_at": m.get("saved_at"),
            }
        )
    out.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return out
