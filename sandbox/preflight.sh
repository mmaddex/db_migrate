#!/usr/bin/env bash
# Read-only go/no-go checks against both databases.
#
# Exits 1 if any BLOCKER is found, 0 otherwise. Writes the report to
# $ARTIFACT_DIR/preflight.txt and preflight.json.
#
# The only statements run on the target are inside transactions that are
# rolled back (the session_replication_role and CREATE EXTENSION trials).
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs

BLOCKERS=()
WARNINGS=()
INFOS=()
blocker() { BLOCKERS+=("$*"); }
warning() { WARNINGS+=("$*"); }
info()    { INFOS+=("$*"); }

finish() {
  local report="$ARTIFACT_DIR/preflight.txt" json="$ARTIFACT_DIR/preflight.json" status=pass
  [ ${#BLOCKERS[@]} -eq 0 ] || status=fail
  {
    say "pg-migrate preflight  $(ts)  image=$IMAGE_REVISION  sync=$SYNC_NAME  schemas=$SCHEMAS"
    say ""
    local x
    for x in ${BLOCKERS[@]+"${BLOCKERS[@]}"}; do say "BLOCKER: $x"; done
    for x in ${WARNINGS[@]+"${WARNINGS[@]}"}; do say "WARN:    $x"; done
    for x in ${INFOS[@]+"${INFOS[@]}"};     do say "INFO:    $x"; done
    say ""
    say "RESULT: $status (${#BLOCKERS[@]} blocker(s), ${#WARNINGS[@]} warning(s))"
  } | redact | tee "$report"
  jq -n --arg status "$status" --arg at "$(ts)" --arg image "$IMAGE_REVISION" \
    --args '{status: $status, generated_at: $at, image_revision: $image,
             blockers: $ARGS.positional[0] | fromjson,
             warnings: $ARGS.positional[1] | fromjson,
             info: $ARGS.positional[2] | fromjson}' \
    "$(printf '%s\n' ${BLOCKERS[@]+"${BLOCKERS[@]}"} | jq -R . | jq -s -c 'map(select(length>0))')" \
    "$(printf '%s\n' ${WARNINGS[@]+"${WARNINGS[@]}"} | jq -R . | jq -s -c 'map(select(length>0))')" \
    "$(printf '%s\n' ${INFOS[@]+"${INFOS[@]}"} | jq -R . | jq -s -c 'map(select(length>0))')" \
    | redact >"$json"
  [ "$status" = pass ]
}
# shellcheck disable=SC2154  # rc is assigned inside the trap
trap 'rc=$?; if [ $rc -ne 0 ] && [ -z "${FINISHED:-}" ]; then blocker "preflight aborted unexpectedly (exit $rc)"; FINISHED=1; finish || true; fi' EXIT

# ------------------------------------------------------------ configuration
for var in HEROKU_DATABASE_URL RENDER_DATABASE_URL; do
  if [ -z "${!var:-}" ]; then
    blocker "$var is not set (pass it in migration.env with --env-file)"
  elif ! url_is_valid "${!var}"; then
    blocker "$var is not a valid postgres:// URL (host and database name are required)"
  elif url_password_has_colon "${!var}"; then
    warning "$var has a ':' in its password; Getsafe saw tooling break on this. Rotate the credential if anything misbehaves."
  fi
done
if [ ${#BLOCKERS[@]} -gt 0 ]; then FINISHED=1; finish; exit 1; fi
write_service_file
case "$SYNC_NAME" in
  *[!A-Za-z0-9_]*|'') blocker "SYNC_NAME must contain only letters, digits and underscores" ;;
esac

# ------------------------------------------------------------ connectivity
src_ok=1 dst_ok=1
if ! err="$(psql_src -c 'SELECT 1' 2>&1 >/dev/null)"; then
  blocker "cannot connect to Heroku (sslmode=require): $(printf '%s' "$err" | head -n1)"; src_ok=0
fi
if ! err="$(psql_dst -c 'SELECT 1' 2>&1 >/dev/null)"; then
  blocker "cannot connect to Render (sslmode=require): $(printf '%s' "$err" | head -n1)"; dst_ok=0
fi
if [ $src_ok = 0 ] || [ $dst_ok = 0 ]; then FINISHED=1; finish; exit 1; fi

for side in src dst; do
  enc="$("psql_$side" -c "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")"
  [ "$enc" = t ] || blocker "connection to $([ $side = src ] && echo Heroku || echo Render) is not using TLS"
done

# ---------------------------------------------------------------- versions
src_ver="$(psql_src -c 'SHOW server_version_num')"
dst_ver="$(psql_dst -c 'SHOW server_version_num')"
src_major=$((src_ver / 10000)); dst_major=$((dst_ver / 10000))
info "Heroku Postgres $(psql_src -c 'SHOW server_version'), Render Postgres $(psql_dst -c 'SHOW server_version'); client tools in this image: $PG_CLIENT_MAJORS"
# shellcheck disable=SC2086  # PG_CLIENT_MAJORS is a space-separated list
if ! client_major="$(pick_client_major "$src_major" "$dst_major" $PG_CLIENT_MAJORS)"; then
  blocker "no client tools in this image can dump Heroku's Postgres $src_major (it has $PG_CLIENT_MAJORS); rebuild the image with $src_major in PG_CLIENT_MAJORS"
elif ! client_pg_dump "$client_major" >/dev/null; then
  blocker "pg_dump $client_major is not installed in this image (PG_CLIENT_MAJORS says it is); rebuild the image"
else
  info "schema.sh will dump with pg_dump $client_major"
  if [ "$dst_major" -lt "$src_major" ]; then
    warning "Render ($dst_major) is older than Heroku ($src_major); the schema may use features the target lacks"
  elif [ "$client_major" -gt "$dst_major" ]; then
    warning "this image has no client tools for Render's Postgres $dst_major; the dump from pg_dump $client_major may use syntax Render rejects"
  fi
  # shellcheck disable=SC2086
  oldest="$(printf '%s\n' $PG_CLIENT_MAJORS | sort -n | head -n1)"
  [ "$src_major" -ge "$oldest" ] \
    || warning "Heroku's Postgres $src_major is older than any client tools in this image ($PG_CLIENT_MAJORS); pg_dump $client_major can read it, but this combination is untested"
fi

# -------------------------------------------- trigger-disable permission
if ! err="$(psql_dst -c 'BEGIN; SET LOCAL session_replication_role = replica; ROLLBACK;' 2>&1 >/dev/null)"; then
  blocker "the Render role cannot SET session_replication_role = replica ($(printf '%s' "$err" | head -n1)). An admin must grant superuser or, on PG 15+, run: GRANT SET ON PARAMETER session_replication_role TO $(psql_dst -c 'SELECT quote_ident(current_user)')"
else
  info "Render role can set session_replication_role = replica"
fi

# ------------------------------------------------------------ privileges
[ "$(psql_src -c "SELECT has_database_privilege(current_database(), 'CREATE')")" = t ] \
  || blocker "the Heroku role lacks CREATE on its database; Bucardo needs it to create the bucardo schema"
no_trigger="$(psql_src -c "
  SELECT string_agg(quote_ident(n.nspname)||'.'||quote_ident(c.relname), ', ' ORDER BY n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
    AND NOT has_table_privilege(c.oid, 'TRIGGER')")"
[ -z "$no_trigger" ] || blocker "the Heroku role cannot create triggers on: $no_trigger"
[ "$(psql_dst -c "SELECT has_database_privilege(current_database(), 'CREATE')")" = t ] \
  || blocker "the Render role lacks CREATE on its database (needed for schemas and extensions)"

missing_schemas="$(psql_src -c "
  SELECT string_agg(s, ', ') FROM unnest($(schemas_sql)) s
  WHERE NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s)")"
[ -z "$missing_schemas" ] || blocker "schemas not found on Heroku: $missing_schemas"

# ----------------------------------------------------------- primary keys
# Bucardo needs a key on every table: a primary key, or a unique index it can
# use instead. keys.sh finds the tables with neither and writes the SQL that
# adds one (artifacts/keys/), which the runbook's Replication keys step shows.
rm -f "$ARTIFACT_DIR/keys/plan.json"
if keys_out="$("$MIGRATE_HOME/keys.sh" plan 2>&1)"; then
  while IFS= read -r line; do
    case "$line" in
      'BLOCKER: '*) blocker "${line#BLOCKER: }" ;;
      'WARN: '*)    warning "${line#WARN: }" ;;
      'INFO: '*)    info "${line#INFO: }" ;;
    esac
  done <<<"$keys_out"
else
  blocker "checking which tables have a key Bucardo can use failed: $(printf '%s\n' "$keys_out" | grep -m1 ' ERROR ' | sed 's/.* ERROR //')"
fi

# ------------------------------------------------------------- extensions
while IFS=$'\t' read -r ext ext_schema; do
  [ -n "$ext" ] || continue
  avail="$(psql_dst -c "SELECT 1 FROM pg_available_extensions WHERE name = $(lit "$ext")" </dev/null)"
  if [ "$avail" != 1 ]; then
    blocker "extension $ext is installed on Heroku but not available on Render"
    continue
  fi
  installed="$(psql_dst -c "SELECT 1 FROM pg_extension WHERE extname = $(lit "$ext")" </dev/null)"
  if [ "$installed" != 1 ]; then
    # Trial run inside a transaction that is always rolled back.
    if ! err="$(extension_sql "$ext" "$ext_schema" | { echo 'BEGIN;'; cat; echo 'ROLLBACK;'; } | psql_dst -f - 2>&1 >/dev/null)"; then
      blocker "extension $ext is available on Render but this role cannot create it: $(printf '%s' "$err" | grep -m1 ERROR || printf '%s' "$err" | head -n1)"
    fi
  fi
  if [ "$ext_schema" = heroku_ext ]; then
    warning "extension $ext lives in Heroku's heroku_ext schema; schema.sh will create heroku_ext on Render so references keep working"
  fi
done <<EOF
$(psql_src -c "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname <> 'plpgsql' ORDER BY 1")
EOF

# ---------------------------------------------------- collation / encoding
src_loc="$(psql_src -c "SELECT pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = current_database()")"
dst_loc="$(psql_dst -c "SELECT pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = current_database()")"
IFS=$'\t' read -r s_enc s_coll s_ctype <<<"$src_loc"
IFS=$'\t' read -r d_enc d_coll d_ctype <<<"$dst_loc"
[ "$s_enc" = "$d_enc" ] || warning "encoding differs: Heroku=$s_enc Render=$d_enc"
[ "$s_coll" = "$d_coll" ] || warning "collation differs: Heroku=$s_coll Render=$d_coll. Indexes are built under Render's collation; a person must decide whether the sort-order change is acceptable"
[ "$s_ctype" = "$d_ctype" ] || warning "ctype differs: Heroku=$s_ctype Render=$d_ctype"
info "Heroku encoding/collate/ctype: $s_enc/$s_coll/$s_ctype; Render: $d_enc/$d_coll/$d_ctype"

# ------------------------------------------------- things Bucardo skips
mviews="$(psql_src -c "
  SELECT string_agg(quote_ident(schemaname)||'.'||quote_ident(matviewname), ', ' ORDER BY schemaname, matviewname)
  FROM pg_matviews WHERE schemaname = ANY ($(schemas_sql))")"
[ -z "$mviews" ] || warning "materialized views are not replicated; cutover.sh refreshes them on Render: $mviews"

lobs="$(psql_src -c "SELECT count(*) FROM pg_largeobject_metadata")"
[ "$lobs" = 0 ] || warning "$lobs large object(s) exist on Heroku; large objects are NOT migrated"

gencols="$(psql_src -c "
  SELECT string_agg(DISTINCT quote_ident(n.nspname)||'.'||quote_ident(c.relname), ', ')
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE a.attgenerated <> '' AND NOT a.attisdropped AND c.relkind IN ('r','p')
    AND n.nspname = ANY ($(schemas_sql))" 2>/dev/null || true)"
[ -z "$gencols" ] || info "tables with generated columns; start_sync.sh excludes those columns from Bucardo's COPY (customcols) and Render computes them: $gencols"

unlogged="$(psql_src -c "
  SELECT string_agg(quote_ident(n.nspname)||'.'||quote_ident(c.relname), ', ' ORDER BY n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relpersistence = 'u' AND c.relkind = 'r' AND n.nspname = ANY ($(schemas_sql))")"
[ -z "$unlogged" ] || warning "unlogged tables (replicated, but contents are lost if Render restarts): $unlogged"

# ------------------------------------------------------ Heroku specifics
heroku_refs="$(psql_src -c "
  SELECT count(*) FROM (
    SELECT 1 FROM pg_views WHERE schemaname = ANY ($(schemas_sql)) AND definition ILIKE '%heroku_ext%'
    UNION ALL
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname = ANY ($(schemas_sql)) AND (p.prosrc ILIKE '%heroku_ext%' OR array_to_string(p.proconfig, ',') ILIKE '%heroku_ext%')
    UNION ALL
    SELECT 1 FROM pg_attrdef d JOIN pg_class c ON c.oid = d.adrelid JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = ANY ($(schemas_sql)) AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%heroku_ext%'
    UNION ALL
    SELECT 1 FROM pg_db_role_setting s WHERE array_to_string(s.setconfig, ',') ILIKE '%heroku_ext%'
  ) x")"
[ "$heroku_refs" = 0 ] || warning "$heroku_refs object(s) or settings reference the heroku_ext schema; check search_path and qualified names after cutover"

if [ "$(psql_src -c "SELECT 1 FROM pg_namespace WHERE nspname = 'bucardo'")" = 1 ]; then
  warning "a bucardo schema already exists on Heroku (left over from an earlier run?). start_sync.sh reuses it; cleanup_source.sh removes it"
fi

# --------------------------------------------------------- target state
dst_tables="$(psql_dst -c "
  SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))")"
if [ "$dst_tables" != 0 ]; then
  if has_state schema.done; then
    info "Render already has $dst_tables table(s) in $SCHEMAS (schema loaded at $(state_time schema.done))"
  else
    warning "Render already has $dst_tables table(s) in $SCHEMAS; schema.sh expects an empty target schema"
  fi
fi

# ------------------------------------------------------------- sizing info
info "Heroku database size: $(psql_src -c "SELECT pg_size_pretty(pg_database_size(current_database()))")"
while IFS=$'\t' read -r t sz rows; do
  [ -n "$t" ] && info "table $t: $sz, ~$rows rows"
done <<EOF
$(psql_src -c "
  SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname),
         pg_size_pretty(pg_total_relation_size(c.oid)),
         greatest(c.reltuples, 0)::bigint
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
  ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 25")
EOF
ntables="$(list_tables psql_src | grep -c . || true)"
info "$ntables table(s) in $SCHEMAS will be replicated"

FINISHED=1
finish
