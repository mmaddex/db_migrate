#!/usr/bin/env bash
# Put Render back to the state right after schema.sh, for a rerun.
#
#   reset_target.sh --confirm                truncate tables, drop built indexes
#   reset_target.sh --confirm --drop-schema  drop and recreate $SCHEMAS entirely
#
# Also stops Bucardo and the watchdog, drops the bucardo schema on Render,
# and clears the local Bucardo configuration (start_sync.sh reinstalls it).
# Bucardo's triggers on Heroku are left in place; run cleanup_source.sh if
# you are not rerunning soon.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs
require_confirm "$@"
acquire_lock
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL
write_service_file

DROP_SCHEMA=0
has_flag --drop-schema "$@" && DROP_SCHEMA=1

if has_state cutover.done; then
  die "cutover completed at $(state_time cutover.done); Render may now hold production writes. Refusing to reset."
fi

stop_watchdog
stop_bucardo_intentionally

if [ $DROP_SCHEMA = 1 ]; then
  sql=""
  IFS=',' read -r -a schema_list <<<"$SCHEMAS"
  for s in "${schema_list[@]}"; do
    s="$(printf '%s' "$s" | tr -d '[:space:]')"
    [ -n "$s" ] || continue
    sql="$sql DROP SCHEMA IF EXISTS $(ident "$s") CASCADE; CREATE SCHEMA $(ident "$s");"
  done
  log "dropping and recreating schemas on Render: $SCHEMAS"
  psql_dst -1 -c "$sql" >/dev/null 2> >(redact >&2) || die "dropping schemas failed"
  clear_state schema.done
else
  tables="$(list_tables psql_dst | paste -sd, -)"
  if [ -n "$tables" ]; then
    log "truncating $(printf '%s' "$tables" | tr ',' '\n' | grep -c .) table(s) on Render"
    psql_dst -c "TRUNCATE $tables RESTART IDENTITY" >/dev/null 2> >(redact >&2) || die "TRUNCATE failed"
  fi
  # Drop the indexes build_indexes.sh created so the next copy runs index-free.
  idx_file="$ARTIFACT_DIR/schema/indexes.sql"
  if [ -f "$idx_file" ]; then
    n=0
    while IFS= read -r q; do
      [ -n "$q" ] || continue
      psql_dst -c "DROP INDEX IF EXISTS $q" </dev/null >/dev/null 2> >(redact >&2) || die "could not drop index $q"
      n=$((n + 1))
    done <<EOF
$(perl -ne 'print "$2.$1\n" if /^CREATE (?:UNIQUE )?INDEX ((?:"(?:[^"]|"")*"|[^\s."(]+)) ON (?:ONLY )?((?:"(?:[^"]|"")*"|[^\s."(]+))\./' "$idx_file")
EOF
    log "dropped $n deferred index(es) on Render"
  fi
fi

if [ "$(psql_dst -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'")" != 0 ]; then
  log "dropping bucardo schema on Render"
  psql_dst -c "DROP SCHEMA bucardo CASCADE" >/dev/null 2> >(redact >&2) || die "could not drop bucardo schema on Render"
fi

# Local Bucardo configuration: drop the control database; start_local.sh
# reinstalls it on the next start_sync.sh.
if local_pg_running && bucardo_installed; then
  log "dropping local Bucardo control database"
  CTL_DB=postgres psql_ctl -c "DROP DATABASE bucardo" >/dev/null || die "could not drop local bucardo database"
fi
rm -f "$BUCARDO_PIDDIR"/*.pid "$BUCARDO_PIDDIR"/fullstopbucardo 2>/dev/null || true

clear_state sync.started indexes.done indexes.running cutover.started bucardo.stopped_intentionally watchdog.stop
say "reset_target: Render is back to the post-schema state$([ $DROP_SCHEMA = 1 ] && echo ' (schemas dropped; run schema.sh again)')."
say "Bucardo triggers remain on Heroku; run cleanup_source.sh --confirm if you are not rerunning soon."
