# euler/orchestration — the `euler` CLI verbs

The operator's lifecycle commands. **Verbs/UX only** — the on-disk layout, the
round loop, and scaffolding live in `euler/execution`; this module parses arguments
and calls into it. Run via `bin/euler`.

```
euler/orchestration/
  cli.py        the verbs: list / new / assign / finalize / start / status / failures / stop / doctor / verify-health
  __main__.py   `python -m euler.orchestration` (what bin/euler execs)
  tests/{test_cli_verbs.py, test_orchestration.py}
```

## Verbs

| verb | does |
|---|---|
| `list [--json]` | projects + live worker counts + model (usable when verify is down) |
| `new <p> [--profile native\|codex] [--roles codex:1,claude-code:1,grok:1,cursor:1] [--model M]` | → `execution.scaffold.do_new`; default creates the six-worker native-provider team with explicit `PROVIDER=` values |
| `assign <p>/<w> (--task/--file/--stdin)` | overwrite that worker's `TASK.md` |
| `finalize <p> [<fact_id>…]` | record target(s) in `TARGET.md` (no id ⇒ suggest terminal facts); records only, does not stop workers |
| `start <p>[/<w>]` | proving preflight, then `execution.scaffold.spawn_loop` (idempotent via `.pid.lock`). Failure → `PRECHECK_FAILED`, no subprocess |
| `status <p>[/<w>] [--json]` | per-worker liveness + round + `stuck?` soft signal; `--json` includes `failures` (usable when verify is down) |
| `failures <p> [--json]` | read-only failure-cluster ledger (ids/states/triggers); no worker private memory. Sidecar/ledger crash recovery is in `execution/failure_clusters.py`; `FAILURE_CONTROL_FAILED` stops the worker loop |
| `stop <p>[/<w>] [--force]` | graceful `.stop` / `--force` `killpg` |
| `verify-health [--json]` | read-only service identity check (`ours`/`foreign`/`stale`/`down`) |
| `doctor [--json]` | read-only proving-readiness (python+mcp, worker tools, verify identity) |

## Notes

- Liveness is **zombie-aware** (`os.kill(pid,0)` + a `/proc/<pid>/stat` Z-state
  check), so `status`/`list` report exited processes and `start` can restart a crashed worker.
- Restart a worker loop with `stop` followed by `start`.
- Touches core only indirectly: `new` creates the empty `global_memory/`/`fact_graph/`
  dirs (populated lazily by core on first write); it never writes the truth stores.
- **No `submit` verb.** Direct gateway imports and JSONL writes are prohibited
  production fallbacks; missing MCP produces `PRECHECK_FAILED`.

## Tests

`python -m pytest euler/orchestration/` (offline; fake codex + stub project).
