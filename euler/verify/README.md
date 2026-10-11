# euler.verify — the verify service (sole write-gate)

An **informal-LLM proof verifier** behind a tiny HTTP gateway. It is the sole
authority on mathematical correctness: a worker's `fact_submit` (in `euler.gateway`)
calls it, and the candidate fact is written to the fact graph **iff** this service
returns `verdict: "correct"`.

It is **not** a formal / Lean checker — a cold-start LLM agent reads the
natural-language markdown proof (logic, theorem application, external-citation
checking) and returns a verdict. There is **no human in the loop by default** —
research-level target theorems still need expert review before being trusted.

## Black-box contract

```
POST /verify
  request : {"statement": <str, >=1 char>, "proof": <str, >=1 char>}   # application/json
  200     : {"verification_report": {"summary": str,
                                      "critical_errors": [{"location": str, "issue": str}, ...],
                                      "gaps":            [{"location": str, "issue": str}, ...]},
             "verdict": "correct" | "wrong",
             "repair_hints": str}                # "" iff verdict == "correct"
  400     : vacuous input, or a P1/P3/P5 pre-check match (see prechecks.py)
  422     : request-model validation (empty statement/proof)
  500     : verifier agent failed / wrote no output / invalid JSON / non-dict
  504     : verifier agent timed out (only if CODEX_TIMEOUT_SECONDS is set)

GET /health -> {"status": "ok", "pid": <int>, "euler_root": <str>}
                                                 # async; never queues behind /verify;
                                                 # pid + root identify this deployment
```

**Invariant (required by the verifier prompt and revalidated by the service):**
`verdict == "correct"` ⟺ `critical_errors == []` **and** `gaps == []`. This service
does not re-check the mathematics, but rejects malformed or internally
inconsistent verdict JSON before it can reach the write-gate.

## Modules
- `prechecks.py` — pure, offline-testable: vacuousness + P1/P3/P5 hard prohibitions
  (all env-toggleable, all purely additive — they can only *reject* more).
- `launcher.py` — cold-start backend launcher. The default `grok` backend runs
  Grok Build with `grok-4.6` / `high` in an isolated verifier workspace.
  `codex` and `cursor` remain explicit rollback backends. All expose only Euler MCP role `verifier`, use
  an atomic run-id, and return `verification.json`.
- `service.py` — FastAPI app (`/verify`, `/health`).

## Run

```bash
python -m euler.verify          # 127.0.0.1:8091, default CODEX_TIMEOUT_SECONDS=900
```

Binds **loopback by default** (set `VERIFY_HOST=0.0.0.0` if the gateway runs on
another host). The selected backend CLI must already be authenticated. For Grok
Build, put `grok` on PATH or set `EULER_GROK_BIN`; for Cursor rollback, set
`EULER_CURSOR_BIN`; for Codex, configure the existing wrapper/account. There is
no silent fallback between backends. The Grok child receives a small environment
allowlist and a newly provisioned isolated `GROK_HOME`. The only host-derived
file linked into it is authentication; Grok may create its own runtime cache there, but user-global
MCPs, hooks, rules, skills, and unrelated credentials are not inherited.

## Configuration (env vars)

| var | default | meaning |
| --- | --- | --- |
| `VERIFY_HOST` / `VERIFY_PORT` (or `PORT`) | `127.0.0.1` / `8091` | bind addr (`python -m euler.verify`) |
| `VERIFY_AGENT_HOME` | `<this dir>/agent` | isolated verifier workspace (AGENTS.md + skills) |
| `VERIFIER_RESULTS_DIR` | `<this dir>/runs` | per-verification run dirs (`log.md` + `verification.json`) |
| `EULER_VERIFY_BACKEND` | `grok` | active backend; `cursor` and `codex` are explicit rollback only |
| `EULER_GROK_BIN` | `grok` | Grok Build CLI binary |
| `EULER_VERIFY_GROK_MODEL` / `EULER_VERIFY_GROK_EFFORT` | `grok-4.6` / `high` | Grok verifier model and effort |
| `EULER_GROK_AUTH_FILE` | `~/.grok/auth.json` | only host credential linked into isolated Grok HOME |
| `EULER_VERIFY_GROK_HOME` | `<EULER_ROOT>/runtime/grok-verifier-home` | isolated Grok verifier state |
| `EULER_CURSOR_BIN` | `cursor-agent` | legacy Cursor rollback CLI |
| `EULER_VERIFY_CURSOR_MODEL` | `cursor-grok-4.6-xhigh` | legacy Cursor rollback model |
| `EULER_CURSOR_AUTH_FILE` | `~/.config/cursor/auth.json` | only host credential linked into the isolated Cursor HOME |
| `EULER_VERIFY_CURSOR_HOME` | `<VERIFY_AGENT_HOME>/.cursor-home` | isolated HOME; excludes user-global MCPs/hooks/rules |
| `EULER_CODEX_BIN` | `<repo>/bin/codex` → `which codex` → bare `"codex"` | the codex binary; resolved via the shared `euler.codex` launcher |
| `EULER_VERIFY_MODEL` / `EULER_VERIFY_EFFORT` | `gpt-5.5` / `xhigh` | Codex-backend knobs |
| `CODEX_TIMEOUT_SECONDS` | `0` lib / **`900`** via `python -m euler.verify` | per-verification codex timeout |
| `VERIFY_MIN_STATEMENT_CHARS` / `VERIFY_MIN_PROOF_CHARS` / `VERIFY_MIN_PROOF_WORDS` | 10 / 30 / 5 | vacuousness thresholds |
| `VERIFY_REJECT_PROBLEM_MD_CITATIONS` / `VERIFY_REJECT_UNPROVEN_CONDITIONALS` / `VERIFY_REJECT_VAGUE_GESTURES` | `1` | toggle P1 / P3 / P5 (`0` disables) |

## How `fact_submit` reaches it
`euler.gateway`'s `fact_submit` POSTs `{statement, proof}` to `EULER_VERIFY_URL`
(e.g. `http://127.0.0.1:8091/verify`), writes the fact **iff** `verdict ==
"correct"`, and always records the outcome to global memory (kind `verification`).
Until this service is up and `EULER_VERIFY_URL` is set, `fact_submit` returns a
clear "verify service not wired" error.

Before launching the verifier, the service writes an atomic `request.json` in
the run directory. Its fingerprint covers the exact length-delimited
`(statement, proof)` pair. A later identical request may reuse only a completed
result whose manifest matches and whose `verification.json` still passes schema
validation. Partial or malformed runs and legacy runs without a manifest are
ignored; changing either the statement or proof always triggers a fresh check.

## Trust assumptions (security)

- The Codex backend bypasses its sandbox; the Cursor backend uses forced tool
  approval so it can write the result artifact. `VERIFY_AGENT_HOME` (contract +
  skills) is **trusted input**; do not point it at untrusted content.
- It is an **LLM judge, not a formal (Lean) checker**, with **no human in the loop
  by default**; a `correct` verdict writes a permanent fact. Research-level target
  theorems need expert human review before being trusted.
- Binds **loopback** by default; the 900-second launcher default bounds each
  verifier-agent call.
