"""Local-machine helpers: auto-detect a running engine and run a bundle dir.

Two independent conveniences:

* :func:`detect_local_engine` — probe the usual localhost ports so a user who
  has the engine running doesn't have to pass ``--remote`` at all.
* :func:`run_bundle_dir` — execute a generated bundle's ``run.py`` in-place, so
  ``phantom-bundle run ./my-bundle "task"`` just works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx

from .errors import PhantomError

# Ports the engine is commonly served on (README uses 8500; uvicorn default 8000).
_LOCAL_CANDIDATES = [
    "http://localhost:8500",
    "http://127.0.0.1:8500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


def detect_local_engine(candidates: list[str] | None = None) -> str | None:
    """Return the base URL of a local engine that answers ``/health``, or None.

    We accept a host only if it self-identifies as the phantom swarm engine, so
    we never mistake some other local server for it.
    """
    for base in candidates or _LOCAL_CANDIDATES:
        try:
            r = httpx.get(f"{base}/health", timeout=0.75)
        except httpx.RequestError:
            continue
        if r.status_code != 200:
            continue
        try:
            body = r.json()
        except Exception:
            continue
        if body.get("engine") == "phantom-swarm" or "crew_size" in body:
            return base
    return None


def run_bundle_dir(bundle_dir: str | Path, task: str, *, python: str | None = None) -> int:
    """Run a generated bundle's ``run.py`` with ``task`` as its prompt.

    Returns the child process exit code. Raises :class:`PhantomError` when the
    directory isn't a valid bundle so the user gets a clear pointer.
    """
    path = Path(bundle_dir).expanduser().resolve()
    if not path.exists():
        raise PhantomError(
            f"bundle directory not found: {path}",
            hint="Point at an unzipped bundle, e.g.  phantom-bundle run ./my-bundle \"task\"",
        )

    run_py = _find_run_py(path)
    if run_py is None:
        raise PhantomError(
            f"no run.py found in {path}",
            hint=(
                "Every generated bundle ships a run.py at its root. If you passed "
                "a zip, unzip it first:  unzip my-bundle.zip -d my-bundle"
            ),
        )

    interpreter = python or sys.executable
    # Run from the bundle root so run.py finds agents.json next to it.
    proc = subprocess.run(
        [interpreter, str(run_py.name), task],
        cwd=str(run_py.parent),
    )
    return proc.returncode


def _find_run_py(path: Path) -> Path | None:
    """Locate run.py at the bundle root, or one level down (zip-with-slug case)."""
    direct = path / "run.py"
    if direct.exists():
        return direct
    # A downloaded zip often extracts into a single <slug>/ folder.
    subdirs = [p for p in path.iterdir() if p.is_dir()] if path.is_dir() else []
    if len(subdirs) == 1 and (subdirs[0] / "run.py").exists():
        return subdirs[0] / "run.py"
    return None
