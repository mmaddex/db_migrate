#!/usr/bin/env bash
# End-to-end test of the /opt/migrate scripts against the local harness.
#
#   test/e2e.sh            build, run, tear down
#   KEEP=1 test/e2e.sh     leave the containers running afterwards
#
# Env: SOURCE_PG_MAJOR (Heroku, default 16), TARGET_PG_MAJOR (Render,
# default 17), MIGRATOR_USER (root|postgres), PG_IMAGE, BASE_IMAGE,
# E2E_SUPERUSER (yes|no, default no). Like the app, the test always gives the
# sandbox a Render superuser password (RENDER_SUPERUSER_PASSWORD and its SCRAM
# verifier). With E2E_SUPERUSER=yes the app role (migrator) gets no grant, as
# on Render: preflight creates migration_super_user with that password, the
# test runs the SQL preflight shows for Render support, every step then logs
# in as it, and cutover must give everything back to migrator. With no, the
# app role can disable triggers and preflight must create nothing.
#
# Flow: preflight (blocked by missing grant, then by tables without a key
# Bucardo can use) -> keys.sh add -> preflight passes -> schema -> start_sync
# -> copy -> reset_target -> start_sync again -> copy -> build_indexes
# (detached job) -> watchdog restart test -> writes during replication ->
# cutover -> verify -> cleanup_source -> verify -> keys.sh drop -> verify.
set -euo pipefail
cd "$(dirname "$0")"

export SOURCE_PG_MAJOR="${SOURCE_PG_MAJOR:-16}"
export TARGET_PG_MAJOR="${TARGET_PG_MAJOR:-17}"
export MIGRATOR_USER="${MIGRATOR_USER:-root}"
export E2E_SUPERUSER="${E2E_SUPERUSER:-no}"
# Test-only: the verifier is scram_verifier("super-pass") from app/pgmigrate/vault.py, with a fixed salt.
export RENDER_SUPERUSER_PASSWORD=super-pass
export RENDER_SUPERUSER_VERIFIER='SCRAM-SHA-256$4096:cGctbWlncmF0ZS1lMmUhIQ==$NXctgxqlgiC+pV052fwoX0mhF6Hb8ZG+10cXggLHFAk=:dZ5gSlkUob4dv205WR+MiAEDqhD/MF2m7VWkqdEXTNg='
if [ "$E2E_SUPERUSER" = yes ]; then
  export TARGET_GRANT_REPLICA_ROLE=no
  STEP_ROLE=migration_super_user
else
  export TARGET_GRANT_REPLICA_ROLE=yes
  STEP_ROLE=migrator
fi
COMPOSE=(docker compose -f docker-compose.yml)
START=$(date +%s)

step() { printf '\n\033[1m=== [%3ss] %s\033[0m\n' "$(( $(date +%s) - START ))" "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; "${COMPOSE[@]}" logs --tail 50 migrator >&2 || true; exit 1; }
pass() { printf '  \033[32mok\033[0m %s\n' "$*"; }

# Commands in the migration container, as MIGRATOR_USER (like `render sandboxes exec`).
mig() { "${COMPOSE[@]}" exec -T migrator "$@"; }
# psql as the app role on the source. On the target: as the role the steps
# log in as (dst), as the app's role (app_dst), and as the admin, standing in
# for Render support (admin_dst). dst and app_dst differ with E2E_SUPERUSER.
src() { "${COMPOSE[@]}" exec -T -e PGPASSWORD='app:pass' source psql -X -q -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U app -d app "$@"; }
dst_as() { local user="$1" pass="$2"; shift 2; "${COMPOSE[@]}" exec -T -e PGPASSWORD="$pass" target psql -X -q -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U "$user" -d app "$@"; }
app_dst() { dst_as migrator migrator-pass "$@"; }
if [ "$E2E_SUPERUSER" = yes ]; then dst() { dst_as migration_super_user super-pass "$@"; }; else dst() { app_dst "$@"; }; fi
admin_dst() { "${COMPOSE[@]}" exec -T target psql -X -q -v ON_ERROR_STOP=1 -At -U postgres -d app "$@"; }
admin_nogrant() { "${COMPOSE[@]}" exec -T target-nogrant psql -X -q -v ON_ERROR_STOP=1 -At -U postgres -d app "$@"; }
super_role_q="SELECT coalesce(string_agg(rolsuper::text || ' ' || rolpassword, ','), 'none') FROM pg_authid WHERE rolname = 'migration_super_user'"

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
INSERT INTO public.customer_tags (customer_id, tag) SELECT max(id), 'writer' FROM public.customers ON CONFLICT DO NOTHING;
UPDATE public.customer_tags SET tag = tag || '+' WHERE (customer_id, tag) = (SELECT customer_id, tag FROM public.customer_tags WHERE tag = 'vip' LIMIT 1);
UPDATE public.settings SET value = value || '.' WHERE name = 'setting_' || (1 + floor(random() * 50))::int;
UPDATE public.audit_log SET message = message || ' (seen)' WHERE mig_id = (SELECT min(mig_id) FROM public.audit_log WHERE message LIKE 'seed%' AND message NOT LIKE '%(seen)');
"
start_writer() {
  ( while :; do src -c "$WRITE_SQL" >/dev/null 2>&1 || true; sleep 0.2; done ) &
  WRITER_PID=$!
}
stop_writer() { kill "$WRITER_PID" 2>/dev/null || true; wait "$WRITER_PID" 2>/dev/null || true; WRITER_PID=; }

# ----------------------------------------------------------------- start
step "build image and start harness (source PG $SOURCE_PG_MAJOR, target PG $TARGET_PG_MAJOR, user $MIGRATOR_USER, superuser $E2E_SUPERUSER)"
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
step "preflight creates migration_super_user on a target without the grant, and asks only for Render support's step"
set +e
out="$(mig env RENDER_DATABASE_URL=postgresql://migrator:migrator-pass@target-nogrant:5432/app /opt/migrate/preflight.sh 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] || fail "preflight passed against target-nogrant"
grep -q 'BLOCKER: the Render role migrator cannot SET session_replication_role = replica .* so the migration runs as migration_super_user, whose password only this app has, but it isn.t a superuser yet' <<<"$out" \
  || { echo "$out"; fail "missing replica-role blocker"; }
grep -qF '`ALTER USER migration_super_user WITH SUPERUSER;`' <<<"$out" || { echo "$out"; fail "replica-role blocker lacks the SQL for Render support"; }
grep -q 'CREATE USER' <<<"$out" && { echo "$out"; fail "preflight still asks people to create the user"; }
grep -q 'INFO: *created the Render role migration_super_user' <<<"$out" || { echo "$out"; fail "preflight didn't say it created migration_super_user"; }
grep -qF -e "$RENDER_SUPERUSER_PASSWORD" -e "$RENDER_SUPERUSER_VERIFIER" <<<"$out" && fail "preflight output shows the superuser password or verifier"
# Created with the verifier, so the password never reached the server.
[ "$(admin_nogrant -c "$super_role_q")" = "false $RENDER_SUPERUSER_VERIFIER" ] || fail "migration_super_user on target-nogrant: $(admin_nogrant -c "$super_role_q")"
pass "blocked, with migration_super_user created and only the support step left: $(grep -m1 'session_replication_role' <<<"$out" | cut -c1-80)..."

if [ "$E2E_SUPERUSER" = yes ]; then
  step "preflight creates migration_super_user; Render support makes it a superuser with the SQL preflight shows"
  set +e; out="$(mig /opt/migrate/preflight.sh 2>&1)"; rc=$?; set -e
  [ $rc -ne 0 ] || fail "preflight passed before migration_super_user was a superuser"
  [ "$(admin_dst -c "$super_role_q")" = "false $RENDER_SUPERUSER_VERIFIER" ] || fail "migration_super_user on target: $(admin_dst -c "$super_role_q")"
  grant_sql="$(grep -o '`ALTER USER [^`]*`' <<<"$out" | tr -d '`')"
  [ "$grant_sql" = "ALTER USER migration_super_user WITH SUPERUSER;" ] || { echo "$out"; fail "unexpected SQL for Render support: $grant_sql"; }
  admin_dst -c "$grant_sql"
  pass "preflight created migration_super_user; ran \"$grant_sql\" as the admin"
fi

step "preflight blocks tables without a key Bucardo can use"
set +e
out="$(mig /opt/migrate/preflight.sh 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] || fail "preflight passed with audit_log lacking a primary key"
blk="$(grep 'BLOCKER: tables without a primary key' <<<"$out")" || { echo "$out"; fail "missing no-PK blocker"; }
grep -q 'public.audit_log, public.settings\.' <<<"$blk" || { echo "$blk"; fail "blocker should name audit_log and settings"; }
grep -q 'customer_tags' <<<"$blk" && fail "customer_tags has a unique index Bucardo can use; it must not block"
grep -q 'INFO:.*public.customer_tags has no primary key; Bucardo will use its unique index public.customer_tags_uq (customer_id, tag)' <<<"$out" \
  || { echo "$out"; fail "expected customer_tags to be keyed by its unique index"; }
grep -q 'WARN:.*public.settings: Bucardo would use its unique index public.settings_name_idx, but some rows have NULL in (name)' <<<"$out" \
  || { echo "$out"; fail "expected the NULL-key warning for settings"; }
pass "blocked on public.audit_log and public.settings; customer_tags keyed by its unique index"
grep -q 'migrator-pass\|super-pass\|app:pass\|app%3Apass' <<<"$out" && fail "preflight output leaked a password"
pass "no secrets in output"
plan="$(mig cat /var/lib/migrate/artifacts/keys/plan.json)"
[ "$(jq -r '[.tables[].table] | join(",")' <<<"$plan")" = "public.audit_log,public.settings" ] || fail "plan.json tables"
sha="$(jq -r .add_sha <<<"$plan")"
[ "$sha" = "$(mig sha256sum /var/lib/migrate/artifacts/keys/add_keys.sql | cut -d' ' -f1)" ] || fail "add_sha does not match add_keys.sql"
pass "plan.json and add_keys.sql written"

step "keys.sh add puts a replication key on each, without locking the tables"
set +e
mig /opt/migrate/keys.sh add >/dev/null 2>&1; rc=$?
mig /opt/migrate/keys.sh add --confirm --expect "$(printf '0%.0s' $(seq 64))" >"${TMPDIR:-/tmp}/pgm-keys.txt" 2>&1; rc2=$?
set -e
[ $rc -ne 0 ] || fail "keys.sh add ran without --confirm"
[ $rc2 -ne 0 ] && grep -q 'differs from the SQL the runbook showed' "${TMPDIR:-/tmp}/pgm-keys.txt" || fail "keys.sh add ran SQL with another hash"
[ "$(src -c "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.audit_log'::regclass AND attname = 'mig_id'")" = 0 ] \
  || fail "a refused keys.sh add changed the table"
mig /opt/migrate/keys.sh add --confirm --expect "$sha" 2>&1 | tee "${TMPDIR:-/tmp}/pgm-keys.txt" | grep -E '^(=== |CREATE UNIQUE INDEX|psql:.*NOTICE)' || true
for want in 'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS audit_log_mig_id_key ON public.audit_log (mig_id);' \
            "NOTICE:  public.audit_log: 500 row(s) filled" "NOTICE:  public.settings: 51 row(s) filled" \
            '=== Heroku: done' 'replication keys added to: public.audit_log, public.settings'; do
  grep -qF "$want" "${TMPDIR:-/tmp}/pgm-keys.txt" || fail "keys.sh add output lacks: $want"
done
for t in audit_log settings; do
  [ "$(src -c "SELECT count(*) - count(mig_id), count(*) - count(DISTINCT mig_id) FROM public.$t")" = "0|0" ] \
    || fail "$t.mig_id is not filled and unique"
  [ "$(src -c "SELECT indisvalid FROM pg_index WHERE indexrelid = 'public.${t}_mig_id_key'::regclass")" = t ] \
    || fail "${t}_mig_id_key is not valid"
done
src -c "INSERT INTO public.audit_log (message) VALUES ('after keys')"
[ "$(src -c "SELECT mig_id IS NOT NULL FROM public.audit_log WHERE message = 'after keys'")" = t ] || fail "new rows get no mig_id"
pass "mig_id filled, unique, indexed; new rows get one from the default"
mig /opt/migrate/keys.sh add --confirm 2>&1 | grep -q 'no table needs a replication key' || fail "keys.sh add is not idempotent"
pass "rerun is a no-op"

step "preflight passes"
out="$(mig /opt/migrate/preflight.sh 2>&1)" || { echo "$out"; fail "preflight should pass"; }
grep -q 'INFO:.*public.audit_log has no primary key; Bucardo will use the replication key pg-migrate added, public.audit_log_mig_id_key (mig_id)' <<<"$out" \
  || { echo "$out"; fail "expected audit_log to be keyed by mig_id"; }
for w in "password" "heroku_ext" "materialized views"; do
  grep -q "WARN:.*$w" <<<"$out" || { echo "$out"; fail "expected a warning mentioning '$w'"; }
done
pass "passed with expected warnings (colon in password, heroku_ext, materialized views)"
# pg_dump of the target's major, or the source's when the target is older.
want_dump=$(( TARGET_PG_MAJOR > SOURCE_PG_MAJOR ? TARGET_PG_MAJOR : SOURCE_PG_MAJOR ))
grep -q "INFO:    schema.sh will dump with pg_dump $want_dump\$" <<<"$out" || { echo "$out"; fail "expected pg_dump $want_dump"; }
pass "picked pg_dump $want_dump"
mig cat /var/lib/migrate/artifacts/preflight.json | jq -e '.status == "pass"' >/dev/null || fail "preflight.json status"
if [ "$E2E_SUPERUSER" = yes ]; then
  grep -q 'INFO:.*steps log in to Render as migration_super_user.*cutover gives everything they create to migrator' <<<"$out" \
    || { echo "$out"; fail "expected the steps to log in as migration_super_user"; }
  pass "the steps log in as migration_super_user"
else
  [ "$(admin_dst -c "$super_role_q")" = none ] || fail "preflight created migration_super_user though migrator can disable triggers"
  pass "migrator can disable triggers; no migration_super_user created"
fi

# ---------------------------------------------------------------- schema
step "schema.sh loads the schema without deferred indexes"
mig /opt/migrate/schema.sh 2>&1 | tee "${TMPDIR:-/tmp}/pgm-schema.txt"
grep -q "with pg_dump $want_dump\$" "${TMPDIR:-/tmp}/pgm-schema.txt" || fail "schema.sh did not dump with pg_dump $want_dump"
n_def="$(mig cat /var/lib/migrate/artifacts/schema/indexes.sql | grep -c '^CREATE')"
[ "$n_def" -ge 10 ] || fail "expected >= 10 deferred indexes, got $n_def"
kept="$(mig cat /var/lib/migrate/artifacts/schema/indexes_kept.txt)"
for i in public.customers_external_ref_idx public.audit_log_mig_id_key public.customer_tags_uq public.settings_mig_id_key; do
  grep -qx "$i" <<<"$kept" || fail "$i not kept in the schema"
  [ "$(dst -c "SELECT count(*) FROM pg_indexes WHERE schemaname || '.' || indexname = '$i'")" = 1 ] || fail "kept index $i missing on target"
done
[ "$(dst -c "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum WHERE d.adrelid = 'public.audit_log'::regclass AND a.attname = 'mig_id'")" = "nextval('audit_log_mig_id_seq'::regclass)" ] \
  || fail "mig_id's default did not reach the target"
set +e; mig /opt/migrate/keys.sh add --confirm >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "keys.sh add ran after the schema was loaded"
[ "$(dst -c "SELECT count(*) FROM pg_indexes WHERE indexname = 'orders_customer_id_idx'")" = 0 ] || fail "deferred index exists on target"
[ "$(dst -c "SELECT count(*) FROM pg_extension WHERE extname IN ('pgcrypto','citext','pg_trgm')")" = 3 ] || fail "extensions missing"
[ "$(dst -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'heroku_ext'")" = 1 ] || fail "heroku_ext schema missing"
pass "$n_def indexes deferred; FK and Bucardo key indexes kept; extensions present"
owners="$(dst -c "SELECT string_agg(DISTINCT tableowner, ',') FROM pg_tables WHERE schemaname IN ('public','billing')")"
[ "$owners" = "$STEP_ROLE" ] || fail "tables belong to $owners after schema.sh, not $STEP_ROLE"
pass "tables belong to $STEP_ROLE, the role the steps log in as"
mig /opt/migrate/schema.sh >/dev/null 2>&1 || fail "schema.sh is not idempotent"
pass "rerun is a no-op"

# ---------------------------------------------------------- first sync run
step "start_sync.sh with writes running; wait for the copy"
start_writer
mig /opt/migrate/start_sync.sh
mig grep -q 'Bucardo already installed' /var/lib/migrate/logs/migrate.log || fail "the image's control database has no Bucardo"
! mig grep -q 'installing Bucardo' /var/lib/migrate/logs/migrate.log || fail "start_sync installed Bucardo, which the image already has"
pass "Bucardo came installed with the image"
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
mig grep -q 'installing Bucardo' /var/lib/migrate/logs/migrate.log || fail "start_sync did not reinstall Bucardo after reset_target dropped it"
pass "Bucardo reinstalled after reset"
mig /opt/migrate/start_sync.sh >/dev/null 2>&1 || fail "start_sync.sh is not idempotent"
wait_phase copied 300
mig /opt/migrate/status.sh >"${TMPDIR:-/tmp}/pgm-status.txt"; head -n 14 "${TMPDIR:-/tmp}/pgm-status.txt"
# The keys Bucardo chose for the tables without a primary key.
goat="$(mig psql -X -At -h /var/run/postgresql -U postgres -d bucardo -c "
  SELECT schemaname || '.' || tablename || '=' || pkey FROM bucardo.goat
  WHERE tablename IN ('audit_log', 'customer_tags', 'settings') ORDER BY 1" | paste -sd' ' -)"
[ "$goat" = "public.audit_log=mig_id public.customer_tags=customer_id|tag public.settings=mig_id" ] \
  || fail "Bucardo's keys for the tables without a primary key: $goat"
pass "Bucardo keys: $goat"

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
mig /opt/migrate/cutover.sh --confirm | tee "${TMPDIR:-/tmp}/pgm-cutover.txt"
wait_phase cutover-done 10
if [ "$E2E_SUPERUSER" = yes ]; then
  grep -q 'ownership: everything migration_super_user created on Render now belongs to migrator' "${TMPDIR:-/tmp}/pgm-cutover.txt" \
    || fail "cutover did not report giving migration_super_user's objects to migrator"
fi

step "everything the migration created on Render belongs to the app role"
q="SELECT string_agg(DISTINCT pg_get_userbyid(c.relowner), ',') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname IN ('public','billing','heroku_ext')"
[ "$(dst -c "$q")" = migrator ] || fail "relations on Render belong to $(dst -c "$q")"
q="SELECT string_agg(DISTINCT pg_get_userbyid(nspowner), ',') FROM pg_namespace WHERE nspname IN ('billing','heroku_ext')"
[ "$(dst -c "$q")" = migrator ] || fail "schemas on Render belong to $(dst -c "$q")"
q="SELECT string_agg(DISTINCT pg_get_userbyid(extowner), ',') FROM pg_extension WHERE extname IN ('pgcrypto','citext','pg_trgm')"
[ "$(dst -c "$q")" = migrator ] || fail "extensions on Render belong to $(dst -c "$q")"
pass "tables, sequences, views, schemas and extensions belong to migrator"

step "verify Render matches Heroku"
# Heroku's view was last refreshed at seed time; cutover refreshed Render's
# with current data. Refresh the source too so the two can be compared.
src -c "REFRESH MATERIALIZED VIEW public.customer_order_totals"
tables="$(src -c "SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) FROM pg_tables WHERE schemaname IN ('public','billing') ORDER BY 1")"
for t in $tables public.customer_order_totals; do
  q="SELECT count(*) || ':' || coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM $t x"
  a="$(src -c "$q")"; b="$(app_dst -c "$q")"
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

step "Render works as the new primary, for the app's role"
app_dst -c "INSERT INTO public.customers (email, external_ref, name) VALUES ('new@render.test', 'cus_render', 'On Render')" \
  || fail "insert using the customers sequence failed (sequence not advanced?)"
app_dst -c "INSERT INTO public.events (kind) VALUES ('after-cutover')" || fail "identity insert failed"
before="$(app_dst -c "SELECT count(*) FROM public.audit_log")"
app_dst -c "UPDATE public.orders SET status = 'refunded' WHERE id = (SELECT min(id) FROM public.orders)"
[ "$(app_dst -c "SELECT count(*) FROM public.audit_log")" = $((before + 1)) ] || fail "app trigger did not fire on Render after cutover"
[ "$(app_dst -c "SELECT count(*) - count(DISTINCT mig_id) FROM public.audit_log")" = 0 ] || fail "audit_log.mig_id collided on Render"
app_dst -c "REFRESH MATERIALIZED VIEW public.customer_order_totals" || fail "the app's role cannot refresh the materialized view"
pass "sequences advanced (mig_id too), identity works, app triggers fire, views refresh"

# --------------------------------------------------------- cleanup source
step "keys.sh drop waits for Clean up Heroku"
set +e; out="$(mig /opt/migrate/keys.sh drop --confirm 2>&1)"; rc=$?; set -e
[ $rc -ne 0 ] && grep -q 'run Clean up Heroku first' <<<"$out" || { echo "$out"; fail "keys.sh drop ran while Bucardo's triggers were on Heroku"; }
[ "$(src -c "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.audit_log'::regclass AND attname = 'mig_id' AND NOT attisdropped")" = 1 ] \
  || fail "a refused keys.sh drop changed the table"
pass "refused while Bucardo's triggers use the columns"

step "cleanup_source.sh"
set +e; mig /opt/migrate/cleanup_source.sh --verify-only >/dev/null 2>&1; rc=$?; set -e
[ $rc -ne 0 ] || fail "verify-only should fail before cleanup"
mig /opt/migrate/cleanup_source.sh --confirm
[ "$(src -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'")" = 0 ] || fail "bucardo schema remains on source"
[ "$(src -c "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'bucardo%'")" = 0 ] || fail "Bucardo triggers remain on source"
[ "$(src -c "SELECT count(*) FROM pg_trigger WHERE tgname = 'orders_status_audit'")" = 1 ] || fail "app trigger was dropped"
mig /opt/migrate/cleanup_source.sh --verify-only >/dev/null || fail "verify-only should pass after cleanup"
pass "no Bucardo schema or triggers remain; app trigger intact"

step "keys.sh drop removes the replication keys from both databases"
drop_sha="$(mig /opt/migrate/keys.sh plan >/dev/null 2>&1; mig cat /var/lib/migrate/artifacts/keys/plan.json | jq -r .drop_sha)"
mig /opt/migrate/keys.sh drop --confirm --expect "$drop_sha" 2>&1 | tee "${TMPDIR:-/tmp}/pgm-drop.txt" | grep -E '^(=== |DROP INDEX CONCURRENTLY|ALTER TABLE)' || true
grep -qF 'replication keys removed from Heroku and Render' "${TMPDIR:-/tmp}/pgm-drop.txt" || fail "keys.sh drop did not finish"
for side in src dst; do
  [ "$($side -c "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN ('public','billing') AND a.attname = 'mig_id' AND NOT a.attisdropped")" = 0 ] \
    || fail "mig_id columns remain ($side)"
  [ "$($side -c "SELECT count(*) FROM pg_class WHERE relname IN ('audit_log_mig_id_seq','audit_log_mig_id_key','settings_mig_id_seq','settings_mig_id_key')")" = 0 ] \
    || fail "mig_id sequences or indexes remain ($side)"
done
src -c "INSERT INTO public.audit_log (message) VALUES ('after drop')" || fail "insert on Heroku failed after the drop"
app_dst -c "UPDATE public.orders SET status = 'paid' WHERE id = (SELECT min(id) FROM public.orders)" || fail "trigger insert on Render failed after the drop"
[ "$(app_dst -c "SELECT count(*) FROM public.customer_tags")" -gt 0 ] || fail "customer_tags lost"
pass "columns, sequences and indexes gone on both sides; writes still work"

step "nothing the operator downloads contains a secret"
if mig grep -rIl -e 'migrator-pass' -e 'super-pass' -e 'app:pass' -e 'app%3Apass' /var/lib/migrate; then
  fail "a file under /var/lib/migrate (fetched by fetch_artifacts.sh) contains a password"
fi
pass "no passwords under /var/lib/migrate"
