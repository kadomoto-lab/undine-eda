#!/usr/bin/env bash
# API server on :8700 and Vite dev server on :5173 (proxies /api).
set -e
cd "$(dirname "$0")/.."                   # the repository root
(cd server && python3 -m undine serve --port 8700) &
API=$!
trap 'kill $API' EXIT
cd web && npx vite
