# phantom-swarm-engine

A 20-agent hive mind for Phantom Capital that can run a **complete, AI-controlled
pump.fun launch** end to end: the swarm deliberates a token, designs its logo,
mints it on-chain via pump.fun, announces the contract address on X, and then
trades its own position. Agents debate via OpenRouter, streamed to clients over SSE.

## The swarm (20 agents, one hive mind)

Phoebe orchestrates; 19 specialists converge on a single launch and then run the
trading desk together. No agent has a veto — the swarm decides, Phoebe synthesizes.

- **Creative:** Claire (narrative), Quill (naming/ticker), Pixel (art), Sable (meme), Iris (trends)
- **Growth:** Nova (growth), Echo (community), Orbit (distribution), Vane (sentiment)
- **Build:** Loom (builder), Sol (Solana infra), Ledger (treasury)
- **Market:** Atlas (tokenomics), Vero (timing)
- **Trading desk:** Flux (strategy), Kestrel (momentum), Dax (whale watch), Rune (on-chain), Mira (risk)

## Launch pipeline

`POST /swarm/launch` → deliberate → distill token spec → generate logo →
upload metadata to pump.fun IPFS → mint via PumpPortal (signed locally, key never
leaves the process) → optional dev buy → auto-tweet the contract address → hand
the position to the trading desk.

## Safety rails (real money on mainnet)

- **`PUMP_LIVE`** must be `"true"` to broadcast. Otherwise every launch/trade is a
  **dry run** — the full pipeline runs but no transaction is sent and no SOL is spent.
- **`MAX_DEV_BUY_SOL`** / **`MAX_TRADE_SOL`** hard-cap any single spend.
- The wallet private key lives only in `PHANTOM_WALLET_PRIVATE_KEY`; PumpPortal's
  *local* API returns unsigned transactions that are signed in-process.

## Endpoints

Deliberation
- `POST /swarm/start` — start a deliberation (X-Phantom-Internal)
- `GET  /swarm/stream/{session_id}` — SSE message stream (public)
- `GET  /swarm/status/{session_id}` — session status (public)
- `POST /swarm/schedule` · `GET /swarm/scheduled` — schedule deliberations

Launch & trade
- `POST /swarm/launch` — full autonomous launch: `{ topic, dev_buy_sol?, free_mode? }`
- `GET  /swarm/wallet` — pubkey, balance, live flag, spend caps
- `POST /swarm/trade` — `{ mint, side: buy|sell, sol_amount?, percent? }`
- `GET  /swarm/position/{mint}` — current swarm position

Content & ops
- `POST /swarm/art` — generate art · `POST /swarm/tweet` · `POST /swarm/post`
- `POST /report` — trigger daily report · `GET /health`

## Environment variables

| Var | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | LLM inference |
| `PHANTOM_WALLET_PRIVATE_KEY` | base58 signing key for the swarm wallet |
| `SOLANA_RPC_URL` | RPC endpoint (default: mainnet-beta) |
| `PUMP_LIVE` | `"true"` to broadcast on-chain; anything else = dry run |
| `MAX_DEV_BUY_SOL` | dev-buy cap (default 0.5) |
| `MAX_TRADE_SOL` | per-trade cap (default 0.25) |
| `DELIB_ROUNDS` | debate rounds (default 2) |
| `SEGMIND_API_KEY` | logo generation |
| `TWITTER_API_KEY` / `_SECRET` / `TWITTER_ACCESS_TOKEN` / `_SECRET` | posting |
| `PHANTOM_INTERNAL_SECRET` | gate on write endpoints |
