# phantom-swarm-engine

Real-time **20-agent hive-mind** deliberation engine + **Automated AI Bundler**.
Agents debate via OpenRouter, streamed to clients over SSE.

The engine does two things:

1. **Swarm deliberation** — the original 5-agent debate on any topic.
2. **AI Bundler** — describe an AI agent/swarm/workflow in natural language (or
   JSON) and the 20-agent hive designs, critiques, and optimizes a **Bundle**: a
   drop-in package of prompts, skills, target configs, examples, and deploy
   helpers for Claude Code, Cursor, and generic runtimes.

## Safety & Responsibility

The bundler can generate agents that **move real money on-chain** (see the
pump.fun / Solana tooling below). The generated tools ship with defense-in-depth
— `LAUNCH_DRY_RUN=1` is the default (nothing is submitted), plus hard spend and
slippage caps and a kill-switch — but those are guardrails, **not a guarantee**.

By running this engine or any bundle it produces, **you** are solely responsible
for:

- **Keys** — your Solana private keys (never commit them; the tools sign locally
  and never transmit or log keys).
- **RPC** — the endpoint you point at and its reliability/rate limits.
- **Budget** — every lamport spent; set `MAX_SPEND_SOL` / `DAILY_SPEND_SOL`
  conservatively and test with `LAUNCH_DRY_RUN=1` first.
- **Compliance** — the legality of launching/trading tokens and any market
  conduct (e.g. bundled buys use *your own* wallets and must not be used to fake
  organic volume or harm other traders) in your jurisdiction.

Nothing here is financial advice. Verify third-party endpoints (PumpPortal,
IPFS) against their current docs before mainnet use.

## The bundler crew

A purpose-built crew where **each agent owns exactly one stage** of the pipeline,
in order: **Orchestrator** (leads) → Interpreter → Architect → Prompt Smith →
Tooler → Target Mapper → Exemplar → Guardian → Scribe → Packager, plus
single-focus reviewers (Critic, Namer, Optimizer, Integrator, Inspector,
Evaluator, Observer, Standardizer, Promoter, Guide) that scale a "full" run out
to 20. Grouped for the stream into: Interpret & Design, Build, Quality & Safety,
Docs & Package.

The original 5 Phantom agents (Nova, Loom, Claire, Cipher, Phoebe) remain for the
`/swarm` endpoints.

## Endpoints

### Bundler
- `POST /bundle/create` — start a bundling job (`X-Phantom-Internal`, or payment if the paywall is on)
- `GET  /bundle/estimate` — pre-run cost estimate (`?mode=&agents=&tier=`)
- `GET  /bundle/stream/{session_id}` — SSE stream of deliberation + generation
- `GET  /bundle/status/{session_id}` — status + file list + usage/cost
- `GET  /bundle/{session_id}/download` — download zip (`?format=manifest` for JSON)
- `GET  /bundle/list` — list persisted bundles
- `DELETE /bundle/{session_id}` — delete a bundle (admin, `X-Phantom-Internal`)
- `GET  /bundle/targets` — supported output targets
- `GET  /bundle/pricing` — paywall price (or `{"enabled": false}`)
- `GET  /bundle/ui` — minimal web UI

### Ops
- `GET  /health` — liveness + config (`?deep=1` also probes real LLM connectivity)
- `GET  /health/llm` — dedicated deep check (does a tiny real completion; 503 if the LLM is unreachable)

Every response carries an `X-Request-ID` (honoring an inbound one) that also tags
every log line for that request. Logs are structured (set `LOG_JSON=1` for JSON).
Heavy endpoints are rate-limited (see `RATE_LIMIT_*` env vars).

### Modes

`POST /bundle/create` accepts a `mode` (and, in full mode, a crew size):

- `"lite"` — a **fixed set of 5 essential agents**. Fewest LLM calls; **fastest
  and cheapest**.
- `"full"` (default) — **you choose how many agents** via `"agents": N`
  (5–20, default 20). The crew fills from its core stages outward, so a smaller
  N still covers every pipeline step.

```json
{"spec": "...", "mode": "full", "agents": 10, "tier": "standard", "targets": ["claude-code", "cursor"]}
```

**Model tier** (`tier`, full or lite): `"economy"` runs everything on the cheap
fleet model; `"standard"` (default) uses the premium model for the JSON/leader
steps and fleet for critiques; `"premium"` also upgrades prompt optimization.

Failover is mode-scoped: a "lite" bundle never silently pulls in extra agents.
`targets` is optional — omit it to let the crew choose. The web UI has a target
picker, browses past bundles, and views generated files inline (no unzip).

### Cost expectations

Bundles are cheap. Cost is dominated by a few premium leader calls (design,
refine, optimize) plus one fleet critique per agent. Ask before you run:

```bash
curl -s "http://localhost:8500/bundle/estimate?mode=full&agents=20&tier=standard"
# -> {"estimated_cost_usd": 0.03, "estimated_llm_calls": 22, "breakdown": {...}, ...}
```

Ballpark with the default OpenRouter models (Sonnet premium + Llama-3.3 fleet):

| Run | LLM calls | Typical cost |
| --- | --- | --- |
| `lite` (5 agents) | ~7 | ~$0.01 |
| `full`, 10 agents | ~12 | ~$0.02 |
| `full`, 20 agents, standard | ~22 | ~$0.03 |
| `full`, 20 agents, economy | ~22 | ~$0.005 |

These are indicative — the actual figure depends on the models you configure and
your spec size. Tune the rate model with `COST_PREMIUM_PER_MTOK` /
`COST_FLEET_PER_MTOK`. Each completed bundle's real token usage and cost are on
`GET /bundle/status/{id}` under `usage`.

### Swarm (unchanged)
- `POST /swarm/start` — start deliberation (requires `X-Phantom-Internal`)
- `GET  /swarm/stream/{session_id}` — SSE message stream
- `GET  /swarm/status/{session_id}` — session status
- `GET  /health` — health check

## Bundle contents

```
<slug>/
  run.py                 runnable multi-agent app (stdlib only)
  agents.json            runtime agent config
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
(runnable graph scaffold) and `solana-launch` (pump.fun tool suite — auto-added
for launch specs). Opt into extras via the spec's `targets`.

## Run it locally

```bash
pip install -r requirements.txt

# Config: copy the template and fill in your keys (.env is gitignored and
# auto-loaded at startup — real env vars still take precedence):
cp .env.example .env
#   set LLM_PROVIDER + the matching key (OPENROUTER_API_KEY or DEEPSEEK_API_KEY)

uvicorn main:app --host 0.0.0.0 --port 8500
```

Or export the vars directly instead of using `.env`:

```bash
export LLM_PROVIDER=deepseek DEEPSEEK_API_KEY=sk-...   # or openrouter + its key
```

`GET /health` reports the active `llm_provider` and whether it's `llm_configured`.
Per-tier models are overridable (e.g. `DEEPSEEK_PREMIUM_MODEL`,
`OPENROUTER_FLEET_MODEL`). Then open `http://localhost:8500/bundle/ui`, or use
the API directly (below).

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

## CLI

A dedicated command-line client lives in [`cli/`](cli/) — `phantom-bundler` —
for effortless local/offline and remote use. It wraps the `/bundle/*` API with a
clean set of commands, rich streaming output, and a config file.

```bash
pip install phantom-bundler          # installs `phantom-bundle` (alias: `pbundle`)
# from this repo:  pip install ./cli   (deps only: pip install -r requirements-cli.txt)

# Auto-detects a local engine on :8500/:8000, or point at a hosted one:
phantom-bundle config set remote https://bundler.phantomcapital.live

phantom-bundle create "A 3-agent code-review swarm" --mode full --agents 12 --download
phantom-bundle list
phantom-bundle download <session_id> -o . --unzip
phantom-bundle run ./code-review-swarm "review the diff in main.py"
```

| Command | Purpose |
| --- | --- |
| `create "<spec>"` | Start a bundle, follow the hive live, optionally download it |
| `stream <id>` | Follow an in-flight bundle's SSE stream |
| `status <id>` / `list` | Inspect one bundle / list persisted bundles |
| `download <id>` | Fetch the zip (`--unzip` to extract) |
| `targets` / `ui` / `health` | Show targets · open the web UI · check the engine |
| `run <dir> "<task>"` | Run a generated bundle's `run.py` locally |

Global flags work on every command: `--remote`, `--json` (machine-readable),
`--payment-tx` (paywall), `--internal-secret` (admin). Full docs and examples in
[`cli/README.md`](cli/README.md).

## Getting Started with Pump.fun Agents

**Quickstart** — generate a full autonomous launcher in one call. Recommended
spec: [`examples/pumpfun_launcher.json`](examples/pumpfun_launcher.json).

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  --data @examples/pumpfun_launcher.json
# -> {"session_id": "ab12cd34", ...}. Then download the zip:
curl -OJ http://localhost:8500/bundle/ab12cd34/download
```

> 🛟 **Safety first.** The generated tools default to **`LAUNCH_DRY_RUN=1`** — no
> transaction is submitted until you explicitly turn it off — and enforce
> `MAX_SPEND_SOL` / `MAX_SLIPPAGE_PCT` caps plus a kill-switch. Keep dry-run on
> until you've reviewed the output. See [Safety & Responsibility](#safety--responsibility).

When a spec is about **pump.fun / Solana token launches**, the bundler detects
the domain and enriches the output with a `solana-launch` target: a suite of
**runnable, copy-paste-ready** Python tools your agent can actually call — not
prose. It also injects launch/monitor/risk **skills** into `skills/` and the
Claude Code `SKILL.md` files, plus concrete launch examples.

The emitted `targets/solana-launch/` contains:

| File | What it does |
| --- | --- |
| `pumpportal_client.py` | `create_token` / `buy` / `sell` via PumpPortal's **Local Transaction API** — you sign locally, keys never leave the box |
| `ipfs_metadata.py` | Upload image + metadata to the pump.fun IPFS endpoint → `metadataUri` |
| `wallet.py` | Load a Solana keypair (base58/JSON), balances, local signing |
| `launch_strategy.py` | Dev-buy sizing, timing, multi-wallet **bundled** opening buys |
| `monitor.py` | Post-launch watch → take-profit / stop-loss / anomaly exit |
| `risk_controls.py` | Hard caps (`MAX_SPEND_SOL`, `MAX_SLIPPAGE_PCT`), **dry-run**, kill-switch — every action fails closed |
| `tools.json` | OpenAI/Anthropic-style tool schemas mapping 1:1 to the functions above |
| `example_launch.py` | End-to-end launch example (runs in `LAUNCH_DRY_RUN=1`) |

The tools target PumpPortal's documented **Local** API (no custody, no trading
fee on local; you pay network/priority fees). Wire the functions to your agent's
tool-calling layer using `tools.json`. **Verify endpoints against current
PumpPortal docs before mainnet use — and start with `LAUNCH_DRY_RUN=1`.**

### Example: an autonomous pump.fun token launcher

Send this spec (also in [`examples/pumpfun_launcher.json`](examples/pumpfun_launcher.json)):

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  --data @examples/pumpfun_launcher.json
```

```json
{
  "spec": "An autonomous pump.fun token launcher agent. Given a token name, symbol, description, and image, it: (1) uploads the image + metadata to IPFS, (2) creates the token on pump.fun via the PumpPortal Local Transaction API with a configurable dev buy, signing locally so keys never leave the machine, (3) optionally fires bundled opening buys across my own funded wallets, and (4) starts post-launch monitoring with take-profit, stop-loss, and a kill-switch. It must enforce hard risk limits (max SOL spend, max slippage, dry-run mode) before every on-chain action and refuse anything over budget. Target Claude Code and a generic config runtime.",
  "mode": "full", "agents": 10, "tier": "standard",
  "targets": ["claude-code", "config", "solana-launch"]
}
```

A good resulting bundle contains:

- **`skills/`** — `launch-token`, `upload-metadata`, `wallet-signing`,
  `launch-strategy`, `post-launch-monitor`, `risk-controls`, each with concrete
  "how to call it" instructions and an example line.
- **`targets/solana-launch/`** — the runnable tool modules above (all valid
  Python), `tools.json`, `requirements.txt`, an `example_launch.py`, and a README
  with the money/safety warnings.
- **`targets/claude-code/`** — `CLAUDE.md` plus a `.claude/skills/<slug>/SKILL.md`
  for each launch skill, so the agent in Claude Code knows the tools exist and
  how to sequence them (upload → preflight → create → monitor).
- **`examples/`** — launch, snipe, and safe-exit walkthroughs.

A typical generated bundle for the launcher spec looks like this:

```
phantom-launch-agent/
  README.md  manifest.json  bundle.yaml
  run.py  agents.json                      # runnable multi-agent app (stdlib only)
  prompts/system_prompt.md  prompts/personas.md
  skills/                                  # launch-token, upload-metadata, wallet-signing,
    launch-token.md  upload-metadata.md    #   launch-strategy, post-launch-monitor, risk-controls
    wallet-signing.md  launch-strategy.md  #   (each: what it does + how to call it + example)
    post-launch-monitor.md  risk-controls.md
  examples/example_usage.md  examples/test_cases.json   # launch / snipe / safe-exit
  targets/claude-code/
    CLAUDE.md                              # system prompt + agents(+tools) + skills index
    .claude/skills/<slug>/SKILL.md         # one per launch skill, Claude Code frontmatter
  targets/config/agents.json  agents.yaml
  targets/solana-launch/                   # the runnable tool suite (all valid Python)
    pumpportal_client.py  ipfs_metadata.py  wallet.py
    launch_strategy.py  monitor.py  risk_controls.py
    tools.json  example_launch.py  requirements.txt  README.md
  deploy/                                  # Dockerfile, docker-compose, zeabur, DEPLOY.md
```

Each skill in `skills/` and each Claude Code `SKILL.md` names the exact function
it maps to in `targets/solana-launch/`, and `examples/` shows the call sequence
(`upload_metadata` -> `preflight` -> `create_token` -> `watch`) — so the agent
gets both the *how* (prose skills) and the *what* (callable code + `tools.json`).

Two more ready specs ship in [`examples/`](examples/): a launch **analyst +
sniper** (`pumpfun_sniper.json`) and a **multi-agent launch crew** — planner +
executor + monitor (`pumpfun_launch_crew.json`).

> ⚠️ These bundles generate agents that can move **real SOL on mainnet**. The
> tools default to a dry-run and enforce spend/slippage caps and a kill-switch,
> but you are responsible for keys, RPC, budget, and compliance. Bundled buys use
> **your own** wallets — don't use them to fake organic volume.

## Adding a new output target

Targets are a one-entry registry. Write a pure `build_x(bp) -> {path: content}`
function and register it:

```python
# bundler.py
def build_mytarget(bp: dict) -> dict:
    return {"targets/mytarget/config.json": json.dumps({"name": bp["name"]}) + "\n"}

TARGET_BUILDERS = { ..., "mytarget": build_mytarget }
```

That's it — it now appears in `GET /bundle/targets`, is selectable via
`"targets": ["mytarget"]`, and is picked up by `generate_files`. Builders are
pure and deterministic (all model creativity is already baked into the
blueprint), which keeps generation fast, reproducible, and unit-testable. For a
whole **domain pack** (like pump.fun), keep it in its own module and register its
builder plus a `matches()`/`enrich_blueprint()` hook — see `pumpfun.py`.

## Production deployment

**Docker Compose** (engine + optional Redis):

```bash
cp .env.example .env    # fill in LLM_PROVIDER + key, PHANTOM_INTERNAL_SECRET
docker compose up --build                     # engine only (disk storage)
docker compose --profile redis up --build     # + Redis (durable, multi-replica)
```

- **Storage** — bundles and consumed payment signatures persist to disk by
  default, or to **Redis** when `REDIS_URL` is set (survives restarts, shared
  across replicas). The store pings Redis on boot and falls back to disk if it's
  unreachable, so it never blocks startup. `GET /health` reports the active
  backend.
- **Config validation** — on startup the engine validates the environment and
  logs warnings (no LLM key, unprotected write surface) and errors (paywall on
  without a wallet). Check the boot logs.
- **Health checks** — Compose ships a container healthcheck against `/health`.
  Use `/health/llm` (or `/health?deep=1`) in your load balancer if you want to
  gate on real LLM connectivity.
- **Graceful shutdown** — on SIGTERM the app cancels in-flight background jobs
  and stops the scheduler cleanly (via the lifespan handler).
- **Rate limiting** — per-IP limits on the heavy endpoints via `slowapi`
  (`RATE_LIMIT_*`). Behind a proxy, forward the real client IP.
- **Logging** — set `LOG_JSON=1` for structured logs; every line carries the
  request id.

### Migrating an existing deployment

Nothing breaks: all previous endpoints and behavior are preserved, and the new
dependencies degrade gracefully (no `slowapi` → limiting off; no `redis`/`REDIS_URL`
→ disk storage). To adopt the new features: `pip install -r requirements.txt`
(adds `slowapi`, `redis`), optionally set `REDIS_URL` and `LOG_JSON=1`, and
switch your health probe to `/health/llm` if you want LLM-aware readiness.
Persisted signatures now survive restarts, so replay protection is durable.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `/health` shows `llm_configured: false` | No provider key. Set `OPENROUTER_API_KEY` or `DEEPSEEK_API_KEY` and match `LLM_PROVIDER`. |
| Deliberation messages say an agent "is momentarily unavailable" | An LLM call failed after retries; the run continues resiliently. Check `/health/llm` and your key's balance. |
| `402 payment required` on `/bundle/create` | The crypto paywall is on. Pay and pass `X-Payment-Tx`, or use the admin secret. |
| Payment rejected: "tx not found or not yet confirmed" | The tx hasn't reached the configured commitment. Wait, or set `CRYPTO_COMMITMENT=confirmed`. |
| Payment rejected: "signature already used" | Signatures are single-use and now persisted — each payment buys one run. |
| `429 Too Many Requests` | Rate limit hit. Tune `RATE_LIMIT_*` or back off. |
| Bundles vanish after restart | You're on disk storage with an ephemeral filesystem. Set `REDIS_URL` or mount a volume at `BUNDLE_STORE_DIR`. |
| `solana-launch` tools error on import | Install its own deps: `pip install -r targets/solana-launch/requirements.txt` (`solders`, `solana`, `requests`). |

## License

Source-available under the **Phantom Source-Available License v1.0** (see
[`LICENSE`](LICENSE)). Free for personal, non-commercial, evaluation, academic,
and non-profit use. **Commercial use requires a paid commercial license** —
contact `licensing@phantomcapital.live`. Running a paid hosted instance (e.g.
with the optional crypto paywall enabled) is a commercial use.

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
