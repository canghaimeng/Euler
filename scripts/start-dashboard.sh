#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Read-only dashboard for one project (fact graph DAG + global memory + spend).
#   bash scripts/start-dashboard.sh <project_name> [port] [--config dashboard.json]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
NAME="${1:?usage: start-dashboard.sh <project_name> [port]}"
PORT="${2:-$DASHBOARD_PORT}"
PROJ="$EULER_AGENTS_ROOT/$NAME"
[ -d "$PROJ" ] || { echo "no such project: $PROJ" >&2; exit 1; }
echo "[dashboard] http://127.0.0.1:$PORT  project=$NAME"
if [ "$#" -ge 2 ]; then shift 2; else shift; fi
exec "$EULER_PY" -m euler.observability --project "$PROJ" --port "$PORT" "$@"
