# phantom-bundler

A clean, high-agency command-line client for the **Phantom Automated AI
Bundler** ([phantom-swarm-engine](https://github.com/PhantomCapAI/phantom-swarm-engine)).

Describe an agent/swarm/workflow in plain language; the 20-agent hive designs,
critiques, and packages a drop-in **bundle** (prompts, skills, target configs,
examples, deploy helpers, and a runnable `run.py`). This CLI makes that
effortless from the terminal — local or remote.

```text
$ phantom-bundle create "A 3-agent code-review swarm: linter, security auditor, summarizer"
✓ bundle job started — session e8f85b5a
──── hive stream ────
◆ Orchestrator · Orchestrator  [design]
  [full mode] Designing bundle from spec: A 3-agent code-review swarm…
★ Orchestrator · Orchestrator  [package]
  Bundle 'Code Review Swarm' v0.1.0 ready — 24 files.
```

## Install

```bash
pip install phantom-bundler
```

This installs two equivalent commands: **`phantom-bundle`** and the short alias
**`pbundle`**. From a checkout of this repo:

```bash
pip install ./cli          # or:  cd cli && pip install -e .
```

Just the dependencies (no install):

```bash
pip install -r requirements-cli.txt
```

Requires Python 3.9+. Dependencies are deliberately minimal — `typer`, `httpx`,
`rich`, `tomli-w` — and version-aligned with the engine's own `requirements.txt`.

## Quickstart

```bash
# Point at a hosted engine (or let it auto-detect a local one on :8500/:8000)
phantom-bundle config set remote https://bundler.phantomcapital.live

# Create a bundle and watch the hive deliberate live
phantom-bundle create "A witty tweet-writer agent" --mode lite

# List, download, and run
phantom-bundle list
phantom-bundle download <session_id> -o . --unzip
phantom-bundle run ./witty-tweet-writer "draft a launch thread"
```

If the engine is running on this machine, you can skip `--remote` entirely —
the CLI probes `localhost:8500` and `:8000` and uses whichever answers as the
Phantom engine.

## Commands

| Command | What it does |
| --- | --- |
| `create "<spec>"` | Start a bundle from a spec, follow it live, optionally download it |
| `stream <session_id>` | Follow the live SSE hive stream of an in-flight bundle |
| `status <session_id>` | One-shot status + file list |
| `list` | List bundles persisted on the engine |
| `download <session_id>` | Download the zip (`--unzip` to extract) |
| `targets` | Show supported output targets |
| `ui` | Open the engine's web UI in a browser |
| `run <dir> "<task>"` | Run a generated bundle's `run.py` locally |
| `config show/set/path` | View and edit persisted defaults |
| `health` | Check the engine + whether its LLM is configured |

Run `phantom-bundle --help` or `phantom-bundle <command> --help` for full
details and examples.

### `create`

```bash
phantom-bundle create "A research assistant with a planner and a writer" \
  --mode full --agents 12 \
  --targets claude-code,cursor \
  --output ./research-bundle          # download + unzip when done

# Non-interactive / CI: skip the live stream, emit JSON
phantom-bundle create "..." --no-follow --json
```

| Flag | Meaning |
| --- | --- |
| `--mode lite\|full` | `lite` = 5 essential agents (fast/cheap); `full` (default) = choose the crew size |
| `--agents N` | Crew size in full mode (5–20) |
| `--targets a,b,c` | `claude-code`, `cursor`, `windsurf`, `langgraph`, `config` (omit to let the crew decide) |
| `--output DIR` / `-o` | Download + unzip the finished bundle into `DIR` |
| `--download` / `-d` | Download + unzip into the current directory (shorthand for `-o .`) |
| `--follow/--no-follow` | Stream the hive live (default: follow) |

### `run`

Execute a generated bundle's self-contained `run.py` (stdlib-only) with a task
prompt:

```bash
phantom-bundle run ./my-bundle "review the diff in main.py"
```

Set `OPENROUTER_API_KEY` or `DEEPSEEK_API_KEY` for real LLM output; without a
key the bundle prints an offline stub so you can see the agent wiring.

## Configuration

Settings resolve in this order (first match wins), so scripts always override
saved defaults:

1. an explicit CLI flag (`--remote`, `--mode`, …)
2. an environment variable
3. `~/.phantom-bundler/config.toml`
4. a built-in default

```bash
phantom-bundle config set remote https://bundler.phantomcapital.live
phantom-bundle config set mode lite
phantom-bundle config set targets claude-code,cursor
phantom-bundle config show
```

`~/.phantom-bundler/config.toml`:

```toml
remote = "https://bundler.phantomcapital.live"
mode = "full"
agents = 20
targets = ["claude-code", "cursor"]
payment_tx = ""
internal_secret = ""
```

### Environment variables

| Variable | Overrides |
| --- | --- |
| `PHANTOM_BUNDLER_REMOTE` | engine base URL |
| `PHANTOM_BUNDLER_MODE` | default mode |
| `PHANTOM_BUNDLER_AGENTS` | default crew size |
| `PHANTOM_BUNDLER_PAYMENT_TX` | Solana payment signature |
| `PHANTOM_INTERNAL_SECRET` | admin secret (`X-Phantom-Internal`) |
| `PHANTOM_BUNDLER_HOME` | config directory (default `~/.phantom-bundler`) |

## Paywall & auth

Some hosted engines protect `create` with an admin secret or an on-chain
payment:

```bash
# Admin (X-Phantom-Internal)
phantom-bundle --internal-secret "$PHANTOM_INTERNAL_SECRET" create "..."

# Crypto paywall — pay first, then pass the Solana signature
phantom-bundle --payment-tx <solana-signature> create "..."
```

If a paywall rejects the request, the CLI prints the accepting wallet, network,
and amounts so you know exactly what to send.

## JSON output

Every command accepts a global `--json` flag for machine-readable output —
handy in CI or when piping to `jq`. During `create`/`stream`, each hive event is
emitted as one newline-delimited JSON object.

```bash
phantom-bundle --json list | jq '.[0].session_id'
```

## Development

```bash
cd cli
pip install -e .
python -m unittest discover -s phantom_bundler/tests -v
```

Tests are network-free (httpx `MockTransport` + temp dirs).

## License

Source-available under the **Phantom Source-Available License v1.0** — see the
repository [`LICENSE`](../LICENSE). Commercial use requires a paid license;
contact `licensing@phantomcapital.live`.
