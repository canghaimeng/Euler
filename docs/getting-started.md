# Euler — Getting Started

For the current personal Codex/Grok team, visualization and state-recovery workflow,
start with the [user guide](DASHBOARD-GUIDE.md). This page documents the lower-level
worker and verifier deployment.

Provision a self-contained Euler deployment and bring it to a healthy, ready-to-run
state. Read `concepts.md` first for the mental model; after this, see
`operating-guide.md` to run your first project.

## Prerequisites (on the host)

- **Linux** host you are willing to let autonomous agents operate on (a dedicated
  VM / container / pod — see `security-and-trust.md`).
- `git`, `python3` (3.10+), `curl`, `tar`, `bash`. `bootstrap.sh` provisions
  everything else (Node, a venv, the codex CLI) into `runtime/` — no system-wide
  installs.
- Authenticated CLIs for the selected components: the active verifier defaults to
  `cursor-agent`; Codex-backed renderers or legacy workers still require either
  an OpenAI-compatible API key or a ChatGPT Pro/Plus subscription.
- *(Only for `write-paper`)* a LaTeX engine on `PATH` (`pdflatex`, or `tectonic` via
  `scripts/install-tex.sh`). Not needed for proving.

## 1. Provision the runtime

```bash
bash scripts/bootstrap.sh
```

Idempotent. Installs into gitignored `runtime/`:
- Node 22 → `runtime/node22` (default `NODE_VERSION` `v22.14.0`),
- a Python venv → `runtime/venv` (`mcp`, `fastapi`, `uvicorn`, `pydantic`, `openai`,
  plus the `euler` package itself as an editable install — the worker MCP gateway
  and the `bin/` wrappers run `python -m euler.*` from arbitrary cwds, so the
  package must be on the venv's path; the script validates the venv actually
  imports everything and rebuilds if the base interpreter went dangling),
- the codex CLI → `runtime/codex-npm` (`npm @openai/codex`),
- human-summary node deps (`markdown-it`/`katex`, soft — only for PDF rendering),
- `runtime/runtime.env` (machine paths that `scripts/env.sh` reads).

If `config/providers.env` already holds a real (non-placeholder) API key, bootstrap
also writes the codex `model_provider` for you.

## 2. Configure (copy the templates; fill YOUR values)

Secrets live **only** in these gitignored files (never committed):

```bash
cp config/euler.env.example config/euler.env    # host/account config (edit as needed)
cp config/providers.env.example config/providers.env     # BYO codex backend
```

Then configure the Codex backend for Codex-based components. The default verifier
uses the authenticated Grok Build CLI with `EULER_VERIFY_BACKEND=grok`,
`EULER_VERIFY_GROK_MODEL=grok-4.6`, and `EULER_VERIFY_GROK_EFFORT=high`.
Cursor and Codex verifier backends are explicit rollback choices only:

- **API key (recommended, no login):** in `config/providers.env` set
  `CODEX_BACKEND=api`, `CODEX_API_BASE_URL=<your OpenAI-compatible endpoint>`,
  `CODEX_API_MODEL=gpt-5.5`, `EULER_CODEX_API_KEY=<your key>`. Then
  `bash scripts/setup-codex.sh api` (bootstrap already did this if the key was
  present) writes the provider — the key is read from the env var at run time, never
  stored in a config file.
- **ChatGPT subscription:** in `config/euler.env` set `CODEX_BACKEND=chatgpt`, then
  `bash scripts/setup-codex.sh login` and follow the device-auth flow.

And your **strategy consult transport** in `config/euler.env`
(`EULER_CONSULT_TRANSPORT`): `gpt_pro` (paid OpenAI-compatible, the default — fill
`EULER_CONSULT_API_KEY` / `_BASE_URL` / `_MODEL`), `claude_api` (the Anthropic API
via the native SDK — per-token billing to your `EULER_CONSULT_CLAUDE_API_KEY`,
cost metered from real usage), `claude_code` (your Claude
subscription via the Claude Code CLI — no separate API key: each consult is metered
into the spend ledger at the `EULER_CONSULT_CLAUDE_CODE_PRICE_*` estimate rates), or
`off` (the main agent reasons on its own). See `configuration.md` for the full
variable list.

## 3. Confirm the codex backend is reachable

```bash
bash scripts/check-codex.sh
```

It is **backend-aware**, exits `0` on success, and appends a trace line to
`runtime/logs/codex-health.jsonl`:
- **api** backend → makes one cheap live call to `CODEX_API_BASE_URL` (reads
  `EULER_CODEX_API_KEY`) and scans recent logs; `ok codex API reachable`.
- **chatgpt** backend → there is no API endpoint to ping, so it probes the codex
  login instead and prints `ok codex backend: chatgpt login active (Logged in using
  ChatGPT)`.

## 4. Bring up the verify service (REQUIRED)

```bash
bash scripts/services.sh up verify
```

**Without the verify service, `fact_submit` fails and no facts are ever produced.**
`services.sh` `setsid`-detaches it so it survives your shell. See `operations.md`.

## 5. Health check

```bash
euler doctor                    # proving-readiness (python, gateway tools, verify identity)
euler verify-health             # check verification service identity
bash scripts/doctor.sh          # fuller ops check (codex backend, TeX, Chrome)
```

Reports green / `FAIL` / `warn` across config, python + deps, node, the codex
wrapper + backend, a live verify `/health` probe, and soft checks for `pdflatex`
(write-paper) and Chrome/Chromium (human-summary PDF).

A real healthy run (chatgpt backend, no TeX installed — the `pdflatex` `warn` is a
soft, write-paper-only dependency):

```
== Euler doctor ==
EULER_ROOT=/home/you/Euler
  ok   config/euler.env present
  ok   config/providers.env present
  ok   python: .../runtime/venv/bin/python
  ok   python dep: mcp
  ok   python pkg: euler (importable from any cwd)
  ok   python deps: fastapi/uvicorn/pydantic
  ok   python dep: openai (gpt_pro consult)
  ok   python dep: anthropic (claude_api consult)
  ok   node: .../runtime/node22/bin/node
  ok   codex: codex-cli 0.142.5
  ok   codex login ok (/home/you/codex-home)
  ok   verify service up :8091 (ours)
  warn no pdflatex on PATH (write-paper PDF render needs it; set TEX_ENGINE or install TeX)
  ok   chrome: /usr/bin/chromium-browser (human-summary PDF)
consult transport: gpt_pro
done.
```

On the **api** backend the two codex lines instead read `codex backend: api provider
configured` + `codex API live ping ok`.

## 6. Create and initialize a project

Use the installed Euler CLI directly. `euler new` creates provider-specific
worker homes and MCP configuration under the gitignored runtime; the repository
does not rely on editor- or agent-client instruction files at its root.

```bash
euler new MY_PROJECT
euler init MY_PROJECT --contract contract.json --policy policy.json
```

Continue with `operating-guide.md` to propose, schedule, and launch work.

## Troubleshooting

- Backend flaky? `bash scripts/check-codex.sh` (history in
  `runtime/logs/codex-health.jsonl`).
- Host restarted? `bash scripts/recover.sh` (see `operations.md`).
- Anything red in `doctor.sh` — fix it before starting workers.
