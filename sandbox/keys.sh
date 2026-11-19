#!/usr/bin/env bash
# Replication keys for tables Bucardo can't replicate.
#
# Bucardo needs a key on every table: a primary key or, failing that, a
# unique index (see keys.sql for the one it picks). For a table with
# neither, this adds a surrogate key on Heroku without locking the table:
# a bigint column with a sequence default, filled in batches, and a unique
# index built CONCURRENTLY. Run it before schema.sh, so the column, its
# default and its sequence reach Render with the rest of the schema: Bucardo
# refuses a sync whose source and target columns differ in NOT NULL or
# DEFAULT, and copies the values from Heroku. The column carries a comment
# so this script can find it again and remove it after cutover.
#
#   keys.sh plan                           read-only: prints BLOCKER/WARN/INFO
#                                          lines (preflight.sh relays them) and
#                                          writes $ARTIFACT_DIR/keys/plan.json,
#                                          add_keys.sql and drop_keys.sql
#   keys.sh add --confirm [--expect SHA]   runs add_keys.sql on Heroku
#   keys.sh drop --confirm [--expect SHA]  runs drop_keys.sql on Heroku, then
#                                          Render; only once Bucardo's triggers
#                                          are gone (cleanup_source.sh)
#
# add and drop print every statement as it runs, with its result. With
# --expect they run only if the SQL's sha256 is SHA, the hash of the SQL the
# web app showed: if the database changed since, they stop instead of
# running something nobody reviewed. Either way they finish by planning
# again, so plan.json (which the web app reads) describes the databases as
# they are afterwards.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL

cmd="${1:-}"
[ $# -gt 0 ] && shift
EXPECT=""
CONFIRM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --confirm) CONFIRM=1; shift ;;
    --expect) EXPECT="${2:-}"; shift 2 || die "--expect needs a hash" ;;
    *) die "unknown option $1" ;;
  esac
done
case "$EXPECT" in *[!0-9a-f]*) die "--expect takes a sha256 hash" ;; esac

OUT="$ARTIFACT_DIR/keys"
# The comment on every column this script adds; keys.sql finds them by it.
KEY_MARKER='pg-migrate replication key'
write_service_file
mkdir -p "$OUT"

catalog() { # catalog psql_src|psql_dst
  "$1" -v schemas="$(schemas_sql)" -v marker="$KEY_MARKER" -f "$MIGRATE_HOME/keys.sql" </dev/null
}

# Works out the plan from both databases and writes it to $OUT. Prints the
# report lines.
plan() {
  local src dst dst_added='[]' nulls='{}' row t cond r add_sha='' drop_sha=''
  src="$(catalog psql_src 2> >(redact >&2))" || die "could not read Heroku's catalog"
  # Bucardo picks a unique index without checking its columns for NULLs, and a
  # row with a NULL key doesn't replicate its updates and deletes. Check the
  # ones whose columns allow NULL (an index scan for a key this script added).
  while IFS= read -r row; do
    [ -n "$row" ] || continue
    t="$(printf '%s' "$row" | base64 -d | jq -r '.[0]')"
    cond="$(printf '%s' "$row" | base64 -d | jq -r '.[1]')"
    if r="$(psql_src -c "SET statement_timeout = '30s'" -c "SELECT EXISTS (SELECT 1 FROM $t WHERE $cond)" </dev/null 2>&1)"; then
      case "$r" in t|f) ;; *) r=error ;; esac
    elif printf '%s' "$r" | grep -q 'statement timeout'; then
      r=timeout
    else
      warn "checking $t for NULL keys failed: $(printf '%s' "$r" | redact | head -n1)"
      r=error
    fi
    nulls="$(jq -c --arg t "$t" --arg r "$r" '. + {($t): $r}' <<<"$nulls")"
  done <<EOF
$(jq -r '.tables[] | select(.key != null and .key.valid and .key.nullable)
         | [.table, (.key.columns | map(. + " IS NULL") | join(" OR "))] | @base64' <<<"$src")
EOF
  # Keys this script added that are on Render (schema.sh copied them there),
  # so the removal covers both databases.
  if dst="$(catalog psql_dst 2>/dev/null)"; then
    dst_added="$(jq -c '.added' <<<"$dst")"
  else
    warn "could not read Render's catalog; the removal SQL covers the keys found on Heroku"
  fi
  jq -n --argjson src "$src" --argjson dst_added "$dst_added" --argjson nulls "$nulls" \
    -f "$MIGRATE_HOME/keys.jq" >"$OUT/plan.tmp" || die "planning replication keys failed"
  jq -j '.add_sql' "$OUT/plan.tmp" >"$OUT/add_keys.sql"
  jq -j '.drop_sql' "$OUT/plan.tmp" >"$OUT/drop_keys.sql"
  [ -s "$OUT/add_keys.sql" ] && add_sha="$(sha256 "$OUT/add_keys.sql")"
  [ -s "$OUT/drop_keys.sql" ] && drop_sha="$(sha256 "$OUT/drop_keys.sql")"
  jq --arg at "$(ts)" --arg a "$add_sha" --arg d "$drop_sha" \
    '{generated_at: $at} + . + {add_sha: $a, drop_sha: $d}' "$OUT/plan.tmp" >"$OUT/plan.json"
  rm -f "$OUT/plan.tmp"
  jq -r '.report[] | "\(.level): \(.text)"' "$OUT/plan.json"
  if [ -n "$add_sha" ]; then
    say "INFO: SQL that adds the replication keys: artifacts/keys/add_keys.sql (sha256 $add_sha)"
  fi
}

sha256() { sha256sum "$1" | cut -d' ' -f1; }

# After add or drop, succeeded or not: plan again, quietly.
replan_on_exit() {
  trap 'rc=$?; trap - EXIT; plan >/dev/null 2>&1 || true; exit $rc' EXIT
}

# The SQL must be what the web app showed (--expect), when it says.
check_expect() {
  [ -n "$EXPECT" ] || return 0
  local have
  have="$(sha256 "$1")"
  if [ "$have" != "$EXPECT" ]; then
    die "the SQL now differs from the SQL the runbook showed (sha256 $have, expected $EXPECT): the database changed since the last preflight. Run preflight again, review the new SQL, then retry"
  fi
  log "the SQL matches the runbook's (sha256 $have)"
}

# Runs a SQL file with psql, printing each statement before it runs and its
# result (command tag, notices, errors, time) after.
run_sql() { # run_sql <service> <label> <file>
  local rc=0
  say ""
  say "=== $2: psql -f $(basename "$3")"
  PGOPTIONS="-c client_min_messages=notice" psql -X --no-psqlrc -v ON_ERROR_STOP=1 -d "$1" \
    -c '\timing on' -c '\set ECHO all' -f "$3" </dev/null 2>&1 | redact || rc=$?
  if [ $rc = 0 ]; then say "=== $2: done"; else say "=== $2: failed (psql exit $rc)"; fi
  return $rc
}

case "$cmd" in
  plan)
    plan
    ;;

  add)
    [ $CONFIRM = 1 ] || die "this changes Heroku's tables; rerun with --confirm"
    acquire_lock
    replan_on_exit
    if has_state schema.done; then
      die "the schema was already loaded into Render at $(state_time schema.done). A column added on Heroku now wouldn't be on Render, and Bucardo would refuse the sync. Add keys before Load schema"
    fi
    log "checking which tables need a replication key"
    plan >/dev/null
    if [ ! -s "$OUT/add_keys.sql" ]; then
      say "no table needs a replication key that this script can add"
      exit 0
    fi
    check_expect "$OUT/add_keys.sql"
    not_owned="$(jq -r '[.tables[] | select(.manual == null and (.owner_ok | not)) | .table] | join(", ")' "$OUT/plan.json")"
    if [ -n "$not_owned" ]; then
      die "the Heroku role doesn't own $not_owned (or can't create objects in its schema). Have the owner run artifacts/keys/add_keys.sql, then run preflight again"
    fi
    tables="$(jq -r '[.tables[] | select(.manual == null) | .table] | join(", ")' "$OUT/plan.json")"
    log "adding replication keys on Heroku to: $tables"
    run_sql "$SRC_SERVICE" Heroku "$OUT/add_keys.sql" \
      || die "a statement failed (see above); nothing after it ran. The SQL is safe to run again once the cause is fixed: a lock timeout means a long transaction held the table, so retry when it's quieter"
    say ""
    log "checking the result"
    plan >/dev/null
    trap - EXIT
    left="$(jq -r '[.tables[] | select(.manual == null) | .table] | join(", ")' "$OUT/plan.json")"
    [ -z "$left" ] || die "these tables still have no usable key: $left"
    mark_state keys.added
    say "replication keys added to: $tables. Run preflight again."
    ;;

  drop)
    [ $CONFIRM = 1 ] || die "this drops columns on Heroku and Render; rerun with --confirm"
    acquire_lock
    replan_on_exit
    if bucardo_running; then
      die "Bucardo is still running. Remove the keys only after cutover and Clean up Heroku"
    fi
    if ! verify="$("$MIGRATE_HOME/cleanup_source.sh" --verify-only 2>&1)"; then
      printf '%s\n' "$verify" | redact >&2
      die "Bucardo's schema or triggers are still there: run Clean up Heroku first. Its triggers on Heroku use these columns, so with the column gone every write to those tables would fail"
    fi
    log "Bucardo's triggers are gone from both databases"
    plan >/dev/null
    if [ ! -s "$OUT/drop_keys.sql" ]; then
      say "no replication keys added by pg-migrate were found"
      exit 0
    fi
    check_expect "$OUT/drop_keys.sql"
    run_sql "$SRC_SERVICE" Heroku "$OUT/drop_keys.sql" \
      || die "a statement failed on Heroku (see above). The SQL is safe to run again: a lock timeout means a long transaction held the table, so retry when it's quieter"
    run_sql "$DST_SERVICE" Render "$OUT/drop_keys.sql" \
      || die "a statement failed on Render (see above). The SQL is safe to run again: a lock timeout means a long transaction held the table, so retry when it's quieter"
    mark_state keys.dropped
    say "replication keys removed from Heroku and Render"
    ;;

  *)
    die "usage: keys.sh plan | add --confirm [--expect SHA] | drop --confirm [--expect SHA]"
    ;;
esac
