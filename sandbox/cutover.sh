#!/usr/bin/env bash
# Final cutover. Run only after writes to Heroku have stopped (maintenance
# mode on, every writer scaled to zero).
#
#   cutover.sh --confirm
#
# Steps:
#   1. Wait for a Bucardo sync run that started after writes stopped
#      (`bucardo kick <sync> 0`, twice; see below).
#   2. Require exact row counts to match on every table.
#   3. Set every sequence on Render from Heroku's last_value.
#   4. Refresh materialized views, then ANALYZE.
#   5. If the steps logged in as the Render superuser, give everything it
#      created to the role in RENDER_DATABASE_URL, the one the app logs in as.
#   6. Stop Bucardo (and the watchdog).
#
# Getsafe saw ~15 minutes of "record not found" errors after switching before
# replication had caught up; steps 1-2 exist to prevent that.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs
require_confirm "$@"
acquire_lock
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL
write_service_file

REPORT="$ARTIFACT_DIR/cutover-$(date -u +%Y%m%dT%H%M%SZ).txt"
report() { say "$*" | tee -a "$REPORT"; }

has_state cutover.done && { log "cutover already completed at $(state_time cutover.done)"; exit 0; }
has_state indexes.done || die "build_indexes.sh has not completed; run it before cutover"
bucardo_running || die "Bucardo is not running; run start_sync.sh to restart it, then retry"

report "pg-migrate cutover $(ts) image=$IMAGE_REVISION sync=$SYNC_NAME"
report "Operator confirmed that writes to Heroku have stopped."

# Other sessions of the app role on Heroku that are doing something. Heroku
# only lets us see our own role's activity, so this is a hint, not proof.
others="$(psql_src -c "
  SELECT count(*) FROM pg_stat_activity
  WHERE usename = current_user AND pid <> pg_backend_pid()
    AND state NOT IN ('idle') AND coalesce(application_name,'') NOT LIKE 'bucardo%'
    AND coalesce(application_name,'') <> $(lit "$PGAPPNAME")")"
if [ "$others" != 0 ]; then
  warn "$others other active session(s) of the app role on Heroku. Make sure writers are stopped."
  report "WARNING: $others other active Heroku session(s) at cutover start"
fi

mark_state cutover.started

# ------------------------------------------------- 1. wait for replication
# `bucardo kick <sync> 0` blocks until the next "syncdone" notification. If
# a run was already in progress when we kicked, that run's completion
# satisfies the wait even though it may have started before writes stopped.
# The second kick therefore waits for a run that began after the first kick,
# i.e. after writes stopped.
for n in 1 2; do
  log "kick $n/2: waiting for sync $SYNC_NAME to complete (timeout ${CUTOVER_KICK_TIMEOUT}s, 0 = none)"
  rc=0
  (cd "$LOG_DIR" && bucardo kick "$SYNC_NAME" "$CUTOVER_KICK_TIMEOUT" --notimer 9>&-) 2>&1 | redact | tee -a "$REPORT" || rc=$?
  case $rc in
    0) ;;
    1) clear_state cutover.started; die "sync did not finish within ${CUTOVER_KICK_TIMEOUT}s; Bucardo still running, retry cutover" ;;
    *) clear_state cutover.started; die "bucardo kick failed (exit $rc); see $LOG_DIR/log.bucardo" ;;
  esac
done

# ------------------------------------------------- 2. exact row counts
log "comparing exact row counts on every table"
mismatch=0
while IFS= read -r t; do
  [ -n "$t" ] || continue
  s="$(psql_src -c "SELECT count(*) FROM $t" </dev/null)"
  d="$(psql_dst -c "SELECT count(*) FROM $t" </dev/null)"
  if [ "$s" = "$d" ]; then
    report "  ok        $t  $s"
  else
    report "  MISMATCH  $t  heroku=$s render=$d"
    mismatch=$((mismatch + 1))
  fi
done <<EOF
$(list_tables psql_src)
EOF
if [ "$mismatch" -gt 0 ]; then
  clear_state cutover.started
  die "$mismatch table(s) differ; Bucardo left running. Investigate (status.sh --exact), then rerun cutover.sh --confirm. Do NOT point the app at Render."
fi

# ------------------------------------------------------------ 3. sequences
log "setting sequences on Render from Heroku"
seq_sql="$(psql_src -c "
  SELECT format('SELECT setval(%L, %s, %s);',
                quote_ident(schemaname)||'.'||quote_ident(sequencename),
                coalesce(last_value, start_value), (last_value IS NOT NULL)::text)
  FROM pg_sequences WHERE schemaname = ANY ($(schemas_sql)) ORDER BY 1")"
if [ -n "$seq_sql" ]; then
  printf 'BEGIN;\n%s\nCOMMIT;\n' "$seq_sql" | psql_dst -f - >/dev/null 2> >(redact >&2) \
    || die "setting sequences failed"
fi
seq_check_sql="
  SELECT quote_ident(schemaname)||'.'||quote_ident(sequencename), coalesce(last_value::text, 'unset')
  FROM pg_sequences WHERE schemaname = ANY ($(schemas_sql)) ORDER BY 1"
if ! diff <(psql_src -c "$seq_check_sql") <(psql_dst -c "$seq_check_sql") >"$ARTIFACT_DIR/sequence-diff.txt"; then
  report "sequence values differ after setval:"; tee -a "$REPORT" <"$ARTIFACT_DIR/sequence-diff.txt"
  die "sequence values on Render do not match Heroku"
fi
report "sequences: $(printf '%s\n' "$seq_sql" | grep -c setval || true) set and verified"

# ------------------------------------------ 4. materialized views + ANALYZE
mvs="$(psql_dst -c "
  SELECT quote_ident(schemaname)||'.'||quote_ident(matviewname)
  FROM pg_matviews WHERE schemaname = ANY ($(schemas_sql)) ORDER BY 1")"
if [ -n "$mvs" ]; then
  # Views can depend on each other; retry failures until a pass makes no progress.
  pending="$mvs"
  while [ -n "$pending" ]; do
    next=""
    while IFS= read -r mv; do
      [ -n "$mv" ] || continue
      if psql_dst -c "REFRESH MATERIALIZED VIEW $mv" </dev/null >/dev/null 2>"$WORK_DIR/.mv.err"; then
        report "  refreshed $mv"
      else
        next="${next:+$next
}$mv"
      fi
    done <<EOF
$pending
EOF
    if [ "$next" = "$pending" ]; then
      redact <"$WORK_DIR/.mv.err" >&2
      die "could not refresh materialized view(s): $(printf '%s' "$next" | tr '\n' ' ')"
    fi
    pending="$next"
  done
  rm -f "$WORK_DIR/.mv.err"
fi

log "running ANALYZE on Render"
psql_dst -c "SET statement_timeout = 0" -c "ANALYZE" >/dev/null 2> >(redact | grep -v 'skipping' >&2 || true) \
  || die "ANALYZE failed"
report "ANALYZE complete"

# ------------------------------------------- 5. ownership to the app role
# When the steps logged in as the Render superuser (preflight switched to it
# because the app role can't set session_replication_role), it owns every
# schema, table, sequence, view and extension they created. Hand them to the
# role the app logs in as. Before stopping Bucardo, so a failure here can be
# fixed and cutover rerun.
if render_superuser_in_use; then
  app_role="$(render_app_role)"
  su_role="$(psql_dst -c 'SELECT current_user')"
  log "giving everything $su_role owns on Render to $app_role"
  # Each table's owner changes under a brief exclusive lock; don't wait forever for one.
  psql_dst -c "SET lock_timeout = '60s'" -c "REASSIGN OWNED BY CURRENT_USER TO $(ident "$app_role")" >/dev/null 2> >(redact >&2) \
    || die "could not give $su_role's objects to $app_role; Bucardo left running. Fix the error, then rerun cutover.sh --confirm"
  left="$(psql_dst -c "
    SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
      AND n.nspname = ANY ($(schemas_sql))")"
  [ "$left" = 0 ] || die "$left relation(s) in $SCHEMAS still belong to $su_role after REASSIGN OWNED"
  report "ownership: everything $su_role created on Render now belongs to $app_role"
fi

# ---------------------------------------------------------------- 6. stop
stop_watchdog
stop_bucardo_intentionally
mark_state cutover.done
clear_state cutover.started
report "cutover complete $(ts). Point the app at Render now, then run cleanup_source.sh --confirm."
notify "cutover complete for sync $SYNC_NAME"
