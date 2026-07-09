# Pump.fun Autonomous Launch Swarm

Design + paste-ready bundler specs for a swarm that researches, designs,
launches, and monitors pump.fun tokens — using **this repo's bundler** to
generate each agent's prompt/skill package and **LangGraph** to run them with
real on-chain tool calling.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — full swarm design (11 roles), the
  tool/skill list, the two-phase build, safety/wallet guardrails, and next steps.
- **[specs/](specs/)** — natural-language specs you feed straight into the
  bundler. Start with `launch-specialist.spec.md` (the highest-risk agent).

## Quickstart

```bash
# 1. Generate the first agent bundle (Pump.fun Launch Specialist)
phantom-bundle create "$(cat docs/pump-swarm/specs/launch-specialist.spec.md)" \
  --mode full --agents 14 --download --unzip

# 2. Inspect the emitted prompt + skills, then wire targets/langgraph/ as a node.
```

Read **ARCHITECTURE.md → Safety** before any live transaction. This system
spends real SOL; the Treasury Guardian spend-cap/allowlist wrapper and the
human approval gate are hard requirements, not options.

### Specs still to write (one bundler run each)
`trend-scout` · `creative-director` · `art-director` · `metadata-builder` ·
`compliance-guardian` · `treasury-guardian` · `position-manager` ·
`fee-manager` · `curve-monitor` — role definitions are in ARCHITECTURE.md.
