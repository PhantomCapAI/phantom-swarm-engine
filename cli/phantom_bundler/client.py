"""Synchronous httpx client for the phantom-swarm-engine bundle API.

Thin, typed wrapper over the ``/bundle/*`` endpoints plus a line-based SSE
reader for the live hive stream. Sync (not async) on purpose: a CLI is a
straight-line script, and httpx's sync streaming is all we need.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .errors import ApiError, NotFound, PaymentRequired, RemoteUnreachable

# Bundling a full 20-agent hive is many sequential LLM calls — the stream can
# run for minutes. Generous read timeout; short connect timeout to fail fast on
# an unreachable host.
_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)


class BundlerClient:
    """Talks to a single engine base URL (e.g. ``http://localhost:8500``)."""

    def __init__(
        self,
        base_url: str,
        *,
        payment_tx: str | None = None,
        internal_secret: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._payment_tx = payment_tx
        self._internal_secret = internal_secret

    # --- helpers --------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._internal_secret:
            h["X-Phantom-Internal"] = self._internal_secret
        if self._payment_tx:
            h["X-Payment-Tx"] = self._payment_tx
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get_json(self, path: str) -> Any:
        try:
            r = httpx.get(self._url(path), headers=self._headers(), timeout=_TIMEOUT)
        except httpx.RequestError as e:
            raise RemoteUnreachable(self.base_url, str(e)) from e
        if r.status_code == 404:
            raise NotFound(_extract_error(r) or "not found on the engine")
        if r.status_code >= 400:
            raise ApiError(f"engine returned {r.status_code}: {_extract_error(r)}")
        return r.json()

    # --- endpoints ------------------------------------------------------- #
    def health(self) -> dict:
        return self._get_json("/health")

    def targets(self) -> dict:
        return self._get_json("/bundle/targets")

    def pricing(self) -> dict:
        return self._get_json("/bundle/pricing")

    def list_bundles(self) -> list[dict]:
        data = self._get_json("/bundle/list")
        return data.get("bundles", [])

    def status(self, session_id: str) -> dict:
        return self._get_json(f"/bundle/status/{session_id}")

    def create(
        self,
        spec: str,
        *,
        mode: str = "full",
        agents: int | None = None,
        targets: list[str] | None = None,
    ) -> dict:
        """Start a bundling job. Returns ``{session_id, status, stream, ...}``."""
        payload: dict[str, Any] = {"spec": spec, "mode": mode}
        if agents is not None:
            payload["agents"] = agents
        if targets:
            payload["targets"] = targets

        try:
            r = httpx.post(
                self._url("/bundle/create"),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=_TIMEOUT,
            )
        except httpx.RequestError as e:
            raise RemoteUnreachable(self.base_url, str(e)) from e

        if r.status_code == 402:
            body = _safe_json(r)
            raise PaymentRequired(
                body.get("error", "payment required"), body.get("pricing")
            )
        if r.status_code == 403:
            raise ApiError(
                "unauthorized — this engine requires an admin secret. "
                "Set it with  --internal-secret  or  PHANTOM_INTERNAL_SECRET."
            )
        if r.status_code >= 400:
            raise ApiError(f"create failed ({r.status_code}): {_extract_error(r)}")
        return r.json()

    def stream(self, session_id: str) -> Iterator[dict]:
        """Yield decoded SSE message dicts until the stream closes.

        Each yielded item is one parsed ``data:`` JSON object. ``{"type":"ping"}``
        keep-alives are yielded too so callers can show liveness if they want;
        most will filter them out.
        """
        url = self._url(f"/bundle/stream/{session_id}")
        try:
            with httpx.stream(
                "GET", url, headers=self._headers(), timeout=_TIMEOUT
            ) as r:
                if r.status_code == 404:
                    raise NotFound(f"no live stream for session {session_id}")
                if r.status_code >= 400:
                    r.read()
                    raise ApiError(
                        f"stream failed ({r.status_code}): {_extract_error(r)}"
                    )
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue
        except httpx.RequestError as e:
            raise RemoteUnreachable(self.base_url, str(e)) from e

    def download(self, session_id: str) -> tuple[bytes, str]:
        """Fetch the bundle zip. Returns ``(bytes, filename)``."""
        url = self._url(f"/bundle/{session_id}/download")
        try:
            r = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT)
        except httpx.RequestError as e:
            raise RemoteUnreachable(self.base_url, str(e)) from e
        if r.status_code == 404:
            raise NotFound(f"bundle {session_id} not found on the engine")
        if r.status_code == 409:
            raise ApiError(
                f"bundle {session_id} is not ready yet ({_extract_error(r)}). "
                "Wait for it to finish, then download again."
            )
        if r.status_code >= 400:
            raise ApiError(f"download failed ({r.status_code}): {_extract_error(r)}")
        filename = _filename_from_headers(r.headers) or f"{session_id}.zip"
        return r.content, filename

    def manifest(self, session_id: str) -> dict:
        """Fetch the raw ``{blueprint, files}`` manifest (no zip)."""
        return self._get_json(f"/bundle/{session_id}/download?format=manifest")


# --------------------------------------------------------------------------- #
# small response helpers
# --------------------------------------------------------------------------- #
def _safe_json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_error(r: httpx.Response) -> str:
    body = _safe_json(r)
    if body.get("error"):
        return str(body["error"])
    text = (r.text or "").strip()
    return text[:200] if text else ""


def _filename_from_headers(headers: httpx.Headers) -> str | None:
    disp = headers.get("content-disposition", "")
    if "filename=" in disp:
        name = disp.split("filename=", 1)[1].strip().strip('"')
        return name or None
    return None
