#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Configure the codex backend used by the workers + the verifier.
#
#   bash scripts/setup-codex.sh api      # use your BYO OpenAI-compatible endpoint
#                                        # from config/providers.env — no login needed
#   bash scripts/setup-codex.sh login    # alternative: device-auth YOUR ChatGPT account
#   bash scripts/setup-codex.sh status   # show the active backend + a live ping
#
# `api` writes a model_provider into $CODEX_HOME/config.toml pointing at the
# OpenAI-compatible endpoint; the key is read at run time from the env var named
# by `env_key` (EULER_CODEX_API_KEY, exported by env.sh from config/providers.env).
# $CODEX_HOME is under runtime/ (gitignored) and rebuilt by bootstrap.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
mkdir -p "$CODEX_HOME"
export EULER_CODEX_BIN="${EULER_CODEX_BIN:-$EULER_ROOT/bin/codex}"
export PATH="$(dirname "$EULER_CODEX_BIN"):$PATH"

write_provider(){
  : "${CODEX_API_BASE_URL:?CODEX_API_BASE_URL not set (config/providers.env)}"
  : "${EULER_CODEX_API_KEY:?EULER_CODEX_API_KEY not set (config/providers.env)}"
  cat > "$CODEX_HOME/config.toml" <<EOF
# Auto-written by scripts/setup-codex.sh api — codex model backend for Euler.
# The API key is read at run time from the env var below (env.sh exports it from
# config/providers.env); it is NOT stored in this file.
model_provider = "euler_api"

[model_providers.euler_api]
name = "Euler codex API (${CODEX_API_MODEL:-gpt-5.5})"
base_url = "$CODEX_API_BASE_URL"
env_key = "EULER_CODEX_API_KEY"
wire_api = "responses"
# Long xhigh mathematical audits can reason silently for more than Codex's
# default SSE idle window.  Keep the transport alive for up to one hour; the
# authoring/verifier subprocess still has its separate 7200-second hard limit.
stream_idle_timeout_ms = 3600000
EOF
  echo "[setup-codex] wrote $CODEX_HOME/config.toml (provider euler_api -> $CODEX_API_BASE_URL, model ${CODEX_API_MODEL:-gpt-5.5})"
}

case "${1:-status}" in
  api)
    write_provider
    echo "[setup-codex] backend = api. Verify with: bash scripts/check-codex.sh"
    ;;
  login|apikey)
    # ChatGPT-subscription path: remove the API provider so codex uses its own
    # auth, then run codex's own device-auth login flow.
    [ -f "$CODEX_HOME/config.toml" ] && grep -q 'model_provider = "euler_api"' "$CODEX_HOME/config.toml" 2>/dev/null \
      && { rm -f "$CODEX_HOME/config.toml"; echo "[setup-codex] removed the api provider (switching to ChatGPT login)"; }
    echo "[setup-codex] launching codex login (device auth) — CODEX_HOME=$CODEX_HOME"
    # --device-auth: the code-entry flow that works on a headless server. The
    # default `codex login` flow needs a browser reaching localhost:1455 and
    # just hangs silently over plain ssh.
    exec env CODEX_HOME="$CODEX_HOME" "$EULER_CODEX_BIN" login --device-auth
    ;;
  status)
    echo "[setup-codex] CODEX_BACKEND=$CODEX_BACKEND  CODEX_HOME=$CODEX_HOME"
    if [ -f "$CODEX_HOME/config.toml" ] && grep -q euler_api "$CODEX_HOME/config.toml"; then
      echo "  provider: euler_api ($CODEX_API_BASE_URL, ${CODEX_API_MODEL:-gpt-5.5})"
    else
      echo "  provider: codex default (ChatGPT login) — $(env CODEX_HOME="$CODEX_HOME" "$EULER_CODEX_BIN" login status 2>&1 | head -1)"
    fi
    ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
