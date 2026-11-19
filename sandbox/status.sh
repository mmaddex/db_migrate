#!/usr/bin/env bash
# Report migration status. Read-only (except for log rotation).
#
#   status.sh            human-readable report
#   status.sh --json     machine-readable report (used by the web UI)
#   status.sh --exact    exact row counts (count(*)) instead of estimates
#
# The phase comes from the databases, not from Bucardo's own output, which
# Getsafe found unreliable:
#   not-started | schema-loaded | copying | copied | indexing | replicating
#   | cutting-over | cutover-done
set -uo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs

JSON=0 EXACT=0
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    --exact) EXACT=1 ;;
    *) die "unknown option $a" ;;
  esac
done

ERRORS=()
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "${HEROKU_DATABASE_URL:-}" ] && [ -n "${RENDER_DATABASE_URL:-}" ]; then
  write_service_file
else
  ERRORS+=("HEROKU_DATABASE_URL / RENDER_DATABASE_URL not set in this sandbox")
fi

src_up=0 dst_up=0
psql_src -c 'SELECT 1' >/dev/null 2>&1 && src_up=1 || ERRORS+=("cannot connect to Heroku")
psql_dst -c 'SELECT 1' >/dev/null 2>&1 && dst_up=1 || ERRORS+=("cannot connect to Render")

# ----------------------------------------------------------- log rotation
BUCARDO_LOG="$LOG_DIR/log.bucardo"
if [ -f "$BUCARDO_LOG" ]; then
  size_mb=$(( $(stat -c %s "$BUCARDO_LOG" 2>/dev/null || echo 0) / 1048576 ))
  if [ "$size_mb" -ge "$LOG_ROTATE_MB" ]; then
    # Bucardo appends to an open handle, so copy then truncate in place.
    gzip -c "$BUCARDO_LOG" >"$LOG_DIR/log.bucardo.$(date -u +%Y%m%dT%H%M%SZ).gz" && : >"$BUCARDO_LOG"
    ls -1t "$LOG_DIR"/log.bucardo.*.gz 2>/dev/null | tail -n +6 | xargs -r rm -f
  fi
fi

# ------------------------------------------------------------- processes
pg_local=false; local_pg_running && pg_local=true
bucardo=false; bucardo_running && bucardo=true
watchdog=false; watchdog_running && watchdog=true
hb_age=null
if [ -f "$STATE_DIR/watchdog.heartbeat" ]; then
  hb_age=$(( $(date -u +%s) - $(cat "$STATE_DIR/watchdog.heartbeat") ))
fi

# ------------------------------------------------------------ control db
otc=null sync_status=null last_run="null"
row="$(ctl_sync_row)"
if [ -n "$row" ]; then
  otc="${row%%	*}"; sync_status="$(printf '%s' "${row#*	}" | jq -R .)"
  last_run="$(psql_ctl -c "
    SELECT row_to_json(r) FROM (
      SELECT started, ended, status, inserts, deletes, truncates, lastgood, lastbad
      FROM bucardo.syncrun WHERE sync = $(lit "$SYNC_NAME")
      ORDER BY started DESC LIMIT 1) r" 2>/dev/null)"
  [ -n "$last_run" ] || last_run=null
fi

# ---------------------------------------------------------------- target
copy_progress='[]' index_progress='[]' invalid_indexes='[]'
if [ $dst_up = 1 ]; then
  copy_progress="$(psql_dst -c "
    SELECT coalesce(json_agg(json_build_object(
      'table', p.relid::regclass::text, 'tuples', p.tuples_processed,
      'bytes', p.bytes_processed)), '[]')
    FROM pg_stat_progress_copy p" 2>/dev/null || echo '[]')"
  index_progress="$(psql_dst -c "
    SELECT coalesce(json_agg(json_build_object(
      'table', p.relid::regclass::text, 'index', p.index_relid::regclass::text,
      'phase', p.phase, 'blocks_done', p.blocks_done, 'blocks_total', p.blocks_total,
      'tuples_done', p.tuples_done, 'tuples_total', p.tuples_total)), '[]')
    FROM pg_stat_progress_create_index p" 2>/dev/null || echo '[]')"
  invalid_indexes="$(psql_dst -c "
    SELECT coalesce(json_agg(quote_ident(n.nspname)||'.'||quote_ident(c.relname)), '[]')
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT i.indisvalid AND n.nspname = ANY ($(schemas_sql))" 2>/dev/null || echo '[]')"
fi

# ---------------------------------------------------------------- tables
# Estimates: reltuples once the table has been analyzed (or indexed), else
# n_live_tup. n_live_tup alone can overshoot right after Bucardo's initial
# copy (its TRUNCATE-then-DELETE fallback inside a savepoint skews the
# counters until the next ANALYZE), so it is only the fallback.
count_sql="
  SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname),
         (CASE WHEN c.reltuples >= 0 THEN c.reltuples ELSE coalesce(s.n_live_tup, 0) END)::bigint
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
  ORDER BY 1"
exact_counts() {
  local side="$1" t
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    printf '%s\t%s\n' "$t" "$("$side" -c "SELECT count(*) FROM $t" </dev/null 2>/dev/null || echo -1)"
  done <<EOF
$(list_tables "$side" 2>/dev/null)
EOF
}
: >"$TMP/src.tsv"; : >"$TMP/dst.tsv"
if [ $EXACT = 1 ]; then
  [ $src_up = 1 ] && exact_counts psql_src >"$TMP/src.tsv"
  [ $dst_up = 1 ] && exact_counts psql_dst >"$TMP/dst.tsv"
else
  [ $src_up = 1 ] && psql_src -c "$count_sql" >"$TMP/src.tsv" 2>/dev/null
  [ $dst_up = 1 ] && psql_dst -c "$count_sql" >"$TMP/dst.tsv" 2>/dev/null
fi

# ----------------------------------------------------------------- phase
running_job() { [ -f "$JOB_DIR/$1.pid" ] && pid_alive "$(cat "$JOB_DIR/$1.pid")"; }
phase="not-started" detail=""
if has_state cutover.done; then
  phase="cutover-done"; detail="cutover finished at $(state_time cutover.done)"
elif has_state cutover.started; then
  phase="cutting-over"; detail="cutover started at $(state_time cutover.started)"
elif ! has_state sync.started; then
  if has_state schema.done; then phase="schema-loaded"; detail="run start_sync.sh"; else detail="run preflight.sh, then schema.sh"; fi
elif [ $src_up = 0 ] || [ $dst_up = 0 ]; then
  phase="unknown"; detail="a database is unreachable"
elif ! reason="$(copy_done)"; then
  phase="copying"; detail="$reason"
elif has_state indexes.done; then
  phase="replicating"; detail="indexes built at $(state_time indexes.done); ready for cutover"
elif running_job build_indexes || has_state indexes.running || [ "$index_progress" != '[]' ]; then
  phase="indexing"; detail="build_indexes.sh in progress"
else
  phase="copied"; detail="initial copy finished; run build_indexes.sh"
fi

healthy=true
# A failed Bucardo run (e.g. a KID dying on a table) is retried forever and
# looks like a stalled copy; surface it.
if [ "$last_run" != null ] && jq -e '.lastbad == true or ((.status // "") | startswith("Failed"))' >/dev/null 2>&1 <<<"$last_run"; then
  case "$phase" in copying|copied|indexing|replicating)
    healthy=false
    ERRORS+=("last Bucardo run failed: $(jq -r '.status // ""' <<<"$last_run" | redact | cut -c1-300)")
  ;; esac
fi
case "$phase" in
  copying|copied|indexing|replicating)
    [ "$bucardo" = true ] || { healthy=false; ERRORS+=("Bucardo is not running"); }
    [ "$pg_local" = true ] || { healthy=false; ERRORS+=("local control Postgres is not running"); }
    [ "$watchdog" = true ] || { healthy=false; ERRORS+=("watchdog is not running; rerun start_sync.sh"); }
    ;;
esac

# ------------------------------------------------------------------- jobs
jobs_json='[]'
for f in "$JOB_DIR"/*.cmd; do
  [ -e "$f" ] || continue
  name="$(basename "$f" .cmd)"
  st="finished"; rc=null
  running_job "$name" && st="running"
  [ -f "$JOB_DIR/$name.rc" ] && rc="$(cat "$JOB_DIR/$name.rc")"
  jobs_json="$(jq -c --arg n "$name" --arg s "$st" --argjson rc "${rc:-null}" \
    --arg started "$(cat "$JOB_DIR/$name.started" 2>/dev/null)" \
    '. + [{name: $n, state: $s, exit_code: $rc, started_at: $started}]' <<<"$jobs_json")"
done

markers='{}'
for m in keys.added schema.done sync.started indexes.running indexes.done cutover.started cutover.done cleanup_source.done keys.dropped bucardo.stopped_intentionally; do
  if has_state "$m"; then
    markers="$(jq -c --arg k "$m" --arg v "$(state_time "$m")" '. + {($k): $v}' <<<"$markers")"
  fi
done

log_tail=""
[ -f "$BUCARDO_LOG" ] && log_tail="$(tail -n 40 "$BUCARDO_LOG" | redact)"
restarts=""
[ -f "$STATE_DIR/watchdog.restarts" ] && restarts="$(tail -n 10 "$STATE_DIR/watchdog.restarts")"

REPORT="$(jq -n \
  --arg at "$(ts)" --arg image "$IMAGE_REVISION" --arg sync "$SYNC_NAME" --arg schemas "$SCHEMAS" \
  --arg phase "$phase" --arg detail "$detail" --argjson healthy "$healthy" \
  --argjson pg_local "$pg_local" --argjson bucardo "$bucardo" --argjson watchdog "$watchdog" \
  --argjson hb_age "$hb_age" --argjson otc "$otc" --argjson sync_status "$sync_status" \
  --argjson last_run "$last_run" --argjson copy "$copy_progress" --argjson idx "$index_progress" \
  --argjson invalid "$invalid_indexes" --argjson jobs "$jobs_json" --argjson markers "$markers" \
  --argjson exact "$([ $EXACT = 1 ] && echo true || echo false)" \
  --rawfile src "$TMP/src.tsv" --rawfile dst "$TMP/dst.tsv" \
  --arg log_tail "$log_tail" --arg restarts "$restarts" \
  --args '
  def rows($s): $s | split("\n") | map(select(length > 0) | split("\t") | {key: .[0], value: (.[1] | tonumber? // null)}) | from_entries;
  (rows($src)) as $S | (rows($dst)) as $D |
  ([$S, $D] | map(keys) | add | unique) as $names |
  {
    generated_at: $at, image_revision: $image, sync_name: $sync, schemas: $schemas,
    phase: $phase, phase_detail: $detail, healthy: $healthy,
    processes: {local_postgres: $pg_local, bucardo: $bucardo, watchdog: $watchdog, watchdog_heartbeat_age_s: $hb_age},
    bucardo_sync: {onetimecopy: $otc, status: $sync_status, last_run: $last_run},
    copy_progress: $copy, index_progress: $idx, invalid_indexes: $invalid,
    markers: $markers, jobs: $jobs, exact_counts: $exact,
    tables: [$names[] | {table: ., source_rows: $S[.], target_rows: $D[.]}],
    totals: {source_rows: ([$S[]] | map(. // 0) | add // 0), target_rows: ([$D[]] | map(. // 0) | add // 0)},
    watchdog_restarts: ($restarts | split("\n") | map(select(length > 0))),
    log_tail: ($log_tail | split("\n")),
    errors: $ARGS.positional
  }' ${ERRORS[@]+"${ERRORS[@]}"})"

printf '%s\n' "$REPORT" >"$ARTIFACT_DIR/status.json"

if [ $JSON = 1 ]; then
  printf '%s\n' "$REPORT"
  exit 0
fi

# ------------------------------------------------------------- text report
jq -r '
  "pg-migrate status  \(.generated_at)  image=\(.image_revision)  sync=\(.sync_name)",
  "",
  "PHASE:    \(.phase)  (\(.phase_detail))",
  "HEALTHY:  \(.healthy)",
  "PROCS:    local_postgres=\(.processes.local_postgres) bucardo=\(.processes.bucardo) watchdog=\(.processes.watchdog) heartbeat_age=\(.processes.watchdog_heartbeat_age_s // "-")s",
  "BUCARDO:  onetimecopy=\(.bucardo_sync.onetimecopy // "-") status=\(.bucardo_sync.status // "-") last_run=\(.bucardo_sync.last_run // {} | "\(.started // "-") -> \(.ended // "running") \(.status // "")")",
  (if (.copy_progress | length) > 0 then "COPYING:  " + (.copy_progress | map("\(.table) \(.tuples) rows") | join(", ")) else empty end),
  (if (.index_progress | length) > 0 then "INDEXING: " + (.index_progress | map("\(.index) \(.phase)") | join(", ")) else empty end),
  (if (.invalid_indexes | length) > 0 then "INVALID INDEXES: " + (.invalid_indexes | join(", ")) else empty end),
  (if (.errors | length) > 0 then "ERRORS:   " + (.errors | join("; ")) else empty end),
  "",
  "TABLE (\(if .exact_counts then "exact" else "estimated" end) rows)                   SOURCE       TARGET",
  (.tables[] | "\(.table | .[0:40] | . + (" " * (40 - length)))  \(.source_rows // "-" | tostring | (" " * (11 - length)) + .)  \(.target_rows // "-" | tostring | (" " * (11 - length)) + .)"),
  "TOTAL                                     \(.totals.source_rows | tostring | (" " * (11 - length)) + .)  \(.totals.target_rows | tostring | (" " * (11 - length)) + .)",
  (if (.watchdog_restarts | length) > 0 then "", "WATCHDOG RESTARTS:", (.watchdog_restarts[]) else empty end)
' <<<"$REPORT"

if [ "$bucardo" = true ]; then
  say ""
  say "--- bucardo status"
  timeout 20 bash -c 'cd "$LOG_DIR" && bucardo status' 2>&1 | redact | head -n 40
fi
if [ -n "$log_tail" ]; then
  say ""
  say "--- tail of $BUCARDO_LOG"
  printf '%s\n' "$log_tail" | tail -n 20
fi
