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
- `POST /bundle/create` — start a bundling job (requires `X-Phantom-Internal`)
- `GET  /bundle/stream/{session_id}` — SSE stream of deliberation + generation
- `GET  /bundle/status/{session_id}` — status + file list
- `GET  /bundle/{session_id}/download` — download zip (`?format=manifest` for JSON)
- `GET  /bundle/targets` — supported output targets

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
  targets/config/        agents.json + agents.yaml
```

## Example

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"spec": "A 3-agent code-review swarm: a linter, a security auditor, and a summarizer. Targets: Claude Code and Cursor."}'
# -> {"session_id": "ab12cd34", "stream": "/bundle/stream/ab12cd34", ...}

curl -N http://localhost:8500/bundle/stream/ab12cd34          # watch live
curl -OJ http://localhost:8500/bundle/ab12cd34/download        # get the zip
```

Adding a new output target is one function in `bundler.py` (`TARGET_BUILDERS`).

## Paywall (optional)

Off by default. When enabled, `POST /bundle/create` requires payment (or the
`X-Phantom-Internal` admin secret). This charges people to use **your hosted
instance** — it does not touch users' own wallets/tokens or the bundles they
generate. Two providers; crypto takes precedence if both are set.

`GET /bundle/pricing` reports the active price/provider; the web UI at
`/bundle/ui` drives the whole flow.

### Crypto (Solana)

The operator sets the receiving wallet via `CRYPTO_PAY_TO` (not hardcoded — a
fork never silently pays someone else). Payment is a real on-chain transfer,
verified against a Solana RPC before any work runs, single-use per signature.

Flow: pay from your wallet → `POST /bundle/create` with `X-Payment-Tx: <signature>`.

```bash
CRYPTO_PAYMENTS_ENABLED=1
CRYPTO_PAY_TO=<your-solana-wallet>   # you set this
CRYPTO_PRICE=0.5
CRYPTO_ASSET=SOL                     # or an SPL mint address
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
```

### Stripe

Flow: `POST /bundle/checkout` → pay on Stripe → redirect back with the session id
→ `POST /bundle/create` with `X-Payment-Session: <id>` (verified `paid`, single-use).

```bash
BUNDLE_PAYMENTS_ENABLED=1
STRIPE_SECRET_KEY=sk_live_...
BUNDLE_PRICE=100
STRIPE_CURRENCY=usd
PUBLIC_BASE_URL=https://your-host    # for Stripe redirects
```
