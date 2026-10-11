# Euler — CLI & Tools Reference

Euler has two control surfaces:

1. **The `euler` CLI** — lifecycle verbs you (or the main agent on your behalf) run
   to manage projects and workers.
2. **The MCP tools** — callable through the shipped `bin/euler-mcp`,
   `bin/euler-control-mcp`, `bin/write-paper-mcp`, and `bin/human-summary-mcp`
   entry points. Worker scaffolds receive only the truth gateway.

You mostly talk to the main agent in natural language; it runs the CLI verbs and
calls the tools. This page is the reference for what exists.

For constructor-valid frontier, kernel and compute JSON examples, see
[Frontier v0.2](../ARCHITECTURE.md).

---

## The `euler` CLI

Run via `bin/euler` (which sources `scripts/env.sh`). Every verb names a project;
there is no default project.

| verb | form | what it does |
|---|---|---|
| `list` | `euler list [--json]` | all projects + live worker counts + model |
| `new` | `euler new <project> [--profile native\|codex] [--roles codex:1,grok:1,...] [--model M]` | scaffold a project; the default native roster uses Codex, Claude Code, Grok, and Cursor |
| `init` | `euler init <project> --contract FILE --policy FILE [--obligation FILE]` | initialize Euler control on an existing project (create-or-assert-same). Omitted obligation uses the contract's exact statement |
| `frontier` | `euler frontier <project>` | active obligations, routes, blockers and evidence |
| `research-update` | `euler research-update <project> --operations FILE` | apply an atomic typed research-state change set |
| `admit-fact` | `euler admit-fact <project> <fact_id> --obligation ID --scope-hash HASH --alignment-note TEXT` | link an already admitted Fact after scope review |
| `kernel-check` | `euler kernel-check <project> --obligation ID --request FILE --request-id ID` | reserved actual Lean check |
| `kernel-align` | `euler kernel-align <project> --obligation ID --artifact HASH --review FILE` | record explicit statement alignment |
| `compute` | `euler compute <project> --request FILE` | bounded polynomial search with independent checking |
| `compute-review` | `euler compute-review <project> --review FILE` | review original-object realization of a checked witness |
| `propose` | `euler propose <project>/<worker> --candidate FILE` | persist a CandidateAction for that worker |
| `schedule` | `euler schedule <project> [--dispatch] [--launch]` | run the transparent scheduler. `--launch` is off by default and implies `--dispatch` |
| `reconcile` | `euler reconcile <project>/<worker>` | reconcile the latest verified start/launch/result/end receipt chain |
| `checkpoint` | `euler checkpoint <project> [--retry-infra --retry-reason TEXT]` | proof audit for DISCHARGED, counterexample audit for REFUTED; explicit infrastructure retry |
| `assign` | `euler assign <project>/<worker> (--task "…" \| --file P \| --stdin) [--branch-id ID --subgoal-id ID]` | **not** an Euler-managed workflow. Lease-bound TASK writes come from `euler schedule --dispatch` |
| `finalize` | `euler finalize <project> [--paper <paper_id>] [<fact_id> …]` | record the approved target theorem(s) in the paper's `TARGET.md` (what write-paper reads; default paper → `<project>/TARGET.md`, a non-default `--paper` → `<project>/papers/<paper_id>/TARGET.md`). **With no id:** print candidate terminal facts as suggestions (writes nothing) |
| `start` | `euler start <project>[/<worker>]` | launch the autonomous worker loop(s) — **fail-closed**: proving preflight must pass first |
| `status` | `euler status <project>[/<worker>] [--json]` | per-worker liveness + round + last activity. Project-level `--json` includes `control` status when initialized. Usable when verify is down |
| `failures` | `euler failures <project> [--json]` | read-only failure-cluster ledger (`cluster_id` / state / trigger / assigned worker). Does not read worker private memory. Ledger is the state source; `failures/requests/<cluster_id>.json` is the immutable sidecar. A control-plane error is `FAILURE_CONTROL_FAILED` (not a math verdict) |
| `stop` | `euler stop <project>[/<worker>] [--force]` | graceful (finish the round, exit at the boundary) or `--force` (kill the process group) |
| `verify-health` | `euler verify-health [--json]` | read-only verify-service **identity** (`ours`/`foreign`/`stale`/`down`). Transport only — **not** a mathematical verdict |
| `doctor` | `euler doctor [--json]` | read-only proving-readiness (python+mcp, worker gateway tools, verify identity). Verify down is a warning |

Notes:
- `finalize` only **records** the answer; it does not stop workers. Deciding a
  verified fact is *the answer* is your call (the main agent surfaces it).
- `start` launches each worker detached in its own process group, so it survives
  your session; `stop --force` can therefore kill a live worker and its in-flight
  codex child. Before spawn **and before every round** it runs proving preflight
  (python+MCP import, worker gateway table, worker MCP config `role=worker`
  plus a live stdio `initialize`/`tools/list` handshake, verify `/health`
  identity with matching pid **and** nonempty matching `euler_root`, writable
  contained fact_graph/global_memory/local_memory via a unique atomic probe).
  Missing `/health` `euler_root` is foreign. An own pid/root responder that is
  not HTTP 200 `status=ok` makes `verify-health` exit nonzero. Any preflight
  failure writes `PRECHECK_FAILED` with `{component,reason}` and **does not
  launch a worker/codex subprocess**. After preflight the orchestrator writes a
  per-round start receipt (`<project>/receipts/<worker>/round_NNNN.start.json`)
  and verifies its hashes. Managed kickoff uses the compiled action packet as
  automatic history; legacy unmanaged workers use their bounded memory digest.
  Persist/verify failure is `RECEIPT_FAILED` (no provider launch).
  After the round a companion `.end.json` records the end watermark and appended IDs.
- There is **no** `euler submit` (and no production fallback from missing MCP
  tools to `from euler.gateway.server import fact_submit` or direct JSONL writes).
  Fact writes go only through the worker MCP tool `fact_submit`.
- `list` / `status` / `failures` / `doctor` / `verify-health` do not require verify to be up.

---

## MCP server — `euler-control` (main only)

Launched by `bin/euler-control-mcp` as `[mcp_servers.euler-control]` with
`EULER_ROLE=main`. Workers and the verifier see **no** control tools. The main
agent still has **no** `fact_submit`.

| tool | args | what it does |
|---|---|---|
| `euler_project_status` | `project` | deterministic control-plane status |
| `euler_register_contract` | `project, contract, policy, obligation?` | initialize/assert-same managed control |
| `euler_frontier` | `project` | read the active frontier |
| `euler_update_frontier` | `project, operations` | apply a transactional research change set |
| `euler_admit_fact` | `project, fact_id, obligation_id, confirmed_scope_hash, alignment_note` | associate existing admitted evidence |
| `euler_kernel_check` | `project, obligation_id, request_id, request` | execute a local Lean check |
| `euler_kernel_align` | `project, obligation_id, kernel_artifact_hash, review` | attest reviewed formal-statement alignment |
| `euler_compute` | `project, request` | bounded polynomial search and checking |
| `euler_compute_review` | `project, review` | record witness realization review |
| `euler_propose_action` | `project, worker, candidate` | persist a CandidateAction |
| `euler_schedule` | `project, dispatch?, launch?` | scheduler; launch defaults false |
| `euler_record_failure` | `project, failure` | typed FailureRecord event |
| `euler_analyze_recurrence` | `project, tool_hash?` | read-only assessment; never auto-parks |
| `euler_checkpoint` | `project, retry_infra?, retry_reason?` | trigger/replay proof or counterexample final audit |

---

## MCP server 1 — `euler` (the gateway): the door to the truth stores

The gateway is **role-gated**: what a caller can see depends on `EULER_ROLE`. The
main agent runs as `role=main`.

**The six tools** (a trailing `?` marks an optional argument):

| tool | args | what it does |
|---|---|---|
| `gm_add` | `kind, claim, evidence, verifiable?, glossary?, links?, project?` | publish a finding to shared global memory |
| `gm_search` | `query, kinds?, limit_per_kind?, project?` | search global-memory findings |
| `fact_submit` | `statement, proof, predecessors?, glossary_introduces?, intuition?, source_id?, external_refs?` | **the write-gate** — verify, then write the fact iff `correct`, always trace the verdict |
| `fact_search` | `query, limit?, project?` | search the verified fact graph |
| `fact_revoke` | `fact_id, reason, project?` | cascade-revoke a fact + its dependents |
| `search_arxiv_theorems` | `query, num_results?` | semantic search over arXiv theorem statements |

**Who can see what (`euler/gateway/roles.py`):**

| role | tools |
|---|---|
| **worker** | `gm_add`, `gm_search`, `fact_submit`, `fact_search`, `search_arxiv_theorems` |
| **main** | `gm_add`, `gm_search`, `fact_search`, `fact_revoke`, `search_arxiv_theorems` (**no `fact_submit`**) |
| **verifier** (the fact-checking verifier behind `fact_submit`) | `search_arxiv_theorems` only (read-only) |

The main agent thus **cannot write a fact** and **cannot** even see `fact_submit`;
the verifier can write nothing. See `security-and-trust.md`.

---

## MCP server 2 — `write-paper`: fact graph → publishable paper

Six tools (`euler/write_paper/server.py`), each wrapping an isolated codex role.
The main agent calls them with small structured args; the heavy bytes (style guide,
fact-graph math) are assembled inside the tool and never enter the main agent's
context. Each tool returns a small honest envelope (status + paths + flags + a
`log_path` to a per-call diagnostic log), never the full `.tex`.

| tool | role | what it does |
|---|---|---|
| `paper_subgraph` | (curation, no codex) | return a compact, deterministic statements-only skeleton of the target-fact closure for the main agent to read and pick from — no codex, no writes; feed the chosen `fact_ids` to `paper_write` |
| `paper_write` | writer | draft the first complete `main.tex` from the target closure + house style |
| `reference_audit` | reference auditor | **offline** — flag bibliography entries it cannot vouch for (no tools/network) |
| `reference_verify` | reference verifier | **online** — verify flagged citations (arXiv + web) and update the reference ledger in place |
| `paper_revise` | reviser | revise `main.tex` for compile fixes / operator annotations / citation fixes (in-tool compile-retry loop) |
| `paper_verify_math` | (math re-verification) | re-check the whole paper's math, as written, through a dedicated verifier before delivery |

Most tools take an optional `paper_id` — a project can hold multiple papers (the
default paper uses the legacy `<project>/paper/` workspace; any other `paper_id`
gets an isolated `<project>/papers/<paper_id>/`). See the write-paper skill README
(`agents/skills/write-paper/README.md`) for the full workflow.

---

## MCP server 3 — `human-summary`: fact graph → reader report

| tool | what it does |
|---|---|
| `summary_write` | render a human-readable, id-free progress report (compiled PDF) from the fact graph — precise problem statement, partial results with real proof sketches, the main obstacle, a neutral timeline, and the remaining lemma |

`summary_write` takes an optional `language` (else it follows the operator's
language in `OPERATOR.md`).

---

## Main-agent skills (invoked in-session, not MCP tools)

Euler's reusable agent skills are shipped under `agents/skills/`:
`initialize` (first-run setup), `elaboration` (the strategy synthesis),
`consult` (the strategy consult), `human-summary`, and `write-paper`. These
orchestrate the tools and CLI above; see `operating-guide.md` for how they fit the
lifecycle.

---

## The persistent services (run via `scripts/services.sh`)

| service | port | required? |
|---|---|---|
| `verify` | 127.0.0.1:8091 | **yes** — no verify ⇒ `fact_submit` fails ⇒ no facts |
| `dashboard` | 127.0.0.1:8099 | optional (read-only view; port-forward to see it) |

```bash
bash scripts/services.sh up verify            # required before any proving
bash scripts/services.sh up dashboard <p>     # optional
bash scripts/services.sh status | logs <svc> [-f] | down <svc>|all
```

See `operations.md` for the runbook and `configuration.md` for the environment
variables that tune all of the above.
