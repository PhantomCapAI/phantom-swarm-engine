# phantom-swarm-engine

Real-time **20-agent hive-mind** deliberation engine + **Automated AI Bundler**.
Agents debate via OpenRouter, streamed to clients over SSE.

The engine does two things:

1. **Swarm deliberation** — the original 5-agent debate on any topic.
2. **AI Bundler** — describe an AI agent/swarm/workflow in natural language (or
   JSON) and the 20-agent hive designs, critiques, and optimizes a **Bundle**: a
   drop-in package of prompts, skills, target configs, examples, and deploy
   helpers for Claude Code, Cursor, and generic runtimes.

## The hive (20 agents)

5 core (Phoebe, Nova, Loom, Claire, Cipher) + 15 specialists (Atlas, Sable, Vex,
Juno, Echo, Rune, Pax, Quill, Iris, Onyx, Lyra, Mira, Kai, Dax, Wren), grouped
into pods: Architecture, Prompts & Content, Tooling & Integration, Quality &
Safety, Growth & Adoption. Phoebe orchestrates.

## Endpoints

### Bundler
- `POST /bundle/create` — start a bundling job (`X-Phantom-Internal`, or payment if the paywall is on)
- `GET  /bundle/stream/{session_id}` — SSE stream of deliberation + generation
- `GET  /bundle/status/{session_id}` — status + file list
- `GET  /bundle/{session_id}/download` — download zip (`?format=manifest` for JSON)
- `GET  /bundle/list` — list persisted bundles
- `GET  /bundle/targets` — supported output targets
- `GET  /bundle/pricing` — paywall price (or `{"enabled": false}`)
- `GET  /bundle/ui` — minimal web UI

### Modes

`POST /bundle/create` accepts a `mode`:

- `"full"` (default) — the whole **20-agent hive** deliberates. Richest result.
- `"lite"` — just the **original 5 agents** (Nova, Loom, Claire, Cipher, Phoebe).
  Fewer LLM calls, so noticeably **faster and cheaper**.

Failover is mode-scoped: a "lite" bundle never silently pulls in specialists.

### Swarm (unchanged)
- `POST /swarm/start` — start deliberation (requires `X-Phantom-Internal`)
- `GET  /swarm/stream/{session_id}` — SSE message stream
- `GET  /swarm/status/{session_id}` — session status
- `GET  /health` — health check

## Bundle contents

```
<slug>/
  README.md              manifest.json          bundle.yaml
  prompts/               optimized system prompt + personas
  skills/                runtime-agnostic skill definitions
  examples/              example usage + test cases
  deploy/                Dockerfile, docker-compose, zeabur, DEPLOY.md
  targets/claude-code/   CLAUDE.md + .claude/skills/<slug>/SKILL.md
  targets/cursor/        .cursor/rules/*.mdc
  targets/windsurf/      .windsurfrules + .windsurf/rules/*.md
  targets/config/        agents.json + agents.yaml
```

Targets: `claude-code`, `cursor`, `windsurf`, `config` (default) + `langgraph`
(runnable graph scaffold, opt-in via the spec's `targets`).

## Run it locally

```bash
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...          # required: the agents call OpenRouter
export PHANTOM_INTERNAL_SECRET=changeme      # optional: protects write endpoints
uvicorn main:app --host 0.0.0.0 --port 8500
```

Then open `http://localhost:8500/bundle/ui`, or use the API directly (below).

## Example

Create a bundle (full 20-agent hive), watch it stream, download the zip:

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"spec": "A 3-agent code-review swarm: a linter, a security auditor, and a summarizer. Targets: Claude Code and Cursor."}'
# -> {"session_id": "ab12cd34", "mode": "full", "stream": "/bundle/stream/ab12cd34", ...}

curl -N http://localhost:8500/bundle/stream/ab12cd34          # watch live (SSE)
curl -s http://localhost:8500/bundle/status/ab12cd34          # status + file list
curl -OJ http://localhost:8500/bundle/ab12cd34/download        # get the zip
```

Lite mode — same call, `"mode": "lite"` (original 5 agents, faster & cheaper):

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"spec": "A tweet-writer agent with a witty, concise voice.", "mode": "lite"}'
```

If the crypto paywall is on, drop the secret and pass a paid transaction instead:

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Payment-Tx: <solana-signature>" \
  -H "Content-Type: application/json" \
  -d '{"spec": "..."}'
```

Adding a new output target is one function in `bundler.py` (`TARGET_BUILDERS`).

## Tests

Pure-logic unit tests (no network / no LLM) covering blueprint normalization,
file generation, both deliberation modes, and the payment gate:

```bash
python -m unittest discover -s tests
```

## Paywall (optional, crypto) — OFF by default

> **Opt-in.** The paywall is fully disabled unless you set
> `CRYPTO_PAYMENTS_ENABLED=1` **and** `CRYPTO_PAY_TO`. With neither set, the
> engine behaves exactly as before (protected only by `X-Phantom-Internal`), and
> nothing crypto-related runs. Enabling it is entirely the operator's choice.

When enabled, `POST /bundle/create` requires an on-chain payment (or the
`X-Phantom-Internal` admin secret). This charges people to use **your hosted
instance** — it does not touch users' own wallets/tokens or the bundles they
generate. No KYC, no third party: just a wallet.

The operator sets the receiving wallet via `CRYPTO_PAY_TO` (not hardcoded — a
fork never silently pays someone else). Payment is a real Solana transfer,
verified against an RPC before any work runs, single-use per signature.

**Multi-asset:** accept SOL and/or USDC (each with its own price) via
`CRYPTO_ACCEPT`. The payer sends whichever they like; the server fetches the tx
once and accepts it if it paid any accepted asset to your wallet.

Flow: pay from your wallet → `POST /bundle/create` with `X-Payment-Tx: <signature>`.
The web UI at `/bundle/ui` shows the wallet + accepted amounts and takes the signature.

```bash
CRYPTO_PAYMENTS_ENABLED=1
CRYPTO_PAY_TO=<your-solana-wallet>       # you set this
CRYPTO_ACCEPT=SOL:0.5,USDC:75            # accept either; per-asset price
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
# custom SPL token: CRYPTO_ACCEPT="<MINT>:1000:6:MYTOKEN"
# legacy single-asset still works: CRYPTO_ASSET=SOL / CRYPTO_PRICE=0.5
```

- `GET /bundle/pricing` — accepted assets/prices + wallet, or `{"enabled": false}`
