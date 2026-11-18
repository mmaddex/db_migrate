#!/usr/bin/env bash
# Build the indexes deferred by schema.sh, on Render, while Bucardo keeps
# replicating. Runs only after the initial copy has finished.
#
#   build_indexes.sh [--jobs N]
#
# Each CREATE [UNIQUE] INDEX from indexes.sql runs as
# CREATE [UNIQUE] INDEX CONCURRENTLY IF NOT EXISTS, one statement per psql
# call (CONCURRENTLY cannot run in a transaction). Invalid indexes left by a
# failed or interrupted build are dropped and rebuilt. Finishes by checking
# that Render has as many indexes in $SCHEMAS as Heroku.
#
# Long-running: the web app runs it detached through job.sh and tails its
# log, so it survives a dropped connection or a redeploy of the app.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs
acquire_lock
require_env HEROKU_DATABASE_URL RENDER_DATABASE_URL
write_service_file

JOBS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done

SCHEMA_OUT="$ARTIFACT_DIR/schema"
[ -f "$SCHEMA_OUT/indexes.sql" ] || die "no $SCHEMA_OUT/indexes.sql; run schema.sh first"
if ! reason="$(copy_done)"; then
  die "the initial copy has not finished ($reason); wait for status.sh to report phase 'copied'"
fi
if has_state indexes.done; then
  log "indexes already built at $(state_time indexes.done); re-checking only"
fi
mark_state indexes.running

# One CONCURRENTLY statement per line, derived from indexes.sql.
concurrent_sql() {
  perl -ne '
    next unless /^CREATE (UNIQUE )?INDEX /;
    # Partitioned parents (ON ONLY) cannot be built concurrently.
    if (/ ON ONLY /) { s/^CREATE (UNIQUE )?INDEX /CREATE $1INDEX IF NOT EXISTS /; }
    else             { s/^CREATE (UNIQUE )?INDEX /CREATE $1INDEX CONCURRENTLY IF NOT EXISTS /; }
    print;
  ' "$SCHEMA_OUT/indexes.sql"
}

run_one() {
  # $1 = statement. Session settings make large builds bearable.
  local stmt="$1" start rc=0 errf
  errf="$(mktemp "$TMPDIR_IDX/err.XXXXXX")"
  start=$(date +%s)
  printf "SET maintenance_work_mem = '%s';\nSET statement_timeout = 0;\nSET lock_timeout = 0;\n%s\n" \
    "$INDEX_MAINTENANCE_WORK_MEM" "$stmt" | psql_dst -f - >/dev/null 2>"$errf" || rc=$?
  if [ $rc -ne 0 ]; then
    warn "failed after $(( $(date +%s) - start ))s: $stmt"
    redact <"$errf" >&2 || true
  else
    log "built in $(( $(date +%s) - start ))s: $(printf '%s' "$stmt" | cut -c1-160)"
  fi
  rm -f "$errf"
  return $rc
}

invalid_indexes() {
  psql_dst -c "
    SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname)
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT i.indisvalid AND n.nspname = ANY ($(schemas_sql))
    ORDER BY 1"
}

drop_invalid() {
  local idx
  while IFS= read -r idx; do
    [ -n "$idx" ] || continue
    warn "dropping invalid index $idx"
    psql_dst -c "DROP INDEX CONCURRENTLY IF EXISTS $idx" </dev/null 2>&1 | redact >&2 || warn "could not drop $idx"
  done <<EOF
$(invalid_indexes)
EOF
}

# Fresh statistics for the copied tables: the planner uses them for the
# builds and afterwards, and status.sh's estimates become accurate.
log "running ANALYZE on the copied tables"
psql_dst -c "SET statement_timeout = 0" -c "ANALYZE $(list_tables psql_dst | paste -sd, -)" >/dev/null 2> >(redact >&2) \
  || warn "ANALYZE before the index build failed; continuing"

TMPDIR_IDX="$(mktemp -d)"
trap 'clear_state indexes.running; rm -rf "$TMPDIR_IDX"' EXIT
concurrent_sql >"$TMPDIR_IDX/statements.sql"
total="$(grep -c . "$TMPDIR_IDX/statements.sql" || true)"
log "building $total index(es) with $JOBS parallel job(s)"

for attempt in 1 2 3; do
  drop_invalid
  failed=0
  if [ "$JOBS" -le 1 ]; then
    while IFS= read -r stmt; do
      [ -n "$stmt" ] || continue
      run_one "$stmt" </dev/null || failed=$((failed + 1))
    done <"$TMPDIR_IDX/statements.sql"
  else
    # Parallel builds across tables. Builds on the same table queue on the
    # table lock, which is safe.
    export INDEX_MAINTENANCE_WORK_MEM TMPDIR_IDX PGSERVICEFILE DST_SERVICE
    export -f run_one _psql psql_dst log warn _log_line ts redact
    tr '\n' '\0' <"$TMPDIR_IDX/statements.sql" |
      xargs -0 -r -P "$JOBS" -I{} bash -c 'run_one "$1"' _ {} || failed=1
  fi
  if [ -z "$(invalid_indexes)" ] && [ "$failed" -eq 0 ]; then
    break
  fi
  warn "attempt $attempt: $failed failure(s), invalid: $(invalid_indexes | tr '\n' ' ')"
done

# Statements that depend on the new indexes (ATTACH PARTITION, COMMENT ON
# INDEX, CLUSTER ON, REPLICA IDENTITY). All are no-ops when already applied.
if [ -s "$SCHEMA_OUT/index_post.sql" ]; then
  log "running dependent statements from index_post.sql"
  psql_dst -f "$SCHEMA_OUT/index_post.sql" >/dev/null 2> >(redact >&2) \
    || die "a statement in index_post.sql failed"
fi

# ---------------------------------------------------------------- verify
remaining_invalid="$(invalid_indexes)"
[ -z "$remaining_invalid" ] || die "invalid indexes remain after 3 attempts: $(printf '%s' "$remaining_invalid" | tr '\n' ' ')"

index_names_sql="
  SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname)
  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace
  WHERE n.nspname = ANY ($(schemas_sql)) ORDER BY 1"
psql_src -c "$index_names_sql" >"$TMPDIR_IDX/src_idx"
psql_dst -c "$index_names_sql" >"$TMPDIR_IDX/dst_idx"
n_src="$(grep -c . "$TMPDIR_IDX/src_idx" || true)"
n_dst="$(grep -c . "$TMPDIR_IDX/dst_idx" || true)"
missing="$(comm -23 "$TMPDIR_IDX/src_idx" "$TMPDIR_IDX/dst_idx" | tr '\n' ' ')"
extra="$(comm -13 "$TMPDIR_IDX/src_idx" "$TMPDIR_IDX/dst_idx" | tr '\n' ' ')"
cp "$TMPDIR_IDX/src_idx" "$ARTIFACT_DIR/indexes_source.txt"
cp "$TMPDIR_IDX/dst_idx" "$ARTIFACT_DIR/indexes_target.txt"
[ -z "$extra" ] || warn "indexes on Render that Heroku does not have: $extra"
if [ "$n_src" != "$n_dst" ] || [ -n "$missing" ]; then
  die "index count mismatch: Heroku=$n_src Render=$n_dst; missing on Render: ${missing:-none}"
fi

mark_state indexes.done
log "all $n_dst indexes present and valid on Render"
say "indexes: $n_dst/$n_src present and valid"
