#!/usr/bin/env bash
# Unit tests for the shell helpers that parse secrets and config. Runs under
# any bash >= 3.2 (CI also runs it in a bash:3.2 container, as on macOS).
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

  SCHEMAS=" public , o'brien "
  check "schemas_sql quoting" "ARRAY['public','o''brien']::text[]" "$(schemas_sql)"
  check "lit"   "'it''s'" "$(lit "it's")"
  check "ident" '"a""b"'  "$(ident 'a"b')"

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

(
  export MIGRATIONS_DIR="$T/migrations" MIGRATION=unit SANDBOX_PLAN=
  unset SANDBOX_PLAN
  mkdir -p "$MIGRATIONS_DIR/unit"
  printf 'SANDBOX_PLAN=4c-8g\nSANDBOX_NAME="custom"\n' >"$MIGRATIONS_DIR/unit/migration.conf"
  # shellcheck source=local/config.sh
  . "$ROOT/local/config.sh"
  check "migration.conf overrides default" 4c-8g "$SANDBOX_PLAN"
  check "migration.conf quoted value"      custom "$SANDBOX_NAME"
  version_ge 2.37.0 2.36.0 && v=yes || v=no; check "version 2.37.0 >= 2.36.0" yes "$v"
  version_ge 2.35.9 2.36.0 && v=yes || v=no; check "version 2.35.9 < 2.36.0"  no  "$v"
  version_ge 3.0.0 2.36.0 && v=yes || v=no;  check "version 3.0.0 >= 2.36.0"  yes "$v"
  exit $fails
) || fails=$?

(
  export MIGRATIONS_DIR="$T/migrations" MIGRATION=unit SANDBOX_PLAN=1c-2g
  . "$ROOT/local/config.sh"
  check "environment beats migration.conf" 1c-2g "$SANDBOX_PLAN"
  exit $fails
) || fails=$?

if [ "$fails" -ne 0 ]; then echo "$fails failure(s)"; exit 1; fi
echo "all unit tests passed"
