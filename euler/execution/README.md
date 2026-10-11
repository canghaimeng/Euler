# euler/execution — the worker swarm (round loop + scaffolding + layout)

Where heterogeneous workers reason and verify. This module owns the on-disk
**layout**, project/worker **scaffolding**, and the per-worker **round loop**. The
`euler` CLI (`euler/orchestration`) is a thin UX layer over this; the real lifecycle
lives here.

```
euler/execution/
  layout.py     paths + names; WorkerLayout; parse_roles("high:3,xhigh:4")
  scaffold.py   do_new (project + worker dirs, .codex config, symlinks), spawn_loop
  (providers live in ``euler/providers``: native Codex/Claude Code/Grok/Cursor)
  preflight.py     fail-closed proving readiness (before any worker round/process)
  memory_digest.py bounded shared-memory retrieval + immutable round receipts
  loop.py          the round loop: kickoff prompt, run_round, stop conditions, status
  __main__.py   `python -m euler.execution <worker_dir>` → loop.main
  tests/{test_execution.py, test_loop.py, test_preflight.py, test_memory_digest.py}
```

## On-disk layout (`layout.py`)

`<agents_root>/<project>/` holds the shared `global_memory/` + `fact_graph/` +
`project.json` + `receipts/<worker>/`; each `workers/<worker>/` is an agent cwd with `AGENTS.md` →
its role contract, optional `.agents/skills` → `agents/skills/worker`, a
`.codex/config.toml` (MCP = `python -m euler.gateway`, `EULER_ROLE=worker`,
`EULER_VERIFY_URL`, `tool_timeout_sec=3600`), the selected provider config,
`TASK.md`, `local_memory/`, and the
control files (`.status.json` `.pid` `.stop` `logs/`). `agents_root` =
`EULER_AGENTS_ROOT` (default `runtime/projects`).

## Default native roster

| workers | provider | purpose |
|---|---|---|
| `codex1`, `codex2` | `codex` | independent research and proof construction |
| `grok1`, `grok2` | `grok` | independent research, checking, and bounded computation |
| `claude1` | `claude-code` | long-context research and verification |
| `cursor1` | `cursor` | independent checking, engineering, and research support |

Native worker providers are `codex`, `claude-code`, `grok`, and `cursor`. Create one of each without hand-editing files:

```bash
euler new DEMO --roles codex:1,claude-code:1,grok:1,cursor:1
```

Role files store `PROVIDER=...`. Claude Code defaults to the `claude` CLI; set
`EULER_CLAUDE_CODE_BIN=claf` (or another compatible wrapper) when needed. Every
provider follows the same Memory Contract and verifier-gated `fact_submit` route.

## The round loop (`loop.py`)

**Proving preflight** (`preflight.py`) runs in `euler start` **before**
`spawn_loop` (defense in depth) and **immediately before every** `run_round`,
including continuation rounds. It fail-closes unless: this Python can import
Euler+MCP; the worker gateway table is complete; the worker MCP config launches
`python -m euler.gateway` with `EULER_ROLE=worker` **and** a live stdio
`initialize`+`tools/list` handshake returns the full worker tool set; verify
`/health` is HTTP 200 `status=ok` **and** belongs to this deployment (matching
pidfile pid **and** nonempty matching `euler_root` — missing root is foreign);
project `fact_graph/` / `global_memory/` and the worker's `local_memory/` are
contained in their expected roots and accept a unique atomic temp probe that is
then removed (never a fact or memory JSONL write). Failure writes
`PRECHECK_FAILED` with `{component,reason}` and must
not launch `codex`. `/health` reports the verification service identity.
An own pid/root responder that is not HTTP 200 `ok` makes `euler verify-health`
exit nonzero.

**Memory digest + receipts** (`memory_digest.py`) run after preflight and
**before every** `run_round`. The orchestrator (not the model) retrieves shared
global-memory kinds `proof_attempt`, `obstacle`, `dead_end`, `counterexample`,
`verification`: exact `branch_id`/`subgoal_id` hits first when TASK.md carries
optional YAML-like frontmatter, then BM25 fill from the assignment body, deduped,
default top 5 per kind, deterministic total cap (16). Full history is never
injected. One immutable start receipt is written under
`<project>/receipts/<worker>/round_NNNN.start.json` with a **monotonic
allocator** (survives process restart; concurrent alloc cannot collide).
Receipts are created with `O_CREAT|O_EXCL` (pre-existing path is a fail, never
overwrite). Payload includes per-kind queries/hits, start watermark
(`byte_size`, prefix/file digest, ID count/hash), canonical digest +
`digest_hash`, and `receipt_hash` over the full start payload except
`receipt_hash` itself. The file is re-read and both hashes verified before
`codex` launch; failure is `RECEIPT_FAILED`. The injected prompt is additionally
capped by configurable char/UTF-8 byte limits (lowest-priority hits/snippets
pruned). After the round a companion `round_NNNN.end.json` is likewise exclusive
and self-hashed; it records `memory_watermark_end` and IDs from a verified
append-only suffix (truncation/rewrite/prefix mutation fail closed) without
rewriting the start payload/hash. Finalization failure is `RECEIPT_FAILED` and
the next round does not start.

**Failure clusters (stage 4).** After a successful round-close the loop scans
NEW stamped `proof_attempt` / `verification` / `obstacle` rows (legacy unstamped
records never trigger). Configurable thresholds default to: ≥3 blocked/abandoned
attempts on a subgoal; the same normalized approach failed by ≥2 workers; ≥3
consecutive `transport_status=ok` `verdict=wrong` verifications on a subgoal
(infra `verdict=null` never counts); the same normalized obstacle seen by ≥2
workers. Hits persist a system-owned request under `<project>/failures/` (project
lock + append-only `ledger.jsonl` + immutable `requests/<cluster_id>.json`
sidecar; one request per `cluster_id`). The ledger is the state source; the
sidecar is the request snapshot. Crash recovery under the lock: sidecar exists
but no `requested` ledger row → validate the sidecar against the freshly
detected cluster and append **exactly one** recovered `requested` event with a
fresh monotonic `seq`; ledger `requested` but sidecar missing → recreate the
sidecar from that ledger event. Corrupt/mismatched sidecars fail closed
(`FAILURE_CONTROL_FAILED`) — they are never left invisible and never silently
marked local-only. `resolve_request` appends `resolved` once, only for the
currently requested cluster assigned to this worker. The assigned worker's next
kickoff injects `FAILURE SYNTHESIS MODE` with those IDs. That round still has to
satisfy stages 1–3 and must append a matching-context `key_failures_summary`
obstacle or dead_end **candidate** whose `source_record_ids` equal the request
`member_ids` exactly (and whose failed-attempt list equals `attempt_ids` when
present); missing/fake/extra IDs or missing output is `FAILURE_SYNTHESIS_FAILED`.
Control-plane errors (`FAILURE_CONTROL_FAILED`) stop the loop before another
round. The scheduler never writes facts and never confirms a `dead_end`.

A **round = one `codex exec` continuation session** that resumes from persisted
memory (NOT one increment). Launched detached in its **own process group**
(`start_new_session`), so it survives your shell and `stop --force` can `killpg` the
loop + its codex child. Stop conditions checked at the round boundary: `.stop` flag,
`.run_deadline`, `EULER_MAX_ROUNDS` (0 = unlimited), `EULER_MAX_CONSEC_FAILURES`
(5). Config read at call time (`EULER_ROUND_HARD_TIMEOUT` 4h, `EULER_ROUND_BEAT` 5s).
`.status.json` is written atomically. **Resumability is continuity in the stores**,
not process state — a fresh `start` rebuilds context from memory + the fact graph.

## Connects to

Reads `TASK.md` (from `euler assign`) + `master_guidance` (strategy). Writes facts
only via a worker's `fact_submit` (gateway → verify). The loop itself never writes
the truth stores — it only scrapes the resulting `fact_id` from the round log for
status.

## Tests

`python -m pytest euler/execution/` (offline; a fake codex stub drives the loop /
stop / scaffolding).
