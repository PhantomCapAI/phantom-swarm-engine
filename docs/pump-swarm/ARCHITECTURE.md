# Pump.fun Autonomous Launch Swarm — Architecture

A multi-agent system that can research trends, design a coin, launch it on
pump.fun, manage the initial position + creator fees, and monitor the bonding
curve — with hard money/safety guardrails and a human approval gate on every
on-chain spend.

This design plugs into the two layers you already have / want:

- **Design layer** — `phantom-swarm-engine` (this repo) generates the *prompt +
  skill bundle* for each agent role. One bundler run per role.
- **Runtime layer** — **LangGraph** supervisor graph runs the agents with real
  tool calling. Tools are backed by **`pump-fun-sdk` (MCP server)** for on-chain
  actions and **PumpPortal** as the HTTP fallback, over a Solana RPC.

```
                        ┌─────────────────────────────┐
                        │        Orchestrator          │  LangGraph supervisor
                        │  (state, routing, budget,    │  — never signs, only routes
                        │   approval gate, kill switch)│
                        └──────────────┬──────────────┘
             read-only research  ┌─────┴───────────────────────────┐  on-chain (gated)
        ┌───────────────┬────────┴──────┬───────────┬──────────────┴────────┐
   ┌────▼────┐   ┌──────▼─────┐   ┌──────▼─────┐ ┌───▼──────┐  ┌────────────▼──────────┐
   │ Trend   │   │ Creative   │   │ Metadata / │ │Compliance│  │  Treasury / Wallet     │
   │ Scout   │   │ Director   │   │ Art Dir.   │ │ Guardian │  │  Guardian (signer      │
   │(ideas)  │   │(name/story)│   │(image/meta)│ │ (VETO)   │  │  policy, caps, log)    │
   └─────────┘   └────────────┘   └────────────┘ └──────────┘  └───────────┬───────────┘
                                                                            │ signs iff approved
                                              ┌─────────────────────────────┼───────────────┐
                                        ┌─────▼──────┐  ┌────────────┐  ┌────▼───────┐  ┌────▼─────┐
                                        │  Launch    │  │  Position  │  │   Fee      │  │  Curve   │
                                        │  Engineer  │  │  Manager   │  │  Manager   │  │  Monitor │
                                        │ (create+   │  │ (own buy/  │  │ (claim +   │  │ (curve,  │
                                        │  dev buy)  │  │  sell risk)│  │  split)    │  │  alerts) │
                                        └────────────┘  └────────────┘  └────────────┘  └──────────┘
```

## Agent roles & responsibilities

| # | Agent | Type | Owns | Key tools |
|---|-------|------|------|-----------|
| 0 | **Orchestrator** | control | Routing, shared state, per-launch + daily budget, the human approval gate, global kill switch. Never holds keys, never signs. | LangGraph state; `budget.check`, `approval.request`, `killswitch.status` |
| 1 | **Trend Scout** | read-only | Surface coin ideas from X/Farcaster/news + on-chain signals (what's graduating, volume leaders). Outputs ranked candidate themes with evidence. | `web.search`, `x.search`, `pumpfun.trending`, `solana.recent_launches` |
| 2 | **Creative Director** | read-only | Name, ticker (≤10 chars, uppercase), one-line hook, description, meme/narrative angle, socials plan. Anti-slop, specific. | none (LLM only) |
| 3 | **Art Director** | read-only | Generate logo/token image (512×512 PNG) + optional banner; ensure it renders and is on-brand. | `image.generate`, `image.validate` |
| 4 | **Metadata Builder** | write (off-chain) | Assemble the token metadata JSON (name, symbol, description, image, socials), upload image + metadata to IPFS / pump metadata endpoint, return the `metadataUri`. | `ipfs.upload`, `pumpfun.metadata_upload` |
| 5 | **Compliance / Risk Guardian** | **veto gate** | Screen name/ticker/claims for trademark collisions, impersonation, obvious scam/"guaranteed returns" language, required disclaimers. Confirm this is not a restricted party/jurisdiction. **Can block a launch.** | `web.search`, policy checklist (skill), `wallet.balance` |
| 6 | **Treasury / Wallet Guardian** | **signer policy** | The only component near keys. Enforces: dedicated launch wallet, max SOL per launch, max daily spend, allow-listed program IDs, slippage/priority-fee ceilings. Signs a tx **only** after Orchestrator confirms approval + budget. Logs every signature. | `wallet.balance`, `tx.simulate`, `tx.sign` (policy-wrapped), `killswitch.trip` |
| 7 | **Launch Engineer** | on-chain (gated) | Build + submit the pump.fun `create` tx with `metadataUri`, optional atomic **dev buy**, and creator-fee-share wallet config. Verify the mint landed. **This is the "Pump.fun Launch Specialist" — the first bundle you generate.** | `pumpfun.create_token`, `pumpfun.dev_buy`, `pumpfun.set_fee_share`, `solana.confirm_tx` |
| 8 | **Position Manager** | on-chain (gated) | Manage **the creator's own** position only — size the initial buy, take-profit/stop ladders, slippage + priority fees. Risk-limited. **Not** a wash-trading/volume-faking bot (explicitly out of scope — see Safety). | `pumpfun.buy`, `pumpfun.sell`, `market.quote` |
| 9 | **Fee Manager** | on-chain (gated) | Poll accrued creator fees, claim them, split to the configured wallets (fee-sharing, up to 10). | `pumpfun.creator_fees`, `pumpfun.claim_fees` |
| 10 | **Curve Monitor** | read-only | Track bonding-curve progress toward graduation, volume, holder count, price; emit alerts and hand structured state back to the Orchestrator to trigger Position/Fee actions. | `pumpfun.curve_state`, `pumpfun.trades_ws`, `solana.holders` |

### Why this split
- **One agent, one focus** — mirrors your existing bundler CREW philosophy, so each role gets a tight, testable prompt bundle.
- **Read → decide → sign is a one-way gate.** Research agents (1–5, 10) can never move funds. Only the Treasury Guardian (6) signs, and only after the Orchestrator has an explicit approval + budget check. This is the single most important structural safety property.

## The two-phase build

**Phase A — generate bundles (this repo).** Run the bundler once per role. Start
with the Launch Engineer (`specs/launch-specialist.spec.md`), which is the
riskiest and most valuable to get right. Each run gives you a drop-in
prompt/skill package for Claude Code / Cursor / **LangGraph** (request the
`langgraph` target).

```bash
phantom-bundle create "$(cat docs/pump-swarm/specs/launch-specialist.spec.md)" \
  --mode full --agents 14 --download --unzip
# repeat for trend-scout.spec.md, creative-director.spec.md, ... as you write them
```

**Phase B — wire the runtime (LangGraph).** The generated `targets/langgraph/`
scaffold gives you a `StateGraph`. Turn each bundle into a node, add the
Orchestrator as the supervisor/router, and bind the pump tools (below) to the
on-chain nodes only.

## Tools / skills the agents need

Backed by **`pump-fun-sdk` MCP** (preferred — offline instruction builders, you
control signing) with **PumpPortal Local Transaction API** as HTTP fallback
(0.5% fee, you still sign locally — never use Lightning/custodial for the signer
path). All on-chain tools go through the Treasury Guardian's policy wrapper.

| Skill | Backing call | Used by |
|-------|--------------|---------|
| `pumpfun.metadata_upload` | SDK/IPFS + pump metadata endpoint | Metadata Builder |
| `pumpfun.create_token` | SDK `create` instruction / PumpPortal `create` | Launch Engineer |
| `pumpfun.dev_buy` | atomic initial buy in the create bundle (Jito) | Launch Engineer |
| `pumpfun.set_fee_share` | fee-sharing config (≤10 wallets, Jan-2026 feature) | Launch Engineer |
| `pumpfun.buy` / `pumpfun.sell` | SDK bonding-curve trade / PumpPortal `trade` | Position Manager |
| `pumpfun.creator_fees` / `pumpfun.claim_fees` | creator-fee read + claim | Fee Manager |
| `pumpfun.curve_state` | bonding-curve account read | Curve Monitor |
| `pumpfun.trades_ws` | PumpPortal / SDK trade WebSocket | Curve Monitor |
| `solana.confirm_tx` / `tx.simulate` | RPC `simulateTransaction` + confirmation | Treasury Guardian, Launch Engineer |
| `wallet.balance` | RPC balance / token accounts | Treasury Guardian, Compliance |
| `tx.sign` (policy-wrapped) | local keypair sign under caps/allowlist | Treasury Guardian |
| `image.generate` | your image model of choice | Art Director |
| `web.search` / `x.search` | trend inputs | Trend Scout, Compliance |

## Safety — real money / real wallets

This system spends real SOL autonomously. Treat every guardrail below as a hard
requirement, not a nice-to-have.

**Keys & custody**
- Use a **dedicated launch/hot wallet**, funded with only what a launch cycle
  needs. Never the wallet holding your treasury.
- **Non-custodial signing only.** Keep private keys in your infra (env/secret
  manager, ideally KMS/HSM). Prefer the SDK's offline builders or PumpPortal
  **Local** API so keys never leave your process. Never the Lightning/custodial
  path for the signer.
- **Never commit keys.** `.env` is already gitignored here — keep it that way;
  add a secrets scan to CI.

**Spend controls (enforced in the Treasury Guardian, not just prompts)**
- Max SOL per launch, max SOL per trade, max daily spend, max concurrent
  positions — enforced in code that wraps the signer, so a hallucinated agent
  request physically cannot exceed them.
- **Program-ID allowlist** — only pump.fun / PumpSwap / known programs; reject
  any tx that touches an unexpected program (drains, fake token approvals).
- **Slippage + priority-fee ceilings**; reject trades outside them.
- **`simulateTransaction` before every submit**; abort on unexpected balance
  deltas.

**Human-in-the-loop & circuit breakers**
- **Approval gate**: the first on-chain action of any launch (create + dev buy)
  requires explicit human approval by default. Graduate to fuller autonomy only
  after dry-runs on **devnet**.
- **Global kill switch** the Orchestrator checks before routing to any signing
  node; tripping it halts all on-chain activity.
- **Dry-run / simulation mode** as the default; `--live` is opt-in.
- Full **audit log** of every decision and signature (who/what/why/amount/sig).

**Legal / conduct (out of scope by design)**
- **No market manipulation.** No wash trading, fake-volume/self-trading bots,
  spoofing, or coordinated multi-wallet pumping. The Position Manager manages
  *your own* single position under risk limits — nothing more. Building volume
  fakery is fraud in most jurisdictions and is intentionally excluded here.
- **No misleading claims.** The Compliance Guardian blocks "guaranteed
  returns", fake team/partnership claims, and impersonation/trademark
  collisions, and requires a not-financial-advice disclaimer on socials.
- Launching a token that mimics a real asset can be securities fraud/IP
  infringement. Screen for it; when unsure, don't launch.
- Confirm you're not a restricted person and pump.fun is available in your
  jurisdiction and consistent with its ToS before going live.

## Next concrete steps
1. **Generate the first bundle** from `specs/launch-specialist.spec.md` (above).
   Review the emitted prompt + skills; this is your highest-leverage artifact.
2. **Stand up the tool layer**: run `pump-fun-sdk`'s MCP server against a Solana
   RPC on **devnet**; smoke-test create/buy/claim from a throwaway wallet.
3. **Implement the Treasury Guardian policy wrapper** (caps, allowlist,
   simulate, sign) — do this *before* any live tx. It's the safety keystone.
4. **Scaffold the LangGraph graph** (request the `langgraph` target when
   bundling), add the Orchestrator supervisor, and register the Launch Engineer
   node with its tools behind the Guardian.
5. **End-to-end dry run on devnet**: Trend Scout → Creative → Metadata → launch
   (simulated) → monitor. Keep the human approval gate on.
6. **Write the remaining specs** (trend-scout, creative-director, metadata,
   compliance, fee-manager, curve-monitor), bundle each, add as nodes.
7. **One controlled mainnet launch** with a capped wallet and approval gate on;
   review the audit log; only then consider loosening autonomy.

## Sources
- PumpPortal API (create / trade / claim, Local vs Lightning): https://pumpportal.fun/creation/ · https://pumpportal.fun/creator-fee/ · https://pumpportal.fun/fees/
- pump-fun-sdk (TypeScript, MCP server, fee sharing, vanity keygen): https://github.com/nirholas/pump-fun-sdk
- Pump official docs + fees: https://github.com/pump-fun/pump-public-docs · https://pump.fun/docs/fees
- Creator fee sharing (up to 10 wallets, Jan 9 2026): https://www.mexc.com/news/449516
