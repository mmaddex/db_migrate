#!/usr/bin/env bash
# Exercise the operator scripts in local/ against the test harness, using
# the fake `render` CLI in test/fake-render (sandboxes become containers).
#
#   test/local_e2e.sh          (expects nothing else using the harness)
#   KEEP=1 test/local_e2e.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
COMPOSE=(docker compose -f test/docker-compose.yml)
export PATH="$ROOT/test/fake-render:$PATH"
WORK="$(mktemp -d)"
export FAKE_RENDER_STATE="$WORK/fake-render" MIGRATIONS_DIR="$WORK/migrations" MIGRATION=localtest
export IMAGE_REPO=pg-migrate IMAGE_TAG=test LOCAL_STATE="$WORK/state"
START=$(date +%s)
step() { printf '\n\033[1m=== [%3ss] %s\033[0m\n' "$(( $(date +%s) - START ))" "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
pass() { printf '  \033[32mok\033[0m %s\n' "$*"; }
cleanup() {
  rc=$?
  for c in $(docker ps -aq --filter label=fake-render=1); do docker rm -f "$c" >/dev/null; done
  [ "${KEEP:-0}" = 1 ] || "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
  [ $rc -eq 0 ] && printf '\n\033[32mLOCAL E2E PASSED in %ss\033[0m\n' "$(( $(date +%s) - START ))"
  exit $rc
}
trap cleanup EXIT
src() { "${COMPOSE[@]}" exec -T -e PGPASSWORD='app:pass' source psql -X -q -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U app -d app "$@"; }

step "start harness"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --wait --quiet-pull 2>&1 | tail -n 2
src -c "ALTER TABLE public.audit_log ADD COLUMN id bigserial PRIMARY KEY"

step "migration directory"
mkdir -p "$MIGRATIONS_DIR/localtest"
cp test/migration.test.env "$MIGRATIONS_DIR/localtest/migration.env"
echo "SANDBOX_PLAN=1c-2g" >"$MIGRATIONS_DIR/localtest/migration.conf"

step "import_image.sh / create_sandbox.sh"
local/import_image.sh
id="$(local/create_sandbox.sh)"
[ "$(cat "$MIGRATIONS_DIR/localtest/.sandbox_id")" = "$id" ] || fail ".sandbox_id not saved"
[ "$(local/create_sandbox.sh)" = "$id" ] || fail "create_sandbox.sh did not reuse the running sandbox"
pass "sandbox $id (reused on rerun)"

step "run.sh: preflight, schema, start_sync"
local/run.sh preflight.sh | tail -n 1
set +e; local/run.sh not-a-script.sh >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "run.sh accepted a bogus script"
local/run.sh schema.sh
local/run.sh start_sync.sh

step "status.sh --json"
for _ in $(seq 1 60); do
  phase="$(local/status.sh --json | jq -r '.status.phase')"
  [ "$phase" = copied ] && break
  sleep 2
done
[ "$phase" = copied ] || fail "phase $phase"
local/status.sh --json | jq -e '.sandbox.status == "running" and .status.healthy' >/dev/null || fail "status json"
pass "phase copied, healthy"

step "run.sh --detach build_indexes.sh"
POLL_S=2 local/run.sh --detach build_indexes.sh | tail -n 2

step "fetch_artifacts.sh"
dest="$(local/fetch_artifacts.sh)"
[ -f "$dest/artifacts/schema/indexes.sql" ] || fail "artifacts missing in $dest"
! grep -rIl 'migrator-pass' "$dest" || fail "artifacts contain a password"
pass "artifacts in $dest"

step "hotfix.sh"
local/hotfix.sh sandbox/status.sh 2>&1 | tail -n 1
grep -q HOTFIX "$MIGRATIONS_DIR/localtest/hotfix.log" || fail "hotfix not logged"

step "cutover, teardown guard, cleanup, teardown"
local/run.sh cutover.sh --confirm | tail -n 1
set +e; local/teardown.sh --confirm >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "teardown ran before cleanup_source"
pass "teardown refused before cleanup_source"
local/run.sh cleanup_source.sh --confirm | tail -n 1
local/teardown.sh --confirm
[ -z "$(docker ps -q --filter "name=fake-$id")" ] || fail "sandbox container still running"
pass "sandbox terminated"
