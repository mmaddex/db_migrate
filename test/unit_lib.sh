#!/usr/bin/env bash
# Unit tests for the shell helpers that parse secrets and config. Runs under
# bash, outside the image.
#
#   test/unit_lib.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fails=0
check() { # check NAME EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then printf '  ok   %s\n' "$1"; else printf '  FAIL %s\n       want: %s\n       got:  %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}

echo "bash $BASH_VERSION"
(
  export WORK_DIR="$T/work" SECRETS_DIR="$T/secrets"
  export HEROKU_DATABASE_URL='postgres://u%40x:p%3Aw%40x@ec2-1-2-3-4.compute-1.amazonaws.com:5433/dbname?sslmode=verify-full&connect_timeout=5'
  export RENDER_DATABASE_URL='postgresql://r_user:simple@dpg-abc-a.oregon-postgres.render.com/r_db'
  # shellcheck source=sandbox/env.sh
  . "$ROOT/sandbox/env.sh"

  check "user decoded"      'u@x'      "$(url_field "$HEROKU_DATABASE_URL" user)"
  check "password decoded"  'p:w@x'    "$(url_field "$HEROKU_DATABASE_URL" password)"
  check "host"              'ec2-1-2-3-4.compute-1.amazonaws.com' "$(url_field "$HEROKU_DATABASE_URL" host)"
  check "port"              '5433'     "$(url_field "$HEROKU_DATABASE_URL" port)"
  check "dbname"            'dbname'   "$(url_field "$HEROKU_DATABASE_URL" dbname)"
  check "no port"           ''         "$(url_field "$RENDER_DATABASE_URL" port)"
  check "ipv6 host"         '::1'      "$(url_field 'postgres://a:b@[::1]:5432/d' host)"
  url_is_valid 'mysql://x' && v=valid || v=invalid
  check "rejects non-postgres URL" invalid "$v"
  url_password_has_colon "$HEROKU_DATABASE_URL" && v=yes || v=no
  check "colon in password detected" yes "$v"
  url_password_has_colon "$RENDER_DATABASE_URL" && v=yes || v=no
  check "no colon in password" no "$v"

  write_service_file
  svc="$(cat "$PGSERVICEFILE")"
  check "heroku keeps sslmode from URL" 1 "$(printf '%s\n' "$svc" | sed -n '/^\[heroku\]/,/^$/p' | grep -c '^sslmode=verify-full$')"
  check "heroku has no second sslmode"  1 "$(printf '%s\n' "$svc" | sed -n '/^\[heroku\]/,/^$/p' | grep -c '^sslmode=')"
  check "heroku extra query param"      1 "$(printf '%s\n' "$svc" | grep -c '^connect_timeout=5$')"
  check "render defaults to require"    1 "$(printf '%s\n' "$svc" | sed -n '/^\[render\]/,/^$/p' | grep -c '^sslmode=require$')"
  check "render default port"           1 "$(printf '%s\n' "$svc" | sed -n '/^\[render\]/,/^$/p' | grep -c '^port=5432$')"
  check "service file mode" 600 "$(stat -c %a "$PGSERVICEFILE" 2>/dev/null || stat -f %Lp "$PGSERVICEFILE")"
  case "$PGSERVICEFILE" in "$WORK_DIR"*) v=inside ;; *) v=outside ;; esac
  check "service file outside WORK_DIR" outside "$v"

  check "redacts passwords and URLs" 'a **** b **** c' \
    "$(printf 'a %s b %s c\n' 'p:w@x' "$RENDER_DATABASE_URL" | redact)"
  check "redacts percent-encoded password" 'x ****' "$(printf 'x %s\n' 'p%3Aw%40x' | redact)"

  # With a Render superuser URL, the [render] service logs in as it; the app
  # role is still the user in RENDER_DATABASE_URL.
  check "app role"                    r_user "$(render_app_role)"
  check "same database, explicit port" \
    "$(url_database "$RENDER_DATABASE_URL")" "$(url_database 'postgresql://su:x@DPG-ABC-A.oregon-postgres.render.com:5432/r_db')"
  export RENDER_SUPERUSER_URL='postgresql://migration_super_user:sup3r@dpg-abc-a.oregon-postgres.render.com/r_db'
  write_service_file
  render="$(sed -n '/^\[render\]/,/^$/p' "$PGSERVICEFILE")"
  check "render service uses superuser" 1 "$(printf '%s\n' "$render" | grep -c '^user=migration_super_user$')"
  check "app role unchanged"            r_user "$(render_app_role)"
  check "redacts superuser URL"         'a **** b ****' "$(printf 'a %s b %s\n' "$RENDER_SUPERUSER_URL" sup3r | redact)"
  unset RENDER_SUPERUSER_URL
  write_service_file

  SCHEMAS=" public , o'brien "
  check "schemas_sql quoting" "ARRAY['public','o''brien']::text[]" "$(schemas_sql)"
  check "lit"   "'it''s'" "$(lit "it's")"
  check "ident" '"a""b"'  "$(ident 'a"b')"

  # pick_client_major SRC DST MAJOR...: the pg_dump to use.
  majors=(14 15 16 17 18)
  check "client: target's major"          17 "$(pick_client_major 14 17 "${majors[@]}")"
  check "client: same major"              14 "$(pick_client_major 14 14 "${majors[@]}")"
  check "client: target newer than all"   18 "$(pick_client_major 16 19 "${majors[@]}")"
  check "client: target older (source's)" 17 "$(pick_client_major 17 15 "${majors[@]}")"
  check "client: old source, in-range target" 15 "$(pick_client_major 12 15 "${majors[@]}")"
  check "client: target older than all"   14 "$(pick_client_major 13 13 "${majors[@]}")"
  check "client: list order ignored"      16 "$(pick_client_major 15 16 18 16 14 17 15)"
  pick_client_major 19 19 "${majors[@]}" >/dev/null && v=picked || v=none
  check "client: none can read source"    none "$v"
  check "client: no pg_dump outside image" none "$(client_pg_dump 99 || echo none)"

  printf 'A=1\nexport B="two words"\n# comment\nC='"'"'x=$(id)'"'"'\nbad-key=1\nD=last-line-no-newline' >"$T/env"
  load_env_file "$T/env"
  check "env file: plain"           1               "$A"
  check "env file: export + quotes" 'two words'     "$B"
  check "env file: no evaluation"   'x=$(id)'       "$C"
  check "env file: last line"       'last-line-no-newline' "$D"

  sleep 30 & zpid=$!
  pid_alive "$zpid" && v=alive || v=dead
  check "pid_alive live process" alive "$v"
  kill "$zpid"; wait "$zpid" 2>/dev/null || true
  pid_alive "$zpid" && v=alive || v=dead
  check "pid_alive dead process" dead "$v"

  exit $fails
) || fails=$?

if [ "$fails" -ne 0 ]; then echo "$fails failure(s)"; exit 1; fi
echo "all unit tests passed"
