#!/usr/bin/env bash
# Configure Bucardo (one relgroup, one sync with onetimecopy=1), start it,
# and launch the watchdog. Idempotent: rerun it to heal a sandbox where the
# local Postgres, Bucardo or the watchdog has stopped.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs
acquire_lock
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL

has_state schema.done || die "schema.sh has not completed; load the schema first"
if has_state cutover.done; then
  die "cutover already completed at $(state_time cutover.done); refusing to restart replication"
fi
write_service_file

"$MIGRATE_HOME/start_local.sh" 9>&-

q() { lit "$1"; }
ctl_count() { psql_ctl -c "$1" </dev/null; }

# ---------------------------------------------------------------- databases
add_db() {
  local name="$1" service="$2"
  if [ "$(ctl_count "SELECT count(*) FROM bucardo.db WHERE name = $(q "$name")")" = 1 ]; then
    log "Bucardo database $name already configured"
    return 0
  fi
  log "adding Bucardo database $name"
  # Credentials come from the service file; Bucardo only stores the name.
  bucardo_cli add db "$name" "service=$service" conn="sslmode=require" 2>&1 | redact >&2 \
    || die "bucardo add db $name failed"
}
add_db heroku heroku
add_db render render

# ---------------------------------------------------------------- relgroup
n_rel="$(ctl_count "SELECT count(*) FROM bucardo.herdmap WHERE herd = $(q "$SYNC_NAME")")"
if [ "$n_rel" -gt 0 ]; then
  log "relgroup $SYNC_NAME already has $n_rel table(s)"
else
  log "adding all tables in $SCHEMAS to relgroup $SYNC_NAME"
  schema_args=()
  IFS=',' read -r -a schema_list <<<"$SCHEMAS"
  for s in "${schema_list[@]}"; do
    s="$(printf '%s' "$s" | tr -d '[:space:]')"
    [ -n "$s" ] && schema_args+=(-n "$s")
  done
  bucardo_cli add all tables db=heroku relgroup="$SYNC_NAME" "${schema_args[@]}" 2>&1 | redact >&2 \
    || die "bucardo add all tables failed"
  n_rel="$(ctl_count "SELECT count(*) FROM bucardo.herdmap WHERE herd = $(q "$SYNC_NAME")")"
  n_src="$(list_tables psql_src | grep -c . || true)"
  [ "$n_rel" -eq "$n_src" ] || die "relgroup has $n_rel table(s) but Heroku has $n_src in $SCHEMAS"
  log "relgroup $SYNC_NAME has $n_rel table(s)"
fi

# -------------------------------------------------------------------- sync
if [ "$(ctl_count "SELECT count(*) FROM bucardo.sync WHERE name = $(q "$SYNC_NAME")")" = 1 ]; then
  log "sync $SYNC_NAME already configured"
else
  log "adding sync $SYNC_NAME (heroku:source -> render:target, onetimecopy=1)"
  bucardo_cli add sync "$SYNC_NAME" relgroup="$SYNC_NAME" dbs=heroku:source,render:target onetimecopy=1 2>&1 | redact >&2 \
    || die "bucardo add sync failed"
fi

# --------------------------------------------------------- generated columns
# Bucardo 5.6.0 predates generated columns and includes them in its COPY
# column list, which Postgres rejects ("Generated columns cannot be used in
# COPY"). A customcols clause listing only the ordinary columns fixes both
# the source SELECT and the target COPY; Render computes the generated values.
while IFS=$'\t' read -r table clause; do
  [ -n "$table" ] || continue
  have="$(ctl_count "
    SELECT count(*) FROM bucardo.customcols cc JOIN bucardo.goat g ON g.id = cc.goat
    WHERE g.schemaname || '.' || g.tablename = $(q "$table")")"
  if [ "$have" != 0 ]; then
    log "customcols for $table already configured"
    continue
  fi
  log "excluding generated columns of $table via customcols"
  bucardo_cli add customcols "$table" "$clause" sync="$SYNC_NAME" 2>&1 | redact >&2 \
    || die "bucardo add customcols $table failed"
done <<EOF
$(psql_src -c "
  SELECT n.nspname || '.' || c.relname,
         'SELECT ' || string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
    AND EXISTS (SELECT 1 FROM pg_attribute g
                WHERE g.attrelid = c.oid AND g.attgenerated <> '' AND NOT g.attisdropped)
  GROUP BY 1 ORDER BY 1")
EOF

# -------------------------------------------------------------------- start
clear_state bucardo.stopped_intentionally
if bucardo_running; then
  log "Bucardo already running (MCP pid $(bucardo_mcp_pid))"
else
  log "starting Bucardo"
  bucardo_cli start "started by start_sync.sh" 2>&1 | redact >&2 || die "bucardo start failed"
  for _ in $(seq 1 30); do bucardo_running && break; sleep 1; done
  bucardo_running || die "Bucardo did not stay up; see $LOG_DIR/log.bucardo"
fi
has_state sync.started || mark_state sync.started

start_watchdog
log "watchdog running (pid $(watchdog_pid))"
say "sync $SYNC_NAME started; poll status.sh until the phase is 'copied', then run build_indexes.sh"
