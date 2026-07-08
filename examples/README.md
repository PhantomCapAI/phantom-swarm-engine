# Example specs

Copy-paste specs for `POST /bundle/create`. Each `.json` file is a ready request
body. Send one like:

```bash
curl -X POST http://localhost:8500/bundle/create \
  -H "X-Phantom-Internal: $PHANTOM_INTERNAL_SECRET" \
  -H "Content-Type: application/json" \
  --data @examples/pumpfun_launcher.json
```

(If the crypto paywall is on, drop the secret header and add `-H "X-Payment-Tx: <sig>"`.)

Then stream it and grab the zip:

```bash
SID=<session_id from the response>
curl -N  http://localhost:8500/bundle/stream/$SID     # watch the hive live
curl -OJ http://localhost:8500/bundle/$SID/download    # download <slug>.zip
```

| Spec | What it builds |
| --- | --- |
| `pumpfun_launcher.json` | Autonomous pump.fun **token launcher** (metadata → launch → monitor) |
| `pumpfun_sniper.json` | Launch **analyst + sniper**: evaluate a new mint, snipe, auto-exit |
| `pumpfun_launch_crew.json` | Multi-agent **launch crew**: planner + executor + monitor |

Any spec mentioning pump.fun / token launches automatically gets the
`solana-launch` target: runnable PumpPortal/IPFS/wallet/monitor tools plus safety
controls. See the "Generating pump.fun agents" section of the top-level README.
