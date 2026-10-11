#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Start the Euler verify service (resident HTTP gateway on 127.0.0.1:$VERIFY_PORT;
# each POST /verify spawns a fresh configured verifier agent). Run via services.sh so it
# outlives the launching shell:  bash scripts/services.sh up verify
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
mkdir -p "$EULER_RUNTIME/logs" "$VERIFIER_RESULTS_DIR"
export EULER_CODEX_BIN="${EULER_CODEX_BIN:-$EULER_ROOT/bin/codex}"   # the provisioned codex wrapper
export CODEX_HOME
export CODEX_TIMEOUT_SECONDS="${CODEX_TIMEOUT_SECONDS:-900}"
export VERIFY_HOST="${VERIFY_HOST:-127.0.0.1}" VERIFY_PORT
verify_backend="${EULER_VERIFY_BACKEND:-grok}"
case "$verify_backend" in
  grok)
    verify_model="${EULER_VERIFY_GROK_MODEL:-grok-4.6}/${EULER_VERIFY_GROK_EFFORT:-high}"
    grok_bin="${EULER_GROK_BIN:-grok}"
    if [[ "$grok_bin" == */* ]]; then
      [ -x "$grok_bin" ] || { echo "[start-verify] Grok binary is not executable: $grok_bin" >&2; exit 2; }
    else
      command -v "$grok_bin" >/dev/null 2>&1 || { echo "[start-verify] Grok binary not found: $grok_bin" >&2; exit 2; }
    fi
    grok_home="$("$EULER_PY" -c 'from euler.verify.launcher import ensure_agent_home, _grok_home; ensure_agent_home(); print(_grok_home())')" \
      || { echo "[start-verify] could not provision isolated Grok verifier HOME" >&2; exit 2; }
    verify_agent_home="$("$EULER_PY" -c 'from euler.verify.launcher import _agent_home; print(_agent_home())')"
    env -i HOME="$verify_agent_home" GROK_HOME="$grok_home" \
      GROK_CURSOR_MCPS_ENABLED=false GROK_CLAUDE_MCPS_ENABLED=false \
      PATH="$PATH" USER="${USER:-}" LOGNAME="${LOGNAME:-}" LANG="${LANG:-C.UTF-8}" \
      "$grok_bin" --cwd "$verify_agent_home" inspect --json \
      | "$EULER_PY" -c 'import json,sys; d=json.load(sys.stdin); names=[x.get("name") for x in d.get("mcpServers",[])]; instructions=[x.get("path","") for x in d.get("projectInstructions",[])]; assert names == ["euler"], names; assert any(p.endswith("/euler/verify/agent/AGENTS.md") for p in instructions), instructions' \
      || { echo "[start-verify] isolated Grok verifier config failed closed" >&2; exit 2; }
    ;;
  cursor)
    verify_model="${EULER_VERIFY_CURSOR_MODEL:-cursor-grok-4.6-xhigh}"
    cursor_bin="${EULER_CURSOR_BIN:-cursor-agent}"
    if [[ "$cursor_bin" == */* ]]; then
      [ -x "$cursor_bin" ] || { echo "[start-verify] cursor binary is not executable: $cursor_bin" >&2; exit 2; }
    else
      command -v "$cursor_bin" >/dev/null 2>&1 || { echo "[start-verify] cursor binary not found: $cursor_bin" >&2; exit 2; }
    fi
    cursor_home="$("$EULER_PY" -c 'from euler.verify.launcher import ensure_agent_home, _cursor_home; ensure_agent_home(); print(_cursor_home())')" \
      || { echo "[start-verify] could not provision isolated Cursor verifier HOME" >&2; exit 2; }
    env -i HOME="$cursor_home" XDG_CONFIG_HOME="$cursor_home/.config" \
      PATH="$PATH" USER="${USER:-}" LOGNAME="${LOGNAME:-}" LANG="${LANG:-C.UTF-8}" \
      "$cursor_bin" status >/dev/null 2>&1 \
      || { echo "[start-verify] cursor-agent is not logged in inside the isolated verifier HOME" >&2; exit 2; }
    ;;
  codex)
    verify_model="${EULER_VERIFY_MODEL:-$EULER_CODEX_MODEL}/${EULER_VERIFY_EFFORT:-$EULER_CODEX_EFFORT}"
    ;;
  *)
    echo "[start-verify] unsupported EULER_VERIFY_BACKEND=$verify_backend" >&2
    exit 2
    ;;
esac
echo "[start-verify] port=$VERIFY_PORT backend=$verify_backend model=$verify_model"
exec "$EULER_PY" -m euler.verify
