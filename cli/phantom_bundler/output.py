"""Rich-powered terminal output — the Phantom aesthetic: gold on black, clean.

Everything that draws to the screen lives here so the command layer stays about
control flow. A single shared :data:`console` (and :data:`err_console` for
errors) keeps styling consistent, and honors ``NO_COLOR`` / non-TTY pipes.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__

# Phantom palette. Gold is the signature accent; the rest are quiet supports.
_THEME = Theme(
    {
        "phantom.gold": "#e6b800",
        "phantom.dim": "grey58",
        "phantom.ok": "#3ddc84",
        "phantom.warn": "#f2b134",
        "phantom.err": "#ff5c5c",
        "phantom.agent": "bold #e6b800",
        "phantom.role": "grey62",
    }
)

console = Console(theme=_THEME, highlight=False)
err_console = Console(theme=_THEME, stderr=True, highlight=False)

# Map an SSE message ``type`` to a glyph + style so the stream is scannable.
_TYPE_STYLE = {
    "decision": ("◆", "phantom.gold"),
    "consensus": ("★", "phantom.ok"),
    "tool_call": ("⚙", "phantom.dim"),
    "message": ("·", "phantom.role"),
}


def banner() -> None:
    """Small brand header for interactive commands."""
    if console.is_terminal:
        console.print(
            Text("PHANTOM", style="phantom.gold") + Text("  automated ai bundler", style="phantom.dim"),
        )


def rule(label: str = "") -> None:
    console.rule(Text(label, style="phantom.dim"), style="phantom.gold")


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #
def render_stream_event(msg: dict[str, Any]) -> None:
    """Print one SSE message as a colored, single-line hive utterance."""
    mtype = msg.get("type", "message")
    if mtype == "ping":
        return  # keep-alive; nothing to show

    glyph, style = _TYPE_STYLE.get(mtype, _TYPE_STYLE["message"])
    agent = msg.get("agent", "?")
    role = msg.get("role", "")
    phase = msg.get("phase") or msg.get("round")
    color = msg.get("color")

    # Prefer the agent's own hex color from the engine when present.
    agent_style = f"bold {color}" if _is_hex(color) else "phantom.agent"

    line = Text()
    line.append(f"{glyph} ", style=style)
    line.append(f"{agent}", style=agent_style)
    if role:
        line.append(f" · {role}", style="phantom.role")
    if phase not in (None, ""):
        line.append(f"  [{phase}]", style="phantom.dim")
    console.print(line)

    body = (msg.get("text") or "").strip()
    if body:
        console.print(Text(body, style="default"), style="", markup=False, soft_wrap=True)


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("#") and len(value) in (4, 7)


# --------------------------------------------------------------------------- #
# structured views
# --------------------------------------------------------------------------- #
def bundles_table(bundles: Iterable[dict], *, title: str = "Bundles") -> None:
    rows = list(bundles)
    if not rows:
        console.print(Text("No bundles found.", style="phantom.dim"))
        return
    table = Table(title=title, title_style="phantom.gold", border_style="phantom.dim", header_style="phantom.gold")
    table.add_column("session", style="phantom.gold", no_wrap=True)
    table.add_column("name")
    table.add_column("ver", style="phantom.dim")
    table.add_column("files", justify="right", style="phantom.dim")
    table.add_column("saved", style="phantom.dim")
    for b in rows:
        table.add_row(
            str(b.get("session_id", "—")),
            str(b.get("name") or "—"),
            str(b.get("version") or "—"),
            str(b.get("file_count") if b.get("file_count") is not None else "—"),
            _short_time(b.get("saved_at")),
        )
    console.print(table)


def targets_table(default: Iterable[str], available: Iterable[str], version: str | None = None) -> None:
    default_set = set(default)
    table = Table(
        title="Supported targets",
        title_style="phantom.gold",
        border_style="phantom.dim",
        header_style="phantom.gold",
    )
    table.add_column("target", style="phantom.gold")
    table.add_column("default", justify="center")
    for t in available:
        mark = Text("✓", style="phantom.ok") if t in default_set else Text("—", style="phantom.dim")
        table.add_row(t, mark)
    console.print(table)
    if version:
        console.print(Text(f"bundler version {version}", style="phantom.dim"))


def status_panel(status: dict) -> None:
    lines = Text()
    lines.append("session   ", style="phantom.dim")
    lines.append(f"{status.get('session_id', '—')}\n", style="phantom.gold")
    lines.append("status    ", style="phantom.dim")
    lines.append(f"{status.get('status', '—')}\n", style=_status_style(status.get("status")))
    if status.get("name"):
        lines.append("name      ", style="phantom.dim")
        lines.append(f"{status['name']} v{status.get('version', '?')}\n")
    lines.append("files     ", style="phantom.dim")
    lines.append(f"{status.get('file_count', 0)}\n")
    if status.get("targets"):
        lines.append("targets   ", style="phantom.dim")
        lines.append(f"{', '.join(status['targets'])}\n")
    if status.get("error"):
        lines.append("error     ", style="phantom.dim")
        lines.append(f"{status['error']}\n", style="phantom.err")
    console.print(Panel(lines, border_style="phantom.gold", expand=False))


def files_tree(files: Iterable[str]) -> None:
    for path in sorted(files):
        console.print(Text(f"  {path}", style="phantom.dim"))


def success(message: str) -> None:
    console.print(Text("✓ ", style="phantom.ok") + Text(message))


def info(message: str) -> None:
    console.print(Text("· ", style="phantom.dim") + Text(message, style="phantom.dim"))


def warn(message: str) -> None:
    console.print(Text("! ", style="phantom.warn") + Text(message, style="phantom.warn"))


def error(message: str, hint: str | None = None) -> None:
    err_console.print(Text("✗ ", style="phantom.err") + Text(message, style="phantom.err"))
    if hint:
        err_console.print(Text(hint, style="phantom.dim"))


def print_json(data: Any, *, compact: bool = False) -> None:
    """Machine-readable output (for --json). Always to stdout, no styling.

    ``compact=True`` emits a single line (NDJSON) — used per-event when following
    a stream so the output stays pipeable one-object-per-line.
    """
    import json

    if compact:
        sys.stdout.write(json.dumps(data, default=str) + "\n")
    else:
        sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def version_line() -> None:
    console.print(Text(f"phantom-bundler {__version__}", style="phantom.gold"))


# --------------------------------------------------------------------------- #
# tiny formatting helpers
# --------------------------------------------------------------------------- #
def _status_style(status: Any) -> str:
    return {
        "completed": "phantom.ok",
        "error": "phantom.err",
    }.get(str(status), "phantom.warn")


def _short_time(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    return text[:19].replace("T", " ")  # ISO -> "YYYY-MM-DD HH:MM:SS"
