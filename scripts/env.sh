#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# =============================================================================
# Euler environment — source this in any shell that talks to the system.
#
#   source <repo>/scripts/env.sh
#
# Resolves all paths from config/providers.env + config/euler.env + runtime/
# runtime.env, then exports sane defaults and puts bin/ + the provisioned node +
# venv on PATH. The bin/ wrappers source this for you, so `euler`, `consult`,
# `codex` work without you sourcing it manually. Sourcing twice is harmless
# (idempotent).
# =============================================================================

# Self-locate the repo root (this file lives at <repo>/scripts/env.sh).
EULER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export EULER_ROOT

# 0) codex backend (BYO OpenAI-compatible endpoint + key; gitignored)
if [ -f "$EULER_ROOT/config/providers.env" ]; then
  set -a; . "$EULER_ROOT/config/providers.env"; set +a
fi

# 1) user config (accounts / models / ports / toggles) — overrides the codex file
if [ -f "$EULER_ROOT/config/euler.env" ]; then
  set -a; . "$EULER_ROOT/config/euler.env"; set +a
fi

# 2) machine-derived paths written by bootstrap.sh (node, codex.js, venv)
if [ -f "$EULER_ROOT/runtime/runtime.env" ]; then
  set -a; . "$EULER_ROOT/runtime/runtime.env"; set +a
fi

# 3) defaults for anything still unset
export EULER_RUNTIME="${EULER_RUNTIME:-$EULER_ROOT/runtime}"
export EULER_AGENTS_ROOT="${EULER_AGENTS_ROOT:-$EULER_RUNTIME/projects}"
export VERIFIER_RESULTS_DIR="${VERIFIER_RESULTS_DIR:-$EULER_RUNTIME/verify-runs}"
export CODEX_HOME="${CODEX_HOME:-$EULER_RUNTIME/codex-home}"
export VERIFY_PORT="${VERIFY_PORT:-8091}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8099}"
export EULER_VERIFY_URL="${EULER_VERIFY_URL:-http://127.0.0.1:${VERIFY_PORT}/verify}"
export EULER_CODEX_MODEL="${EULER_CODEX_MODEL:-${CODEX_API_MODEL:-gpt-5.5}}"   # neutral default model for every codex call (defers to the api backend model)
export EULER_CODEX_EFFORT="${EULER_CODEX_EFFORT:-xhigh}"   # neutral default reasoning effort
export EULER_CONSULT_TRANSPORT="${EULER_CONSULT_TRANSPORT:-gpt_pro}"   # gpt_pro | claude_api | claude_code | off
export EULER_CHROME_BIN="${EULER_CHROME_BIN:-}"        # headless Chrome/Chromium for human-summary PDF (empty = auto-detect)
export CODEX_BACKEND="${CODEX_BACKEND:-api}"            # api (BYO key) | chatgpt (your login)

# 4) PATH: bin wrappers first, then the provisioned node + venv (if bootstrapped)
_euler_path="$EULER_ROOT/bin"
[ -n "${EULER_NODE_BIN:-}" ] && [ -d "$EULER_NODE_BIN" ] && _euler_path="$_euler_path:$EULER_NODE_BIN"
[ -n "${EULER_VENV:-}" ]     && [ -d "$EULER_VENV/bin" ] && _euler_path="$_euler_path:$EULER_VENV/bin"
case ":$PATH:" in *":$_euler_path:"*) : ;; *) export PATH="$_euler_path:$PATH" ;; esac

# 5) the python the engine runs on (venv if bootstrapped, else system python3)
if [ -n "${EULER_VENV:-}" ] && [ -x "$EULER_VENV/bin/python" ]; then
  export EULER_PY="$EULER_VENV/bin/python"
else
  export EULER_PY="${EULER_PY:-$(command -v python3 || true)}"
fi

# Worker children must not inherit Sol consult secrets. Run AFTER config load so
# a later euler.env cannot smuggle tokens back in. Does not touch xAI keys.
if [ "${EULER_ROLE:-}" = "worker" ]; then
  unset SOL_GATE_TOKEN EULER_CONSULT_API_KEY SOL_CODEX_HOME SOL_CODEX_BIN
fi

# silent unless EULER_ENV_VERBOSE=1
if [ "${EULER_ENV_VERBOSE:-0}" = "1" ]; then
  echo "EULER_ROOT=$EULER_ROOT"
  echo "EULER_PY=$EULER_PY"
  echo "EULER_AGENTS_ROOT=$EULER_AGENTS_ROOT"
  echo "EULER_VERIFY_URL=$EULER_VERIFY_URL"
  echo "CODEX_HOME=$CODEX_HOME"
  echo "consult transport=$EULER_CONSULT_TRANSPORT"
fi

# 6) verify-service identity probe (shared by doctor.sh / services.sh).
# A bare `/health` 200 does NOT prove the responder is OURS: on a shared host a
# second Euler deployment can hold the same VERIFY_PORT, and its verifier would
# answer our probe (a false "up" that silently routes fact_submit to the wrong
# verifier). This classifies the port by matching BOTH the pid and canonical
# euler_root self-reported by /health. Echoes one word + sets a code:
#   ours      (0) -> HTTP 200 status=ok, matching pid and nonempty euler_root
#   foreign   (3) -> responder identity is missing or does not match
#   stale     (4) -> our pidfile process is dead and nothing answers the port
#   down      (5) -> nothing answers (with or without a live pidfile process)
#   unhealthy (6) -> matching identity, but HTTP/status is unhealthy
euler_verify_health(){
  local url="http://127.0.0.1:${VERIFY_PORT}/health"
  local pf="$EULER_RUNTIME/run/verify.pid"
  local our_pid raw resp http_code fields health_pid health_status health_root expected_root
  our_pid="$(cat "$pf" 2>/dev/null || true)"
  raw="$(curl -sS --max-time 5 -w '\n%{http_code}' "$url" 2>/dev/null || true)"
  http_code="${raw##*$'\n'}"
  resp="${raw%$'\n'*}"
  if [ -z "$resp" ]; then
    if [ -n "$our_pid" ] && kill -0 "$our_pid" 2>/dev/null; then echo down; return 5; fi
    { [ -n "$our_pid" ]; } && { echo stale; return 4; }
    echo down; return 5
  fi

  # EULER_PY is established above. Parse JSON rather than using regexes so a
  # malformed or type-confused identity fails closed.
  fields="$(printf '%s' "$resp" | "$EULER_PY" -c '
import json, os, sys
try:
    obj = json.load(sys.stdin)
    if not isinstance(obj, dict):
        raise ValueError
    pid = obj.get("pid")
    status = obj.get("status")
    root = obj.get("euler_root")
    print(pid if isinstance(pid, int) else "")
    print(status if isinstance(status, str) else "")
    print(os.path.realpath(root) if isinstance(root, str) and root.strip() else "")
except Exception:
    print("")
    print("")
    print("")
' 2>/dev/null || true)"
  health_pid="$(printf '%s\n' "$fields" | sed -n '1p')"
  health_status="$(printf '%s\n' "$fields" | sed -n '2p')"
  health_root="$(printf '%s\n' "$fields" | sed -n '3p')"
  expected_root="$(cd "$EULER_ROOT" 2>/dev/null && pwd -P || true)"

  if [ -n "$our_pid" ] && [ "$our_pid" = "$health_pid" ] \
     && [ -n "$health_root" ] && [ "$health_root" = "$expected_root" ]; then
    if [ "$http_code" = "200" ] && [ "$health_status" = "ok" ]; then
      echo ours; return 0
    fi
    echo unhealthy; return 6
  fi
  # Answered, but identity is incomplete or belongs to another deployment.
  echo foreign; return 3
}
