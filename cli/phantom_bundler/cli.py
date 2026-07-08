"""phantom-bundle — command-line interface for the Phantom AI Bundler.

Typer app wiring the ``/bundle/*`` engine API to a clean set of commands:

    create   start a bundle from a spec (and follow it live)
    stream   follow an in-flight bundle's SSE hive stream
    status   one-shot status of a bundle
    list     list persisted bundles on the engine
    download fetch (and optionally unzip) a finished bundle
    targets  show supported output targets
    ui       open the web UI in a browser
    run      run a generated bundle's run.py locally
    config   view / set persisted defaults
    health   check engine + LLM status

Global flags (`--remote`, `--json`, `--payment-tx`, `--internal-secret`) work on
every command. Run `phantom-bundle --help` or `phantom-bundle <cmd> --help`.
"""

from __future__ import annotations

import sys
import webbrowser
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import typer
from rich.text import Text

from . import ALL_TARGETS, DEFAULT_TARGETS, __version__
from .client import BundlerClient
from .config import CONFIG_PATH, Config, load_file, save_file
from .errors import PhantomError
from .local import detect_local_engine, run_bundle_dir
from . import output as out

app = typer.Typer(
    name="phantom-bundle",
    help="Phantom Automated AI Bundler — create, stream, and run agent bundles.",
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Terminal states of a bundle job — used to stop polling/streaming loops.
_TERMINAL = {"completed", "error"}


# --------------------------------------------------------------------------- #
# shared context: resolves remote URL + auth once, reused by every command
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, remote: Optional[str], payment_tx: Optional[str],
                 internal_secret: Optional[str], as_json: bool, auto_detect: bool):
        self.cfg = Config()
        self.as_json = as_json
        self._explicit_remote = remote
        self._auto_detect = auto_detect
        self.payment_tx = payment_tx or self.cfg.payment_tx
        self.internal_secret = internal_secret or self.cfg.internal_secret
        self._remote: Optional[str] = None

    @property
    def remote(self) -> str:
        """Resolve the engine URL: explicit flag > auto-detected local > config."""
        if self._remote is not None:
            return self._remote
        if self._explicit_remote:
            self._remote = self._explicit_remote.rstrip("/")
            return self._remote
        # No explicit --remote: try to find a local engine before the default.
        if self._auto_detect:
            found = detect_local_engine()
            if found:
                if not self.as_json:
                    out.info(f"auto-detected local engine at {found}")
                self._remote = found
                return self._remote
        self._remote = self.cfg.remote
        return self._remote

    def client(self) -> BundlerClient:
        return BundlerClient(
            self.remote,
            payment_tx=self.payment_tx,
            internal_secret=self.internal_secret,
        )


def _get_ctx(ctx: typer.Context) -> Ctx:
    return ctx.obj  # set in the root callback


# --------------------------------------------------------------------------- #
# root callback — global options
# --------------------------------------------------------------------------- #
def _version_callback(value: bool) -> None:
    if value:
        out.version_line()
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    remote: Optional[str] = typer.Option(
        None, "--remote", "-r", help="Engine base URL (e.g. https://host:8500). "
        "Defaults to an auto-detected local engine, then config.",
        envvar="PHANTOM_BUNDLER_REMOTE",
    ),
    payment_tx: Optional[str] = typer.Option(
        None, "--payment-tx", help="Solana tx signature for a paywalled engine.",
    ),
    internal_secret: Optional[str] = typer.Option(
        None, "--internal-secret", help="Admin secret (X-Phantom-Internal).",
        envvar="PHANTOM_INTERNAL_SECRET",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Machine-readable JSON output.",
    ),
    no_detect: bool = typer.Option(
        False, "--no-detect", help="Don't probe localhost for a running engine.",
    ),
    _version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    ctx.obj = Ctx(remote, payment_tx, internal_secret, as_json, auto_detect=not no_detect)


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
@app.command()
def create(
    ctx: typer.Context,
    spec: str = typer.Argument(..., help="Natural-language (or JSON) spec of the bundle."),
    mode: str = typer.Option("full", "--mode", "-m", help="'lite' (fast/cheap) or 'full'."),
    agents: Optional[int] = typer.Option(None, "--agents", "-a", help="Crew size in full mode (5-20)."),
    targets: Optional[str] = typer.Option(
        None, "--targets", "-t", help="Comma list: claude-code,cursor,windsurf,langgraph,config.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Download + unzip the finished bundle into this dir.",
    ),
    download: bool = typer.Option(
        False, "--download", "-d", help="Download + unzip the finished bundle into the current dir.",
    ),
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Stream the hive live."),
) -> None:
    """Create a bundle from a spec, follow it live, and optionally download it.

    [bold]Examples[/bold]
      phantom-bundle create "A 3-agent code-review swarm"
      phantom-bundle create "Tweet-writer with a witty voice" --mode lite
      phantom-bundle create "Research assistant" -a 12 -t claude-code,cursor -o ./out
      phantom-bundle create "A planner+writer duo" --download        # grab the zip here
    """
    c = _get_ctx(ctx)
    mode = mode.lower()
    if mode not in ("lite", "full"):
        raise PhantomError("mode must be 'lite' or 'full'")
    target_list = _parse_targets(targets)
    # --download is shorthand for "--output ." — resolve to a single dest dir.
    dest = output if output is not None else (Path(".") if download else None)

    client = c.client()
    if not c.as_json:
        out.banner()
        out.info(f"engine: {c.remote}  ·  mode: {mode}"
                 + (f"  ·  agents: {agents}" if agents else ""))

    # A spinner while the create request is in flight — the hive is waking up.
    if c.as_json:
        resp = client.create(spec, mode=mode, agents=agents, targets=target_list)
    else:
        with out.spinner("summoning the hive…"):
            resp = client.create(spec, mode=mode, agents=agents, targets=target_list)
    session_id = resp["session_id"]

    if c.as_json and not follow:
        out.print_json(resp)
        return
    if not c.as_json:
        out.success(f"bundle job started — session {session_id}")

    final_status = resp.get("status", "started")
    if follow:
        final_status = _follow_stream(client, session_id, as_json=c.as_json)

    # Fetch authoritative final status (stream close != disk-persist done).
    status = _safe_status(client, session_id) or {"session_id": session_id, "status": final_status}

    if dest is not None and status.get("status") == "completed":
        _download_and_maybe_unzip(client, session_id, dest, unzip=True, as_json=c.as_json)

    if c.as_json:
        # When following, events were NDJSON — keep the summary on one line too.
        out.print_json({"session_id": session_id, **status}, compact=follow)
    else:
        out.rule("result")
        out.status_panel(status)
        if status.get("status") == "completed":
            out.info(f"download:  phantom-bundle download {session_id} -o .")


# --------------------------------------------------------------------------- #
# stream
# --------------------------------------------------------------------------- #
@app.command()
def stream(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id from `create`."),
) -> None:
    """Follow the live SSE hive stream for an in-flight bundle."""
    c = _get_ctx(ctx)
    client = c.client()
    if not c.as_json:
        out.banner()
    final = _follow_stream(client, session_id, as_json=c.as_json)
    if c.as_json:
        out.print_json({"session_id": session_id, "status": final})


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@app.command()
def status(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id to inspect."),
) -> None:
    """Show the current status + file list of a bundle."""
    c = _get_ctx(ctx)
    data = c.client().status(session_id)
    if c.as_json:
        out.print_json(data)
        return
    out.status_panel(data)
    if data.get("files"):
        out.rule("files")
        out.files_tree(data["files"])


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
@app.command(name="list")
def list_cmd(ctx: typer.Context) -> None:
    """List bundles persisted on the engine (newest first)."""
    c = _get_ctx(ctx)
    bundles = c.client().list_bundles()
    if c.as_json:
        out.print_json(bundles)
        return
    out.bundles_table(bundles, title=f"Bundles @ {c.remote}")


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
@app.command()
def download(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id of a completed bundle."),
    output: Path = typer.Option(Path("."), "--output", "-o", help="Directory to save into."),
    unzip: bool = typer.Option(False, "--unzip/--no-unzip", help="Extract into <output>/<slug>/."),
) -> None:
    """Download a finished bundle's zip (optionally extracting it)."""
    c = _get_ctx(ctx)
    if c.as_json:
        result = _download_and_maybe_unzip(c.client(), session_id, output, unzip=unzip, as_json=True)
        out.print_json(result)
    else:
        with out.spinner(f"fetching {session_id}…"):
            result = _download_and_maybe_unzip(c.client(), session_id, output, unzip=unzip, as_json=False)


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
@app.command()
def targets(ctx: typer.Context) -> None:
    """Show supported output targets (from the engine, or built-in defaults)."""
    c = _get_ctx(ctx)
    try:
        data = c.client().targets()
        default = data.get("targets", DEFAULT_TARGETS)
        available = data.get("available", ALL_TARGETS)
        version = data.get("bundler_version")
    except PhantomError:
        # Offline: fall back to the values baked into this package.
        default, available, version = DEFAULT_TARGETS, ALL_TARGETS, None
        if not c.as_json:
            out.warn("engine unreachable — showing built-in target list")
    if c.as_json:
        out.print_json({"targets": default, "available": available, "bundler_version": version})
        return
    out.targets_table(default, available, version)


# --------------------------------------------------------------------------- #
# ui
# --------------------------------------------------------------------------- #
@app.command()
def ui(ctx: typer.Context) -> None:
    """Open the engine's web UI (/bundle/ui) in a browser."""
    c = _get_ctx(ctx)
    url = f"{c.remote}/bundle/ui"
    if c.as_json:
        out.print_json({"ui": url})
        return
    out.info(f"opening {url}")
    if not webbrowser.open(url):
        out.warn("couldn't launch a browser — open this URL manually:")
        out.console.print(Text(url, style="phantom.gold"))


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@app.command()
def run(
    ctx: typer.Context,
    bundle_dir: Path = typer.Argument(..., help="Path to an unzipped bundle directory."),
    task: str = typer.Argument(..., help="Task prompt for the agent runtime."),
) -> None:
    """Run a generated bundle's run.py locally with a task prompt.

    [bold]Example[/bold]
      phantom-bundle run ./code-review-swarm "review the diff in main.py"

    Set OPENROUTER_API_KEY or DEEPSEEK_API_KEY for real output; without a key
    the bundle prints an offline stub so you can see the agent wiring.
    """
    c = _get_ctx(ctx)
    if not c.as_json:
        out.info(f"running {bundle_dir} · task: {task[:60]}")
        out.rule()
    code = run_bundle_dir(bundle_dir, task)
    if c.as_json:
        out.print_json({"bundle_dir": str(bundle_dir), "exit_code": code})
    raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
config_app = typer.Typer(help="View and edit persisted defaults.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show the resolved configuration (env > file > default)."""
    c = _get_ctx(ctx)
    data = c.cfg.as_dict()
    if c.as_json:
        out.print_json(data)
        return
    out.rule(f"config · {CONFIG_PATH}")
    for k, v in data.items():
        out.console.print(Text(f"  {k:<16}", style="phantom.dim") + Text(str(v)))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="remote | payment_tx | internal_secret | mode | agents | targets"),
    value: str = typer.Argument(..., help="New value. For targets use a comma list."),
) -> None:
    """Persist a default to the config file."""
    allowed = {"remote", "payment_tx", "internal_secret", "mode", "agents", "targets"}
    if key not in allowed:
        raise PhantomError(f"unknown key '{key}'", hint=f"valid keys: {', '.join(sorted(allowed))}")
    data = load_file()
    if key == "agents":
        data[key] = int(value)
    elif key == "targets":
        data[key] = _parse_targets(value) or []
    else:
        data[key] = value
    path = save_file(data)
    out.success(f"set {key} — saved to {path}")


@config_app.command("path")
def config_path() -> None:
    """Print the config file path."""
    out.console.print(str(CONFIG_PATH))


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
@app.command()
def health(ctx: typer.Context) -> None:
    """Check the engine is up and whether its LLM is configured."""
    c = _get_ctx(ctx)
    data = c.client().health()
    if c.as_json:
        out.print_json(data)
        return
    ok = data.get("status") == "alive"
    (out.success if ok else out.warn)(f"engine {c.remote} — {data.get('status', '?')}")
    out.info(f"provider: {data.get('llm_provider', '?')}  ·  "
             f"configured: {data.get('llm_configured')}  ·  crew: {data.get('crew_size', '?')}")


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _parse_targets(raw: Optional[str]) -> Optional[list[str]]:
    if not raw:
        return None
    items = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in items if t not in ALL_TARGETS]
    if unknown:
        raise PhantomError(
            f"unknown target(s): {', '.join(unknown)}",
            hint=f"valid targets: {', '.join(ALL_TARGETS)}",
        )
    return items or None


def _follow_stream(client: BundlerClient, session_id: str, *, as_json: bool) -> str:
    """Consume the SSE stream to completion. Returns the last-seen status hint.

    In JSON mode each event is emitted as one JSON line (newline-delimited) so
    it stays pipeable; otherwise events render as styled hive utterances.
    """
    saw_error = False
    saw_consensus = False
    if not as_json:
        out.rule("hive stream")
    for msg in client.stream(session_id):
        if msg.get("type") == "ping":
            continue
        # The engine signals failure with a message emitted in the 'error' phase,
        # and success with a final 'consensus'-type event.
        if msg.get("phase") == "error":
            saw_error = True
        if msg.get("type") == "consensus":
            saw_consensus = True
        if as_json:
            out.print_json(msg, compact=True)  # NDJSON: one event per line
        else:
            out.render_stream_event(msg)
    if saw_error:
        return "error"
    return "completed" if saw_consensus else "unknown"


def _safe_status(client: BundlerClient, session_id: str) -> Optional[dict]:
    try:
        return client.status(session_id)
    except PhantomError:
        return None


def _download_and_maybe_unzip(
    client: BundlerClient, session_id: str, output: Path, *, unzip: bool, as_json: bool
) -> dict:
    data, filename = client.download(session_id)
    output = output.expanduser()
    output.mkdir(parents=True, exist_ok=True)

    zip_path = output / filename
    zip_path.write_bytes(data)
    result = {"session_id": session_id, "zip": str(zip_path), "bytes": len(data)}

    if unzip:
        slug = Path(filename).stem
        dest = output / slug
        with zipfile.ZipFile(BytesIO(data)) as zf:
            zf.extractall(dest)
        result["extracted_to"] = str(dest)

    if not as_json:
        out.success(f"saved {zip_path}  ({len(data):,} bytes)")
        if unzip:
            out.info(f"extracted to {result['extracted_to']}")
            out.info(f"run it:  phantom-bundle run {result['extracted_to']} \"your task\"")
    return result


def entrypoint() -> None:
    """Console-script entry point with unified, friendly error handling."""
    try:
        app()
    except PhantomError as e:
        out.error(e.message, e.hint)
        sys.exit(1)
    except KeyboardInterrupt:
        out.error("interrupted")
        sys.exit(130)


if __name__ == "__main__":  # python -m phantom_bundler.cli
    entrypoint()
