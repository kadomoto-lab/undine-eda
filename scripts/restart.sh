#!/usr/bin/env bash
# (Re)starts the API server in the background; log in $UNDINE_LOG (default /tmp/undine.log).
cd "$(dirname "$0")/.."                   # the repository root
PIDS=$(pgrep -f "python -m undine serve")
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null && sleep 1
# secrets (e.g. ANTHROPIC_API_KEY=...) live outside the repo: ~/.undine/env, readable only by you
ENVF="${UNDINE_ENV:-$HOME/.undine/env}"
if [ -f "$ENVF" ]; then set -a; . "$ENVF"; set +a; fi
if command -v pixi >/dev/null && [ -f pixi.toml ]; then RUN="pixi run"; else . .venv/bin/activate; RUN=""; fi
nohup $RUN python -m undine serve --port "${PORT:-8700}" > "${UNDINE_LOG:-/tmp/undine.log}" 2>&1 &
sleep 4
curl -s -o /dev/null -w "server: %{http_code}\n" http://127.0.0.1:${PORT:-8700}/api/tools
