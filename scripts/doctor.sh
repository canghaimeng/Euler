#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
# Health check: config, runtime, python deps, codex, verify service. Read-only.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
ok(){ printf '  \033[32mok\033[0m   %s\n' "$1"; }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
wn(){ printf '  \033[33mwarn\033[0m %s\n' "$1"; }
echo "== Euler doctor =="
echo "EULER_ROOT=$EULER_ROOT"
[ -f "$EULER_ROOT/config/euler.env" ] && ok "config/euler.env present" || wn "config/euler.env missing (copy from .example)"
[ -f "$EULER_ROOT/config/providers.env" ] && ok "config/providers.env present" || wn "config/providers.env missing (copy from .example, fill BYO key)"
[ -n "${EULER_PY:-}" ] && [ -x "$EULER_PY" ] && ok "python: $EULER_PY" || no "no python (run bootstrap.sh)"
"$EULER_PY" -c 'from euler._mcp import FastMCP' 2>/dev/null && ok "python dep: mcp" || no "python dep: mcp missing or incompatible"
(cd / && "$EULER_PY" -c 'import euler' 2>/dev/null) && ok "python pkg: euler (importable from any cwd)" || no "euler package not installed in the venv — worker MCP gateway and bin/ wrappers fail off the repo root (run bootstrap.sh)"
"$EULER_PY" -c 'import fastapi, uvicorn, pydantic' 2>/dev/null && ok "python deps: fastapi/uvicorn/pydantic" || no "verify-service deps missing"
"$EULER_PY" -c 'import openai' 2>/dev/null && ok "python dep: openai (gpt_pro consult)" || wn "openai missing (gpt_pro consult disabled)"
"$EULER_PY" -c 'import anthropic' 2>/dev/null && ok "python dep: anthropic (claude_api consult)" || wn "anthropic missing (claude_api consult disabled)"
if [ -n "${EULER_NODE:-}" ] && [ -x "${EULER_NODE:-/nonexist}" ]; then ok "node: $EULER_NODE"; else wn "node not provisioned"; fi
if "$EULER_ROOT/bin/codex" --version >/dev/null 2>&1; then ok "codex: $("$EULER_ROOT/bin/codex" --version 2>/dev/null)"; else no "codex wrapper not working (run bootstrap.sh)"; fi
if [ "${CODEX_BACKEND:-api}" = "api" ]; then
  if [ -f "$CODEX_HOME/config.toml" ] && grep -q euler_api "$CODEX_HOME/config.toml" 2>/dev/null; then ok "codex backend: api provider configured"; else no "codex api provider missing (scripts/setup-codex.sh api)"; fi
  bash "$EULER_ROOT/scripts/check-codex.sh" >/dev/null 2>&1 && ok "codex API live ping ok" || wn "codex API ping FAILED (bash scripts/check-codex.sh — endpoint down/rate-limited?)"
else
  env CODEX_HOME="$CODEX_HOME" "$EULER_ROOT/bin/codex" login status >/dev/null 2>&1 && ok "codex login ok ($CODEX_HOME)" || wn "codex not logged in (scripts/setup-codex.sh login)"
fi
case "$(euler_verify_health)" in
  ours)    ok "verify service up :$VERIFY_PORT (ours)" ;;
  foreign) no "verify port :$VERIFY_PORT answered by a FOREIGN process — another deployment holds this port; set a distinct VERIFY_PORT/DASHBOARD_PORT in config/euler.env (fact_submit would post to the wrong verifier)" ;;
  stale)   wn "verify service down :$VERIFY_PORT (stale pidfile; scripts/services.sh up verify)" ;;
  *)       wn "verify service down :$VERIFY_PORT (scripts/services.sh up verify)" ;;
esac
# write-paper PDF render (soft): TEX_ENGINE override, else pdflatex on PATH.
TEX="${TEX_ENGINE:-pdflatex}"
command -v "$TEX" >/dev/null 2>&1 && ok "latex: $TEX ($($TEX --version 2>/dev/null | head -1))" || wn "no $TEX on PATH (write-paper PDF render needs it; set TEX_ENGINE or install TeX)"
# human-summary PDF render (soft): EULER_CHROME_BIN, else chrome/chromium on PATH.
CHROME="${EULER_CHROME_BIN:-}"; [ -z "$CHROME" ] && CHROME="$(command -v chromium chromium-browser google-chrome google-chrome-stable 2>/dev/null | head -1)"
{ [ -n "$CHROME" ] && [ -x "$CHROME" ]; } && ok "chrome: $CHROME (human-summary PDF)" || wn "no Chrome/Chromium (human-summary PDF render needs it; set EULER_CHROME_BIN or install)"
echo "consult transport: $EULER_CONSULT_TRANSPORT"
echo "done."
