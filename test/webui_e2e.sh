#!/usr/bin/env bash
# Drive the web UI in a headless browser against the test harness, using the
# fake `render` CLI. Two migrations, each with its own sandbox container:
#   alpha  - the full runbook, preflight blocker fixed along the way
#   beta   - targets the database without the replica-role grant; preflight
#            must block it
#
#   test/webui_e2e.sh                # screenshots land in $SHOTS (default: test/screenshots)
#   KEEP=1 test/webui_e2e.sh
#
# Needs: docker compose, go, node with playwright (npm i playwright; npx playwright install chromium).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
COMPOSE=(docker compose -f test/docker-compose.yml)
WORK="$(mktemp -d)"
export SHOTS="${SHOTS:-$ROOT/test/screenshots}"
export FAKE_RENDER_STATE="$WORK/fake-render" LOCAL_STATE="$WORK/state" IMAGE_REPO=pg-migrate IMAGE_TAG=test
PORT="${PORT:-7799}"
mkdir -p "$SHOTS"

cleanup() {
  rc=$?
  [ -n "${UI_PID:-}" ] && kill "$UI_PID" 2>/dev/null || true
  for c in $(docker ps -aq --filter label=fake-render=1); do docker rm -f "$c" >/dev/null; done
  [ "${KEEP:-0}" = 1 ] || "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  [ "${KEEP:-0}" = 1 ] || rm -rf "$WORK"
  [ $rc -eq 0 ] && echo "WEBUI E2E PASSED"
  exit $rc
}
trap cleanup EXIT

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --wait --quiet-pull 2>&1 | tail -n 1

(cd webui && go build -o "$WORK/pgm-ui" .)
"$WORK/pgm-ui" -addr "127.0.0.1:$PORT" -repo "$ROOT" -migrations "$WORK/migrations" \
  -path-prepend "$ROOT/test/fake-render" -poll 3s >"$WORK/ui.log" 2>&1 &
UI_PID=$!
for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$PORT/api/system" >/dev/null 2>&1 && break; sleep 0.2; done

UI_URL="http://127.0.0.1:$PORT" WORK="$WORK" node test/webui_e2e.js || { tail -n 40 "$WORK/ui.log"; exit 1; }
