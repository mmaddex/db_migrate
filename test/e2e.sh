#!/usr/bin/env bash
# End-to-end test of the /opt/migrate scripts against the local harness.
#
#   test/e2e.sh            build, run, tear down
#   KEEP=1 test/e2e.sh     leave the containers running afterwards
#
# Env: SOURCE_PG_MAJOR (Heroku, default 16), TARGET_PG_MAJOR (Render,
# default 17), MIGRATOR_USER (root|postgres), PG_IMAGE, BASE_IMAGE.
#
# Flow: preflight (blocked by missing grant, then by a table without a PK,
# then passing) -> schema -> start_sync -> copy -> reset_target -> start_sync
# again -> copy -> build_indexes (detached job) -> watchdog restart test ->
# writes during replication -> cutover -> verify -> cleanup_source -> verify.
set -euo pipefail
cd "$(dirname "$0")"

export SOURCE_PG_MAJOR="${SOURCE_PG_MAJOR:-16}"
export TARGET_PG_MAJOR="${TARGET_PG_MAJOR:-17}"
export MIGRATOR_USER="${MIGRATOR_USER:-root}"
COMPOSE=(docker compose -f docker-compose.yml)
START=$(date +%s)

step() { printf '\n\033[1m=== [%3ss] %s\033[0m\n' "$(( $(date +%s) - START ))" "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; "${COMPOSE[@]}" logs --tail 50 migrator >&2 || true; exit 1; }
pass() { printf '  \033[32mok\033[0m %s\n' "$*"; }

# Commands in the migration container, as MIGRATOR_USER (like `render sandboxes exec`).
mig() { "${COMPOSE[@]}" exec -T migrator "$@"; }
# psql as the app role on the source, and as the migration role on the target.
src() { "${COMPOSE[@]}" exec -T -e PGPASSWORD='app:pass' source psql -X -q -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U app -d app "$@"; }
dst() { "${COMPOSE[@]}" exec -T -e PGPASSWORD=migrator-pass target psql -X -q -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U migrator -d app "$@"; }

cleanup() {
  rc=$?
  [ -n "${WRITER_PID:-}" ] && kill "$WRITER_PID" 2>/dev/null || true
  if [ "${KEEP:-0}" != 1 ]; then "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
  [ $rc -eq 0 ] && printf '\n\033[32mE2E PASSED in %ss\033[0m\n' "$(( $(date +%s) - START ))"
  exit $rc
}
trap cleanup EXIT

wait_phase() {
  # wait_phase <phase> <timeout_s>
  local want="$1" timeout="$2" waited=0 phase st bad=0
  while :; do
    st="$(mig /opt/migrate/status.sh --json)"
    phase="$(jq -r .phase <<<"$st")"
    [ "$phase" = "$want" ] && { pass "phase=$phase after ${waited}s"; return 0; }
    # Fail fast when Bucardo keeps failing instead of waiting out the timeout.
    if jq -e '.errors | any(startswith("last Bucardo run failed"))' >/dev/null <<<"$st"; then bad=$((bad + 1)); else bad=0; fi
    [ "$bad" -lt 10 ] || { jq -r '.errors[]' <<<"$st"; fail "Bucardo runs keep failing"; }
    [ "$waited" -ge "$timeout" ] && { mig /opt/migrate/status.sh || true; fail "phase still '$phase' after ${timeout}s (wanted $want)"; }
    sleep 3; waited=$((waited + 3))
  done
}

# A writer that keeps changing the source: inserts, updates (firing the app
# trigger), deletes (cascading) and sequence use.
WRITE_SQL="
INSERT INTO public.customers (email, external_ref, name)
  SELECT 'w' || n || '@example.com', 'cus_w' || n, 'Writer ' || n
  FROM (SELECT nextval('public.customers_id_seq') AS n) s;
INSERT INTO public.orders (customer_id, customer_ref, status)
  SELECT id, external_ref, 'new' FROM public.customers ORDER BY id DESC LIMIT 1;
INSERT INTO public.order_items (order_id, line_no, sku, qty)
  SELECT max(id), 1, 'SKU-7', 2 FROM public.orders;
UPDATE public.orders SET status = 'paid' WHERE id = (SELECT min(id) FROM public.orders WHERE status = 'new');
DELETE FROM public.orders WHERE id = (SELECT min(o.id) FROM public.orders o WHERE o.status = 'cancelled'
  AND NOT EXISTS (SELECT 1 FROM billing.invoices i WHERE i.order_id = o.id));
INSERT INTO public.events (kind, payload) VALUES ('purchase', '{\"w\": true}');
INSERT INTO billing.invoices (order_id, amount_cents) SELECT max(id), 4242 FROM public.orders;
UPDATE public.products SET price_cents = price_cents + 1 WHERE sku = 'SKU-' || (1 + floor(random() * 300))::int;
"
start_writer() {
  ( while :; do src -c "$WRITE_SQL" >/dev/null 2>&1 || true; sleep 0.2; done ) &
  WRITER_PID=$!
}
stop_writer() { kill "$WRITER_PID" 2>/dev/null || true; wait "$WRITER_PID" 2>/dev/null || true; WRITER_PID=; }

# ----------------------------------------------------------------- start
step "build image and start harness (source PG $SOURCE_PG_MAJOR, target PG $TARGET_PG_MAJOR, user $MIGRATOR_USER)"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
# Built with plain BuildKit, which takes the CA bundle as a secret directly.
# Compose builds through bake, which won't read a file outside the project
# unless granted --allow=fs.read, and `compose up --build` can't grant it.
docker build --progress=plain -f ../image/Dockerfile -t pg-migrate:test \
  --secret "id=ca,src=${CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE:-}" --build-arg "GIT_SHA=${GIT_SHA:-test}" .. 2>&1 | tail -n 15
"${COMPOSE[@]}" up -d --wait --quiet-pull 2>&1 | tail -n 5
mig id
mig bash -c 'cat /opt/migrate/image.env'

# ------------------------------------------------------------- preflight
step "preflight blocks a target without the session_replication_role grant"
set +e
out="$(mig env RENDER_DATABASE_URL=postgresql://migrator:migrator-pass@target-nogrant:5432/app /opt/migrate/preflight.sh 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] || fail "preflight passed against target-nogrant"
grep -q 'BLOCKER: the Render role cannot SET session_replication_role' <<<"$out" || { echo "$out"; fail "missing replica-role blocker"; }
pass "blocked: $(grep -m1 'session_replication_role' <<<"$out" | cut -c1-100)..."

step "preflight blocks a table without a primary key"
set +e
out="$(mig /opt/migrate/preflight.sh 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] || fail "preflight passed with audit_log lacking a primary key"
grep -q 'BLOCKER: tables without a primary key.*public.audit_log' <<<"$out" || { echo "$out"; fail "missing no-PK blocker"; }
pass "blocked on public.audit_log"
grep -q 'migrator-pass\|app:pass\|app%3Apass' <<<"$out" && fail "preflight output leaked a password"
pass "no secrets in output"

step "fix the source (add a primary key) and preflight passes"
src -c "ALTER TABLE public.audit_log ADD COLUMN id bigserial PRIMARY KEY"
out="$(mig /opt/migrate/preflight.sh 2>&1)" || { echo "$out"; fail "preflight should pass"; }
for w in "password" "heroku_ext" "materialized views"; do
  grep -q "WARN:.*$w" <<<"$out" || { echo "$out"; fail "expected a warning mentioning '$w'"; }
done
pass "passed with expected warnings (colon in password, heroku_ext, materialized views)"
# pg_dump of the target's major, or the source's when the target is older.
want_dump=$(( TARGET_PG_MAJOR > SOURCE_PG_MAJOR ? TARGET_PG_MAJOR : SOURCE_PG_MAJOR ))
grep -q "INFO:    schema.sh will dump with pg_dump $want_dump\$" <<<"$out" || { echo "$out"; fail "expected pg_dump $want_dump"; }
pass "picked pg_dump $want_dump"
mig cat /var/lib/migrate/artifacts/preflight.json | jq -e '.status == "pass"' >/dev/null || fail "preflight.json status"

# ---------------------------------------------------------------- schema
step "schema.sh loads the schema without deferred indexes"
mig /opt/migrate/schema.sh 2>&1 | tee "${TMPDIR:-/tmp}/pgm-schema.txt"
grep -q "with pg_dump $want_dump\$" "${TMPDIR:-/tmp}/pgm-schema.txt" || fail "schema.sh did not dump with pg_dump $want_dump"
n_def="$(mig cat /var/lib/migrate/artifacts/schema/indexes.sql | grep -c '^CREATE')"
[ "$n_def" -ge 10 ] || fail "expected >= 10 deferred indexes, got $n_def"
grep -qx 'public.customers_external_ref_idx' <<<"$(mig cat /var/lib/migrate/artifacts/schema/indexes_kept.txt)" \
  || fail "FK-referenced unique index not kept"
[ "$(dst -c "SELECT count(*) FROM pg_indexes WHERE indexname = 'customers_external_ref_idx'")" = 1 ] || fail "kept index missing on target"
[ "$(dst -c "SELECT count(*) FROM pg_indexes WHERE indexname = 'orders_customer_id_idx'")" = 0 ] || fail "deferred index exists on target"
[ "$(dst -c "SELECT count(*) FROM pg_extension WHERE extname IN ('pgcrypto','citext','pg_trgm')")" = 3 ] || fail "extensions missing"
[ "$(dst -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'heroku_ext'")" = 1 ] || fail "heroku_ext schema missing"
pass "$n_def indexes deferred; FK index kept; extensions present"
mig /opt/migrate/schema.sh >/dev/null 2>&1 || fail "schema.sh is not idempotent"
pass "rerun is a no-op"

# ---------------------------------------------------------- first sync run
step "start_sync.sh with writes running; wait for the copy"
start_writer
mig /opt/migrate/start_sync.sh
wait_phase copied 300
[ "$(src -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'")" = 1 ] || fail "bucardo schema missing on source"

step "reset_target.sh returns Render to the post-schema state"
stop_writer
set +e; mig /opt/migrate/reset_target.sh >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "reset_target ran without --confirm"
mig /opt/migrate/reset_target.sh --confirm
[ "$(dst -c "SELECT count(*) FROM public.orders")" = 0 ] || fail "target not truncated"
[ "$(mig /opt/migrate/status.sh --json | jq -r .phase)" = schema-loaded ] || fail "phase after reset"
mig /opt/migrate/status.sh --json | jq -e '.processes.bucardo == false and .processes.watchdog == false' >/dev/null || fail "Bucardo or watchdog still running after reset"
pass "target empty, Bucardo stopped, local config cleared"

# --------------------------------------------------------- second sync run
step "start_sync.sh again (rerun after reset), with writes running"
start_writer
mig /opt/migrate/start_sync.sh
mig /opt/migrate/start_sync.sh >/dev/null 2>&1 || fail "start_sync.sh is not idempotent"
wait_phase copied 300
mig /opt/migrate/status.sh >"${TMPDIR:-/tmp}/pgm-status.txt"; head -n 14 "${TMPDIR:-/tmp}/pgm-status.txt"

# ----------------------------------------------------------- build indexes
step "build_indexes.sh as a detached job"
mig /opt/migrate/job.sh start build_indexes.sh --jobs 2
mig /opt/migrate/job.sh wait build_indexes.sh 600 || { mig /opt/migrate/job.sh tail build_indexes.sh 80; fail "build_indexes failed"; }
mig /opt/migrate/job.sh tail build_indexes.sh 3
[ "$(dst -c "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT i.indisvalid AND n.nspname IN ('public','billing')")" = 0 ] \
  || fail "invalid indexes on target"
[ "$(dst -c "SELECT obj_description('billing.invoices_order_id_idx'::regclass)")" = "lookup by order" ] || fail "index comment not applied"
wait_phase replicating 60

# ---------------------------------------------------------------- watchdog
step "watchdog restarts a killed Bucardo"
mig bash -c 'kill "$(head -n1 /var/run/bucardo/bucardo.mcp.pid)"'
sleep 2
for i in $(seq 1 30); do
  if mig /opt/migrate/status.sh --json | jq -e '.processes.bucardo' >/dev/null 2>&1; then
    break
  fi
  sleep 1
  [ "$i" -lt 30 ] || fail "watchdog did not restart Bucardo"
done
mig grep -q 'RESTART Bucardo' /var/lib/migrate/state/watchdog.restarts || fail "restart not logged"
pass "Bucardo restarted and logged"

# ------------------------------------------- writes during replication
step "deterministic writes during replication"
src -c "UPDATE public.customers SET name = name || ' (edited)' WHERE id % 97 = 0"
src -c "DELETE FROM public.orders WHERE id IN (SELECT o.id FROM public.orders o WHERE o.status = 'cancelled' AND NOT EXISTS (SELECT 1 FROM billing.invoices i WHERE i.order_id = o.id) LIMIT 25)"
src -c "SELECT nextval('billing.invoice_number_seq') FROM generate_series(1, 50)" >/dev/null
src -c "INSERT INTO public.products (sku, title, price_cents) VALUES ('SKU-NEW', 'Late product', 999)"
sleep 5
stop_writer
pass "writer stopped (this is the 'writes to Heroku have stopped' moment)"

# ---------------------------------------------------------------- cutover
step "cutover.sh"
set +e; mig /opt/migrate/cutover.sh >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "cutover ran without --confirm"
mig /opt/migrate/cutover.sh --confirm
wait_phase cutover-done 10

step "verify Render matches Heroku"
# Heroku's view was last refreshed at seed time; cutover refreshed Render's
# with current data. Refresh the source too so the two can be compared.
src -c "REFRESH MATERIALIZED VIEW public.customer_order_totals"
tables="$(src -c "SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) FROM pg_tables WHERE schemaname IN ('public','billing') ORDER BY 1")"
for t in $tables public.customer_order_totals; do
  q="SELECT count(*) || ':' || coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM $t x"
  a="$(src -c "$q")"; b="$(dst -c "$q")"
  [ "$a" = "$b" ] || fail "$t differs: heroku=$a render=$b"
  pass "$t ${a%%:*} rows, identical"
done
seq_q="SELECT string_agg(schemaname||'.'||sequencename||'='||coalesce(last_value::text,'-'), ',' ORDER BY schemaname, sequencename) FROM pg_sequences WHERE schemaname IN ('public','billing')"
[ "$(src -c "$seq_q")" = "$(dst -c "$seq_q")" ] || fail "sequences differ: $(src -c "$seq_q") vs $(dst -c "$seq_q")"
pass "sequences match: $(dst -c "$seq_q")"
idx_q="SELECT string_agg(schemaname||'.'||indexname, ',' ORDER BY schemaname, indexname) FROM pg_indexes WHERE schemaname IN ('public','billing')"
[ "$(src -c "$idx_q")" = "$(dst -c "$idx_q")" ] || fail "index sets differ"
pass "index sets match ($(dst -c "SELECT count(*) FROM pg_indexes WHERE schemaname IN ('public','billing')") indexes, none invalid)"
mig /opt/migrate/status.sh --json | jq -e '.processes.bucardo == false and .processes.watchdog == false' >/dev/null || fail "Bucardo or watchdog still running after cutover"
pass "Bucardo stopped"

step "Render works as the new primary"
dst -c "INSERT INTO public.customers (email, external_ref, name) VALUES ('new@render.test', 'cus_render', 'On Render')" \
  || fail "insert using the customers sequence failed (sequence not advanced?)"
dst -c "INSERT INTO public.events (kind) VALUES ('after-cutover')" || fail "identity insert failed"
before="$(dst -c "SELECT count(*) FROM public.audit_log")"
dst -c "UPDATE public.orders SET status = 'refunded' WHERE id = (SELECT min(id) FROM public.orders)"
[ "$(dst -c "SELECT count(*) FROM public.audit_log")" = $((before + 1)) ] || fail "app trigger did not fire on Render after cutover"
pass "sequences advanced, identity works, app triggers fire"

# --------------------------------------------------------- cleanup source
step "cleanup_source.sh"
set +e; mig /opt/migrate/cleanup_source.sh --verify-only >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "verify-only should fail before cleanup"
mig /opt/migrate/cleanup_source.sh --confirm
[ "$(src -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'")" = 0 ] || fail "bucardo schema remains on source"
[ "$(src -c "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'bucardo%'")" = 0 ] || fail "Bucardo triggers remain on source"
[ "$(src -c "SELECT count(*) FROM pg_trigger WHERE tgname = 'orders_status_audit'")" = 1 ] || fail "app trigger was dropped"
mig /opt/migrate/cleanup_source.sh --verify-only >/dev/null || fail "verify-only should pass after cleanup"
pass "no Bucardo schema or triggers remain; app trigger intact"

step "nothing the operator downloads contains a secret"
if mig grep -rIl -e 'migrator-pass' -e 'app:pass' -e 'app%3Apass' /var/lib/migrate; then
  fail "a file under /var/lib/migrate (fetched by fetch_artifacts.sh) contains a password"
fi
pass "no passwords under /var/lib/migrate"
