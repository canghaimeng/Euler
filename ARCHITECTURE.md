# Euler — Architecture

Euler's persistent mathematical state is shared by human researchers and
frontier-driven agents. Exact scoped objects support inspection, task selection,
bounded recall and verified updates. The main coordinates work; workers use the
verifier-gated Fact gateway; kernel and computation adapters provide their own
scoped evidence. Human requests create auditable derived investigations through the control plane. Their results follow the existing Fact-admission and final-acceptance procedures.

This branch adds the object resolver, run lifecycle, intervention service and
opt-in human HTTP boundary documented in [HUMAN-INTERVENTION.md](docs/HUMAN-INTERVENTION.md).
The existing observation views remain the human entry point. Historical curated
nodes remain readonly without an exact writable backend identity.

The canonical main-agent operating contract is `agents/contracts/main_agent.md`.

---

## 1. Layered model

```
operator → ① orchestration (main agent + euler CLI)   — coordinates; no direct Fact writes
              ② strategy   (elaboration → consult → master_guidance)
              ③ execution  (native Codex, Claude Code, Grok, and Cursor workers)
   gm_* │         │ fact_submit
        ▼         ▼
   ⑤ truth      ④ verification (cold-start LLM judge; correct ⟺ no critical_errors AND no gaps)
   (fact graph + memory)   — a fact exists iff the verifier accepted it
        ▲
        │ every read/write goes through …
   ⑥ gateway (role-gated MCP: 6 tools; main has NO fact_submit; verifier read-only)

human interface: ⑦ workbench (object inspection · scoped requests · run controls · outcome traces)
               ⑧ ops/runtime (bootstrap · services · doctor · config)
output: write-paper (publication) · human-summary (progress report) — each rendered by an isolated codex

⑨ research control (ProblemContract · Obligation/Route graph · recurrence ·
context compiler · transparent scheduler/leases · bounded compute · acceptance/checkpoints/evaluation)
— reads the truth path but never becomes a second Fact admission path
```

---

## 2. Folder layout

```
Euler/
├─ ARCHITECTURE.md              this file (map + invariants + interface contract)
├─ README.md   pyproject.toml   top-level intro + the installable `euler` package
├─ .gitignore  .github/         repository hygiene + portable CI
├─ config/                      env templates (BYO key; only *.env.example committed)
├─ euler/                       THE ENGINE (installable Python package)
│  ├─ core/                     ⑤ truth: schema · factgraph · global/local memory · bm25 · glossary
│  ├─ gateway/                  ⑥ role-gated MCP: 6 tools · role table (roles.py)
│  ├─ verify/                   ④ verification HTTP service · prechecks · cold-start agent launcher
│  ├─ execution/                ③ worker swarm: round loop · project/worker lifecycle + layout
│  ├─ strategy/                 ② consult gateway (gpt_pro|claude_api|claude_code|off transport)
│  ├─ orchestration/            ① the `euler` CLI verbs
│  ├─ control/                   ⑨ progress/budget/evidence/acceptance/evaluation control system
│  ├─ integrations/             arXiv theorem search (Matlas)
│  ├─ observability/            workbench and opt-in research controls
│  ├─ authoring/                shared main-only renderer primitives: the one-shot isolated codex driver + common helpers
│  ├─ write_paper/              write-paper MCP service: assembler + tools (drives `euler.authoring`)
│  └─ human_summary/            human-summary MCP service: scrubbing assembler + tool (drives `euler.authoring`)
├─ agents/                      AGENT CONTRACTS + SHIPPED SKILLS (data, not Python)
│  ├─ contracts/                main_agent.md · worker.md · verifier.md
│  └─ skills/
│     ├─ initialize/ consult/ elaboration/
│     ├─ human-summary/         report recipe + renderer assets
│     ├─ worker/                proving and analysis skills
│     ├─ verify/                3 verify skills
│     └─ write-paper/           recipe, drivers, role prompts, templates, and house style
├─ bin/                         thin wrappers: euler · euler-mcp · write-paper-mcp · human-summary-mcp · codex · consult
├─ scripts/                     bootstrap · doctor · services · env · setup/check-codex · start-verify/-dashboard · recover · install-tex
├─ docs/                        human docs: getting started · concepts · operating guide · security & trust · …
└─ examples/                    unattended-ops examples + a small toy project
```

---

## 3. Design invariants (must not regress)

1. Three memory tiers, one correctness boundary: local (private) → global
   (shared awareness) → fact graph (the only truth). A proof may build only on
   `fact_id`s; global memory is never a correctness source.
2. Permission is enforced by which tools a role can even see (the gateway role
   table), not by prompt convention. `main` cannot `fact_submit`; `verifier` is
   read-only.
3. The verifier is the sole write-gate. A fact exists only if a `correct` verdict
   came back; the gate lives in the `fact_submit` code path, not in prose.
4. Content-addressed, cascade-revocable fact graph. `fact_id` hashes content
   (problem_id + predecessors + glossary_introduces + statement + proof);
   `external_refs` is deliberately excluded so the paper pipeline can rewrite
   citations without breaking the DAG.
5. Autonomy and resumability. Workers run detached and recover their registered progress and context from persisted memory at round boundaries.
6. The strategy consult is the brain. Between rounds the main agent consults a
   top-tier reasoning model (gpt-5.5-pro over the `gpt_pro` transport, or
   claude-sonnet-4-5 over the `claude_api` / `claude_code` transports) to set direction;
   its reply becomes the swarm's `master_guidance`. Transport is `gpt_pro` (default),
   `claude_api`, `claude_code`, or `off` (no key — the main agent reasons on its own). The consult is not
   optional — it is how the swarm gets steered.
7. Portable and BYO. No hardcoded absolute paths, no committed secrets; keys come
   from gitignored `config/*.env` (templates committed as `*.example`).
8. Scoped author context. Paper and report writers receive dedicated contexts assembled from selected research records and the target audience's requirements. Internal identifiers and operational details are filtered during context assembly.
9. Provider isolation. Codex, Claude Code, Grok, and Cursor use one provider-neutral
   request/command protocol while retaining separate binaries, session recovery,
   MCP configuration, and credential scrubbing. Every mathematical claim reaches
   the Fact Graph only through the same verifier-gated submission path.

---

## 4. Interfaces & ports — the coordination contract

> **Rule:** these rows are the seams where two modules meet. If a change touches a
> row, update both ends in the same change. Ports and contract shapes are pinned:
> one side must treat the other's contract as fixed, and must not change a port or
> interface unilaterally.

**Network ports (loopback — do not renumber):**

| port | service | producer → consumer |
|---|---|---|
| 8091 | verify `/verify`, `/health` | `euler.gateway` `fact_submit` → `euler.verify` (via `EULER_VERIFY_URL`) |
| 8099 | dashboard | operator browser → `euler.observability` (views and configured research controls) |

**Cross-module contracts (both ends must agree):**

| contract | pinned shape | ends |
|---|---|---|
| MCP tool set + role gating | 6 tools; `roles.py` `ROLE_TOOLS` (main has NO `fact_submit`; verifier read-only) | `euler.gateway` ↔ worker/main/verifier agents |
| MCP launch | `python -m euler.gateway` + `EULER_ROLE` env | `euler.verify` launcher · generated worker provider configs · `bin/euler-mcp` → `euler.gateway` |
| verify HTTP | `POST /verify {statement,proof}` → `{verification_report,verdict,repair_hints}`; verdict ⟺ no critical_errors & no gaps | `euler.gateway.fact_submit` ↔ `euler.verify` |
| verify `/health` identity | `GET /health` → `{status, pid, euler_root}`; **transport only** (not a math verdict). Callers match `pid` to `runtime/run/verify.pid` and `euler_root` to this checkout (`ours`/`foreign`/`stale`/`down`) | `euler.execution.preflight` · `euler verify-health` · `scripts/env.sh` `euler_verify_health` ↔ `euler.verify` |
| proving preflight | fail-closed before spawn **and before every** `run_round`: python+MCP import, worker gateway table, MCP `role=worker` → live stdio `initialize`+`tools/list`, verify identity (pid **and** nonempty matching `euler_root`), unique contained store probe (no fact/memory write). Failure → `PRECHECK_FAILED` `{component,reason}`, no `codex exec` | `euler.execution.preflight` ↔ `euler start` / `euler.execution.loop` |
| round context + memory contract | proving rounds require TASK.md `branch_id`/`subgoal_id`; MCP env stamps non-forgeable project/worker/run/branch/subgoal; `gm_add` validates proof_attempt/obstacle/dead_end; `fact_submit` persists transport vs verdict (infra ⇒ `verdict` null). Close: success fact or linked attempts. Failures `TASK_CONTEXT_FAILED` / `MEMORY_CONTRACT_FAILED` | `euler.execution.context` · `round_contract` · `euler.gateway` |
| memory digest + receipts | orchestrator retrieves bounded `proof_attempt`/`obstacle`/`dead_end`/`counterexample`/`verification` (exact branch/subgoal then BM25 fill, top 5/kind, total cap) before every `run_round`; immutable start → launch → result → end receipts bind the prompt, backend execution and appended-ID watermarks. Failure → `RECEIPT_FAILED`, no backend launch / no next round | `euler.execution.memory_digest` · `launch_receipt` ↔ `euler.execution.loop` / `euler.core.GlobalMemory` |
| Euler control | append-only EventStore + CAS; exact ProblemContract/ResearchView; ContextSeed→ActionSpec→ContextRequest; multidimensional reservation/leases; checked compute candidates; final alignment and deterministic evaluation. It may reference active Facts/admission traces but has no Fact write API | `euler.control` ↔ read-only Euler adapter / existing gateway truth boundary |
| failure clusters | after a successful round-close, deterministic project-level clustering of NEW stamped records (blocked attempts / approach signature / consecutive ok+wrong verifications / obstacle signature). Infra `verdict=null` never counts. Project lock + append-only `failures/ledger.jsonl` + immutable `requests/<cluster_id>.json` sidecar; one request per cluster (`requested`/`resolved`/`superseded`). Sidecar-without-ledger is recovered as one `requested` event (fresh `seq`) after validating against the detected cluster; ledger-without-sidecar recreates the sidecar; corrupt/mismatch → `FAILURE_CONTROL_FAILED` (loop stops). Assigned worker's next kickoff injects `FAILURE SYNTHESIS MODE`. Synthesis must still satisfy stages 1–3 and append a matching `key_failures_summary` obstacle or dead_end **candidate** with `source_record_ids` exactly equal to request `member_ids`. Missing/fake IDs → `FAILURE_SYNTHESIS_FAILED`. Scheduler never confirms a dead end or writes facts. Operator: `euler failures <project>`, `euler status --json` | `euler.execution.failure_clusters` ↔ `euler.execution.loop` / `euler.orchestration` |
| fact id inputs | `problem_id + sorted(predecessors) + sorted(glossary) + normalized(statement,proof)`; **external_refs EXCLUDED** | `euler.core` ↔ everyone (write-paper reads `external_refs`) |
| global-memory kinds | the 11 `GLOBAL_KINDS` (incl. `master_guidance`/`elaboration`/`verification`) | `euler.core` ↔ agents · strategy · consult |
| consult JSON envelope | `{transport,reply,usage,cost_usd,…}`; always one envelope — a consult that could not run is `status="failed"` + `error`, never a traceback | `euler.strategy` CLI ↔ consult skill |
| write-paper prompt assets | codex role prompts + style read from `agents/skills/write-paper/` (via `EULER_WRITE_PAPER_SKILL_DIR`) | `euler.write_paper` assembler ↔ `agents/skills/write-paper/` |
| env-var contract | `EULER_* / CODEX_* / VERIFY_* / CONSULT_*` names; the codex CALL + env (bin/model/effort/PATH/`exec` prefix) is resolved through the shared `euler.codex` launcher: neutral `EULER_CODEX_BIN` / `EULER_CODEX_MODEL` / `EULER_CODEX_EFFORT` + per-service `EULER_{VERIFY,WRITE_PAPER,HUMAN_SUMMARY}_{MODEL,EFFORT}` overrides | `euler.codex` + `config/` + `scripts/env.sh` ↔ every codex-exec site (`euler.execution.loop` · `euler.verify.launcher` · `euler.authoring.driver`) |

## Human intervention module map (0.3 development)

- `control/human_objects.py`: canonical public identity, scope, dependency and evidence view.
- `control/campaign.py`: shared admission boundary, pause/resume/stop, durable checkpoints.
- `control/intervention.py`: preview, typed admission, derived contract/route/task, explicit focused start.
- `execution/human_control.py`: lease-specific local process cancellation and round time bounds.
- `observability/human_api.py`: server-owned project/worker policy, same-origin checks, and configured control permissions.
- `observability/static/human.js`: progressive object inspection, review/confirm/start, outcome and source navigation.

Requests bind object and workspace identity, source scope and native worker policy.
Researcher input is a proposal; original Facts, verification grades and original
contract versions are preserved. Original-target final audit remains separate
from accepting a scoped child result. The subsystem tests exercise these invariants; private development event exports are excluded from this distribution.
