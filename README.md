# phantom-swarm-engine

Real-time 5-agent deliberation engine for Phantom Genesis. Agents debate via OpenRouter, streamed to clients via SSE.

## Endpoints
- `POST /swarm/start` — start deliberation (requires X-Phantom-Internal)
- `GET /swarm/stream/{session_id}` — SSE message stream (public)
- `GET /swarm/status/{session_id}` — session status (public)
- `GET /health` — health check
