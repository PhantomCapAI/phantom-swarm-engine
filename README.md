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
