#!/usr/bin/env bash
# Remove everything Bucardo installed on the databases:
#   DROP SCHEMA bucardo CASCADE on Heroku (which also drops Bucardo's
#   triggers, because they depend on functions in that schema), and the same
#   on Render if the schema exists there. Then verify nothing is left.
#
#   cleanup_source.sh --confirm          drop and verify
#   cleanup_source.sh --verify-only      read-only check (used by teardown)
#   cleanup_source.sh --confirm --force  also when replication is still live
#
# Runs in the sandbox, or on the operator machine with plain psql (bash 3.2
# compatible): see local/cleanup_source.sh.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL
write_service_file

VERIFY_ONLY=0 FORCE=0
for a in "$@"; do
  case "$a" in
    --verify-only) VERIFY_ONLY=1 ;;
    --force) FORCE=1 ;;
    --confirm) ;;
    *) die "unknown option $a" ;;
  esac
done

leftovers() {
  # $1 = psql_src | psql_dst. Prints "<schema_exists> <bucardo_triggers>".
  "$1" -c "
    SELECT (SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'),
           (SELECT count(*) FROM pg_trigger t
              JOIN pg_proc p ON p.oid = t.tgfoid
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE NOT t.tgisinternal AND (n.nspname = 'bucardo' OR t.tgname LIKE 'bucardo%'))" </dev/null
}

verify() {
  local ok=0 side label out
  for side in psql_src psql_dst; do
    label=Heroku; [ "$side" = psql_dst ] && label=Render
    if ! out="$(leftovers "$side" 2>/dev/null)"; then
      warn "$label: cannot connect to verify"; ok=1; continue
    fi
    # shellcheck disable=SC2086
    set -- $out
    if [ "$1" = 0 ] && [ "$2" = 0 ]; then
      log "$label: no bucardo schema and no Bucardo triggers"
    else
      warn "$label: bucardo schema present=$1, Bucardo triggers=$2"; ok=1
    fi
  done
  return $ok
}

if [ $VERIFY_ONLY = 1 ]; then
  verify && { say "clean"; exit 0; }
  say "not clean"; exit 1
fi

require_confirm "$@"

if [ -d "$STATE_DIR" ] && bucardo_running && ! has_state cutover.done && [ $FORCE = 0 ]; then
  die "Bucardo is still replicating and cutover has not run. Use --force only if you are abandoning this migration."
fi

drop_schema() {
  local side="$1" label="$2" attempt
  for attempt in 1 2 3 4 5; do
    # Dropping triggers needs a brief exclusive lock on each table; don't
    # queue behind long transactions forever.
    if "$side" -c "SET lock_timeout = '10s'" -c "DROP SCHEMA IF EXISTS bucardo CASCADE" \
         </dev/null >/dev/null 2>"$STATE_DIR/.cleanup.err"; then
      log "$label: dropped schema bucardo (if it existed)"
      rm -f "$STATE_DIR/.cleanup.err"
      return 0
    fi
    warn "$label: attempt $attempt failed: $(redact <"$STATE_DIR/.cleanup.err" | head -n1)"
    sleep $((attempt * 5))
  done
  die "$label: could not drop schema bucardo"
}

drop_schema psql_src Heroku
if [ "$(psql_dst -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'")" != 0 ]; then
  drop_schema psql_dst Render
fi

if verify; then
  mark_state cleanup_source.done
  say "cleanup_source: Heroku and Render are free of Bucardo objects"
else
  die "Bucardo objects remain; see warnings above"
fi
