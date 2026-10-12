# Euler — Configuration Reference

<!-- language-nav -->
[English](configuration.md) · [简体中文](configuration.zh.md) · [Back to Euler](../README.md)
<!-- /language-nav -->

All host- and account-specific configuration lives in gitignored `config/*.env`
files; **no path or secret is hardcoded** elsewhere. `scripts/env.sh` sources the
chain and fills defaults:

```
config/providers.env  →  config/euler.env  →  runtime/runtime.env  →  built-in defaults
   (BYO backend)      (host/account)      (machine paths, auto)   (scripts/env.sh)
```

Only `*.env.example` templates are committed; copy them to the real names and edit.
The `bin/` wrappers source `env.sh` for you. Values below are the defaults from
`scripts/env.sh` / `config/euler.env.example`.

## Frontier checks and audits

| Variable | Purpose |
|---|---|
| `EULER_KERNEL_LAKE_BIN` / `EULER_KERNEL_LEAN_BIN` | Host-configured checker tools; public kernel requests cannot override executables |
| `EULER_FINAL_AUDIT_RESOURCES` | JSON `ResourceVector` reservation for live proof/refutation audits; positive explicit monetary/token allowances required when those project dimensions are limited |
| `EULER_VERIFY_TIMEOUT` | HTTP timeout in seconds; also determines live final-audit wall reservation |
| `EULER_VERIFY_CONFIG_ID` | Operator version identifier for a changed verifier deployment at the same endpoint |
| `EULER_GROK_AUTH_FILE` | Source auth file linked into isolated Grok homes |

See [Architecture](../ARCHITECTURE.md) for the control-layer interfaces and
verification boundaries.

## Codex backend (workers + verifier)

| variable | default | meaning |
|---|---|---|
| `CODEX_BACKEND` | `api` | `api` (BYO OpenAI-compatible key) or `chatgpt` (your ChatGPT login) |
| `CODEX_HOME` | `runtime/codex-home` | codex auth/config home (gitignored) |
| `CODEX_API_BASE_URL` | — | (api) your OpenAI-compatible Responses endpoint |
| `CODEX_API_MODEL` | `gpt-5.5` | (api) backend model |
| `EULER_CODEX_API_KEY` | — | (api) key, **read at run time**, never stored in a file |

These live in `config/providers.env`. See `getting-started.md` §2 and
`scripts/setup-codex.sh`.

## Strategy consult (the system's brain)

| variable | default | meaning |
|---|---|---|
| `EULER_CONSULT_TRANSPORT` | `gpt_pro` | `gpt_pro` \| `claude_api` \| `claude_code` \| `off` |
| `EULER_CONSULT_API_KEY` | — | (gpt_pro) key for the OpenAI-compatible Responses API |
| `EULER_CONSULT_BASE_URL` | `https://api.openai.com/v1` | (gpt_pro) endpoint |
| `EULER_CONSULT_MODEL` | `gpt-5.5-pro` | (gpt_pro) model |
| `EULER_CONSULT_BACKGROUND` | `1` | (gpt_pro) send `background=true`; `0` for a gateway that rejects it (per-call: `--background off`) |
| `EULER_CONSULT_STORE` | `0` | (gpt_pro) send `store=false`; `1` for a gateway that requires stored responses (per-call: `--store on`) |
| `EULER_CONSULT_CLAUDE_CODE_MODEL` | `claude-sonnet-4-5` | (claude_code) model via the `claude` CLI |
| `EULER_CONSULT_CLAUDE_CODE_BIN` | `claude` | (claude_code) path to the CLI |
| `EULER_CONSULT_CLAUDE_CODE_MAX_WALL` | `1800` | (claude_code) hard wall-clock cap per consult (s) |
| `EULER_CONSULT_CLAUDE_CODE_PRICE_IN` | `10.0` | (claude_code) ledger estimate, USD per 1M input tokens |
| `EULER_CONSULT_CLAUDE_CODE_PRICE_OUT` | `50.0` | (claude_code) ledger estimate, USD per 1M output tokens |
| `EULER_CONSULT_CLAUDE_API_KEY` | — (falls back to `ANTHROPIC_API_KEY`) | (claude_api) BYO Anthropic API key |
| `EULER_CONSULT_CLAUDE_API_BASE_URL` | Anthropic default | (claude_api) only for a proxy |
| `EULER_CONSULT_CLAUDE_API_MODEL` | `claude-sonnet-4-5` | (claude_api) any Claude model |
| `EULER_CONSULT_CLAUDE_API_FALLBACK` | `claude-opus-4-1` | (claude_api) refusal-fallback model; `off` disables |
| `EULER_CONSULT_CLAUDE_API_PRICE_IN` | `10.0` | (claude_api) USD per 1M input tokens (real usage) |
| `EULER_CONSULT_CLAUDE_API_PRICE_OUT` | `50.0` | (claude_api) USD per 1M output tokens (real usage) |

- `gpt_pro` = a paid, per-token OpenAI-compatible model. `claude_api` = the
  Anthropic API via the native SDK (per-token, BYO key; cost from real usage).
  `claude_code` = your Claude subscription via the Claude Code CLI (`claude -p`).
  `off` = the main agent reasons on its own, no consult.
- The `claude_code` consult runs **isolated**: a throwaway cwd, no settings and no MCP
  servers loaded (`--setting-sources "" --strict-mcp-config` — needs a recent
  `claude` CLI), web-only tools, and the prompt on stdin (never argv, which is
  world-readable on a shared host). It sees the elaboration and the public web,
  nothing else.
- Consult effort is selected per call with `--effort`. Accepted values are
  `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. All transports support
  through `max`; a `gpt_pro` `max` request is never silently retried without its
  requested reasoning effort.

## Native worker providers

Workers launch through `euler.providers`. The role file stores `PROVIDER=`, never
a hidden model-prefix. Native names:

| `PROVIDER` | binary env | default binary | notes |
|---|---|---|---|
| `codex` | `EULER_CODEX_BIN` | `codex` | `codex exec` |
| `claude-code` | `EULER_CLAUDE_CODE_BIN` | `claude` | `--print` / `--resume`; wrappers such as `claf` allowed |
| `grok` | `EULER_GROK_BIN` | `grok` | `--single` by default; `GROK_RESUME_SESSION` / `EULER_GROK_PERSISTENT` enable resume |
| `cursor` | `EULER_CURSOR_BIN` | `cursor-agent` | `-p`, workspace/add-dir, MCP approval, sandbox disabled |

Provider API keys must not appear in another provider's child environment.

## Models & reasoning effort

The verifier selects `EULER_VERIFY_BACKEND=grok|codex|cursor`; there is no silent
fallback. Grok Build is the default; Codex and Cursor are explicit rollback
backends only.

| variable | default | applies to |
|---|---|---|
| `EULER_CODEX_BIN` | `<repo>/bin/codex`, else `codex` on PATH | all codex calls |
| `EULER_CODEX_MODEL` | `gpt-5.5` | neutral default (all sites) |
| `EULER_CODEX_EFFORT` | `xhigh` | neutral default effort (all sites) |
| `EULER_VERIFY_BACKEND` | `grok` | verifier backend; `cursor` and `codex` are explicit rollback only |
| `EULER_GROK_BIN` | `grok` | Grok Build verifier/worker CLI |
| `EULER_VERIFY_GROK_MODEL` / `_EFFORT` | `grok-4.6` / `high` | active Grok verifier knobs |
| `EULER_VERIFY_GROK_HOME` | `<EULER_ROOT>/runtime/grok-verifier-home` | isolated verifier runtime/auth home |
| `EULER_CURSOR_BIN` / `EULER_VERIFY_CURSOR_MODEL` | `cursor-agent` / `cursor-grok-4.6-xhigh` | legacy Cursor rollback only |
| `EULER_VERIFY_MODEL` / `_EFFORT` | neutral | Codex verifier rollback knobs |
| `EULER_WRITE_PAPER_MODEL` / `_EFFORT` | neutral | paper renderer |
| `EULER_HUMAN_SUMMARY_MODEL` / `_EFFORT` | neutral | human-summary renderer |

## Ports (all loopback)

| variable | default | service |
|---|---|---|
| `VERIFY_PORT` | `8091` | verify service (`127.0.0.1`) |
| `DASHBOARD_PORT` | `8099` | read-only dashboard (`127.0.0.1`) |
| `EULER_VERIFY_URL` | `http://127.0.0.1:8091/verify` | where `fact_submit` posts |
| `VERIFY_HOST` | `127.0.0.1` | verify bind host (keep loopback — see security doc) |

## Runtime data locations (gitignored, under `runtime/`)

| variable | default | holds |
|---|---|---|
| `EULER_RUNTIME` | `<repo>/runtime` | the whole self-contained runtime |
| `EULER_AGENTS_ROOT` | `runtime/projects` | where `euler new` puts projects |
| `VERIFIER_RESULTS_DIR` | `runtime/verify-runs` | per-verification run logs |
| `EULER_PY` | `runtime/venv/bin/python` (else system `python3`) | the engine's Python |

## Worker loop pacing (optional; engine defaults are sane)

| variable | default | meaning |
|---|---|---|
| `EULER_ROUND_HARD_TIMEOUT` | `14400` (4h) | per-round wall-clock cap |
| `EULER_MAX_ROUNDS` | `0` (unlimited) | round backstop |
| `EULER_MAX_CONSEC_FAILURES` | `5` | bail after N consecutive failed rounds |
| `EULER_ROUND_BEAT` | `5` | seconds between rounds |

## Rendering & misc

| variable | default | meaning |
|---|---|---|
| `EULER_CHROME_BIN` | (auto-detect) | headless Chrome/Chromium for human-summary PDF |
| `TEX_ENGINE` | `pdflatex` | write-paper LaTeX engine (`xelatex`/`lualatex`/`tectonic`) |
| `EULER_WRITE_PAPER_RUN_LOG` | on | per-call write-paper diagnostic logs (`0` disables) |
| `EULER_PAPER_VERIFY_WHOLE_DOC_CAP` | `700000` | char budget for one whole-paper math-verify call; over it the tool reports `too_large` (the main agent decomposes — the tool never auto-splits) |

## LaTeX-git push (write-paper deliver, optional)

In `config/latex-git.env` (gitignored): `LATEX_GIT_URL`, `LATEX_GIT_TOKEN`, and
optional `LATEX_GIT_AUTHOR_NAME` / `_EMAIL`. Pushing outward is an operator-gated
action.

---

Ports and the verify HTTP contract are **pinned** cross-module interfaces
(`../ARCHITECTURE.md` §4) — do not renumber `8091`/`8099` without changing both
ends. See `operations.md` to run the services and `cli-and-tools.md` for the
commands that use these.
