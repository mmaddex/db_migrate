# shellcheck shell=bash
#
# Helpers shared by the /opt/migrate scripts. Sourced by env.sh; do not
# source directly.
#
# Rules every helper follows:
#   * Never print a connection URL or password. Use `redact` on any output
#     that might echo one (psql errors, Bucardo logs).
#   * Stay portable bash (no associative arrays or mapfile): test/unit_lib.sh
#     runs these helpers outside the image too.

# ---------------------------------------------------------------- logging --

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

_log_line() {
  local level="$1"; shift
  local line
  line="$(ts) [$(basename "$0" .sh)] $level $*"
  printf '%s\n' "$line" >&2
  if [ -d "$LOG_DIR" ] && [ -w "$LOG_DIR" ]; then
    printf '%s\n' "$line" >>"$LOG_DIR/migrate.log" 2>/dev/null || true
  fi
}
log()  { _log_line INFO "$@"; }
warn() { _log_line WARN "$@"; }
die()  { _log_line ERROR "$@"; exit 1; }

# Print a section header to stdout (reports, not logs).
say() { printf '%s\n' "$*"; }

# ------------------------------------------------------------ environment --

# Load KEY=VALUE lines without evaluating them as shell. Supports optional
# `export ` prefixes, blank lines, comments and single or double quotes.
load_env_file() {
  local file="$1" line key value
  [ -r "$file" ] || die "cannot read env file"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    key="${line%%=*}"
    value="${line#*=}"
    [ "$key" != "$line" ] || continue
    case "$key" in *[!A-Za-z0-9_]*|[0-9]*|'') continue ;; esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    export "$key=$value"
  done <"$file"
}

require_env() {
  local name
  for name in "$@"; do
    if [ -z "$(eval "printf '%s' \"\${$name:-}\"")" ]; then
      die "$name is not set (pass it in migration.env with --env-file)"
    fi
  done
}

# ---------------------------------------------------------------- secrets --

# Replace every password and full URL we know about with ****. Reads the
# secrets from the environment, never from argv.
redact() {
  perl -pe '
    BEGIN {
      $| = 1;  # line by line, so progress reaches the job log as it happens
      @s = ();
      for my $k (qw(HEROKU_DATABASE_URL RENDER_DATABASE_URL RENDER_DATABASE_URL_NOGRANT SLACK_WEBHOOK_URL)) {
        my $u = $ENV{$k} // "";
        next unless length $u;
        push @s, $u;
        if ($u =~ m{^[a-z]+://[^:/@]*:([^@]*)@}i) {
          my $p = $1; push @s, $p if length $p;
          (my $d = $p) =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/ge; push @s, $d if length $d;
        }
      }
      @s = sort { length($b) <=> length($a) } @s;
    }
    for my $s (@s) { s/\Q$s\E/****/g }
  '
}

# url_field URL FIELD -> prints one field of a postgres:// URL, decoded.
# FIELD is one of: user password host port dbname query.
url_field() {
  URL_IN="$1" perl -e '
    my ($f) = @ARGV;
    my $u = $ENV{URL_IN};
    $u =~ m{^postgres(?:ql)?://(?:([^:@/]*)(?::([^@]*))?@)?(\[[^\]]+\]|[^:/?]*)(?::(\d+))?(?:/([^?]*))?(?:\?(.*))?$}i
      or exit 2;
    my %p = (user => $1, password => $2, host => $3, port => $4, dbname => $5, query => $6);
    my $v = $p{$f} // "";
    $v =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/ge unless $f eq "query";
    $v =~ s/^\[(.*)\]$/$1/ if $f eq "host";
    print $v;
  ' "$2"
}

url_is_valid() {
  local host
  host="$(url_field "$1" host 2>/dev/null)" || return 1
  [ -n "$host" ] && [ -n "$(url_field "$1" dbname)" ]
}

# Writes the libpq service file with [heroku] and [render] sections built
# from HEROKU_DATABASE_URL and RENDER_DATABASE_URL. sslmode defaults to
# require unless the URL sets it. The file is mode 0600.
write_service_file() {
  local tmp section var url key val pair query old_umask
  old_umask="$(umask)"
  umask 077
  mkdir -p "$(dirname "$PGSERVICEFILE")" || die "cannot create $(dirname "$PGSERVICEFILE")"
  tmp="$PGSERVICEFILE.tmp.$$"
  : >"$tmp"
  for section in heroku render; do
    case "$section" in
      heroku) var=HEROKU_DATABASE_URL ;;
      render) var=RENDER_DATABASE_URL ;;
    esac
    url="$(eval "printf '%s' \"\${$var:-}\"")"
    [ -n "$url" ] || continue
    url_is_valid "$url" || { rm -f "$tmp"; umask "$old_umask"; die "$var is not a valid postgres:// URL"; }
    {
      printf '[%s]\n' "$section"
      printf 'host=%s\n' "$(url_field "$url" host)"
      val="$(url_field "$url" port)"; printf 'port=%s\n' "${val:-5432}"
      printf 'dbname=%s\n' "$(url_field "$url" dbname)"
      val="$(url_field "$url" user)"; [ -z "$val" ] || printf 'user=%s\n' "$val"
      val="$(url_field "$url" password)"; [ -z "$val" ] || printf 'password=%s\n' "$val"
      query="$(url_field "$url" query)"
      if [ -n "$query" ]; then
        printf '%s\n' "$query" | tr '&' '\n' | while IFS= read -r pair || [ -n "$pair" ]; do
          key="${pair%%=*}"; val="${pair#*=}"
          case "$key" in ''|*[!a-z_]*) continue ;; esac
          printf '%s=%s\n' "$key" "$(printf '%s' "$val" | perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/ge')"
        done
      fi
      case "&$query" in *'&sslmode='*) ;; *) printf 'sslmode=require\n' ;; esac
      printf 'application_name=%s\n\n' "$PGAPPNAME"
    } >>"$tmp"
  done
  mv "$tmp" "$PGSERVICEFILE"
  umask "$old_umask"
}

# Password contains a colon (Getsafe hit problems with this).
url_password_has_colon() {
  case "$(url_field "$1" password)" in *:*) return 0 ;; esac
  return 1
}

# ------------------------------------------------------------------- psql --

_psql() {
  PGOPTIONS="${PGOPTIONS:-} -c client_min_messages=warning" psql -X -q -v ON_ERROR_STOP=1 --no-psqlrc "$@"
}
# Tuples-only, unaligned, tab-separated.
psql_src() { _psql -At -F $'\t' -d "$SRC_SERVICE" "$@"; }
psql_dst() { _psql -At -F $'\t' -d "$DST_SERVICE" "$@"; }
# Local control database (Bucardo's), over the unix socket.
psql_ctl() { _psql -At -F $'\t' -h "$PG_SOCKET_DIR" -U postgres -d "${CTL_DB:-bucardo}" "$@"; }

# SQL array literal of the configured schemas: ARRAY['public','app'].
schemas_sql() {
  local out="" s
  local IFS=','
  for s in $SCHEMAS; do
    s="$(printf '%s' "$s" | tr -d '[:space:]')"
    [ -n "$s" ] || continue
    s="$(printf '%s' "$s" | sed "s/'/''/g")"
    out="${out:+$out,}'$s'"
  done
  [ -n "$out" ] || die "SCHEMAS is empty"
  printf 'ARRAY[%s]::text[]' "$out"
}

# Ordinary and partitioned tables in the configured schemas, as quoted
# schema.table names, one per line. $1 is psql_src or psql_dst.
list_tables() {
  "$1" -c "
    SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
    ORDER BY 1"
}

# SELECT of the unique indexes Bucardo uses as keys: for each table in
# $SCHEMAS without a primary key, the index bucardo.validate_goat picks (a
# unique index with no predicate and no expressions, the one with the fewest
# columns, the newest on a tie), as quoted schema.index names. keys.sql
# reports the same choice in detail.
bucardo_key_indexes_sql() {
  printf '%s' "
  SELECT quote_ident(n.nspname) || '.' || quote_ident(ic.relname)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL (
    SELECT i.indexrelid FROM pg_index i
    WHERE i.indrelid = c.oid AND i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL
    ORDER BY i.indnatts, i.indexrelid DESC LIMIT 1) k
  JOIN pg_class ic ON ic.oid = k.indexrelid
  WHERE c.relkind IN ('r','p') AND n.nspname = ANY ($(schemas_sql))
    AND NOT EXISTS (SELECT 1 FROM pg_constraint p WHERE p.conrelid = c.oid AND p.contype = 'p')"
}

# ----------------------------------------------------------- client tools --

# Major version of a server. $1 is psql_src or psql_dst.
server_major() {
  local v
  v="$("$1" -c 'SHOW server_version_num' </dev/null)" || return 1
  case "$v" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$((v / 10000))"
}

# pick_client_major SRC DST MAJOR...: which of the client majors MAJOR...
# should dump a source of major SRC for a target of major DST.
#
# pg_dump refuses to dump a server newer than itself, and its output is only
# meant to load into its own major or a newer one. So: the newest major from
# SRC up to DST (SRC's own when the target is older than the source). When
# the target is older than every major that can read the source, the oldest
# of those; preflight warns that its output may not load into the target.
# Fails when no major is >= SRC.
pick_client_major() {
  local src="$1" dst="$2" hi best="" low="" m
  shift 2
  hi=$(( dst > src ? dst : src ))
  for m in "$@"; do
    [ "$m" -ge "$src" ] || continue
    if [ "$m" -le "$hi" ] && { [ -z "$best" ] || [ "$m" -gt "$best" ]; }; then best="$m"; fi
    if [ -z "$low" ] || [ "$m" -lt "$low" ]; then low="$m"; fi
  done
  best="${best:-$low}"
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

# The pg_dump of one client major.
client_pg_dump() {
  local bin="/usr/lib/postgresql/$1/bin/pg_dump"
  [ -x "$bin" ] || return 1
  printf '%s\n' "$bin"
}

# Picks the client tools for this migration's two servers. Sets
# PG_CLIENT_MAJOR and PG_DUMP.
use_client_tools() {
  local src dst
  src="$(server_major psql_src)" || die "cannot read the source's server version"
  dst="$(server_major psql_dst)" || die "cannot read the target's server version"
  # shellcheck disable=SC2086  # PG_CLIENT_MAJORS is a space-separated list
  PG_CLIENT_MAJOR="$(pick_client_major "$src" "$dst" $PG_CLIENT_MAJORS)" \
    || die "no client tools in this image can dump Postgres $src (it has $PG_CLIENT_MAJORS); rebuild it with $src in PG_CLIENT_MAJORS"
  PG_DUMP="$(client_pg_dump "$PG_CLIENT_MAJOR")" || die "pg_dump $PG_CLIENT_MAJOR is not installed in this image"
  export PG_CLIENT_MAJOR PG_DUMP
}

# --------------------------------------------------------------- privileges --

# Postgres refuses to run as root, and Bucardo files belong to $RUN_USER.
# When the sandbox shell is root, prepare runtime directories and re-exec
# the calling script as $RUN_USER. Usage: ensure_run_user "$@"
ensure_run_user() {
  if [ "$(id -u)" != 0 ] || [ -n "${MIGRATE_NO_DROP:-}" ]; then
    return 0
  fi
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    die "running as root but user $RUN_USER does not exist"
  fi
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 2775 "$PG_SOCKET_DIR"
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$BUCARDO_PIDDIR" "$WORK_DIR" \
    "$LOG_DIR" "$ARTIFACT_DIR" "$STATE_DIR" "$JOB_DIR"
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 0700 "$SECRETS_DIR"
  # A copied-in fallback env file is root-owned; the run user must read it.
  if [ -n "${MIGRATE_ENV_FALLBACK:-}" ]; then
    chown "$RUN_USER" "$MIGRATE_ENV_FALLBACK" "$(dirname "$MIGRATE_ENV_FALLBACK")" 2>/dev/null || true
    chmod 0600 "$MIGRATE_ENV_FALLBACK" 2>/dev/null || true
  fi
  local self
  self="$(readlink -f "$0")"
  exec runuser -u "$RUN_USER" -- "$self" "$@"
}

# Make sure the runtime directories exist when not root.
ensure_dirs() {
  mkdir -p "$LOG_DIR" "$ARTIFACT_DIR" "$STATE_DIR" "$JOB_DIR" 2>/dev/null \
    || die "cannot create $WORK_DIR subdirectories as $(id -un)"
}

# One mutating script at a time. Read-only scripts (status, preflight) and
# the watchdog do not take the lock. No-op where flock is missing.
acquire_lock() {
  command -v flock >/dev/null 2>&1 || return 0
  exec 9>"$STATE_DIR/migrate.lock"
  if ! flock -n 9; then
    die "another migration step is running (lock $STATE_DIR/migrate.lock held)"
  fi
}

# ------------------------------------------------------------------ state --

mark_state()  { ts >"$STATE_DIR/$1"; }
has_state()   { [ -f "$STATE_DIR/$1" ]; }
state_time()  { if [ -f "$STATE_DIR/$1" ]; then cat "$STATE_DIR/$1"; fi; }
clear_state() { local n; for n in "$@"; do rm -f "$STATE_DIR/$n"; done; }

# ---------------------------------------------------------------- confirm --

# Destructive scripts require --confirm. Usage: require_confirm "$@"
require_confirm() {
  local a
  for a in "$@"; do [ "$a" = "--confirm" ] && return 0; done
  die "this step is destructive; rerun with --confirm"
}
has_flag() {
  local flag="$1" a; shift
  for a in "$@"; do [ "$a" = "$flag" ] && return 0; done
  return 1
}

# -------------------------------------------------------------- processes --

# A zombie (exited, not yet reaped) still answers kill -0. Sandboxes may run
# a PID 1 that never reaps orphans, so treat zombies as dead.
pid_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -o stat= -p "$1" 2>/dev/null)" in Z*|'') return 1 ;; esac
  return 0
}

local_pg_running() {
  pg_isready -q -h "$PG_SOCKET_DIR" -p 5432 >/dev/null 2>&1
}

bucardo_mcp_pid() {
  local f="$BUCARDO_PIDDIR/bucardo.mcp.pid"
  [ -r "$f" ] && head -n1 "$f" | tr -dc '0-9'
}
bucardo_running() { pid_alive "$(bucardo_mcp_pid)"; }

watchdog_pid() { [ -r "$STATE_DIR/watchdog.pid" ] && tr -dc '0-9' <"$STATE_DIR/watchdog.pid"; }
watchdog_running() { pid_alive "$(watchdog_pid)"; }

# Run the bucardo CLI from LOG_DIR: only /etc/bucardorc applies (no stray
# ./.bucardorc), and the reason files it writes to the cwd land with the logs.
# fd 9 (the migrate lock) is closed for every child that may daemonize,
# otherwise Bucardo or Postgres would hold the lock forever.
bucardo_cli() { (cd "$LOG_DIR" && bucardo --quiet "$@" 9>&-); }
bucardo_cli_verbose() { (cd "$LOG_DIR" && bucardo "$@" 9>&-); }

# True when the local control database has Bucardo installed.
bucardo_installed() {
  local_pg_running || return 1
  [ "$(CTL_DB=postgres psql_ctl -c "SELECT 1 FROM pg_database WHERE datname = 'bucardo'" 2>/dev/null)" = 1 ]
}

# Stop Bucardo on purpose: tell the watchdog to leave it alone first.
stop_bucardo_intentionally() {
  mark_state bucardo.stopped_intentionally
  if bucardo_running; then
    log "stopping Bucardo"
    bucardo_cli stop "stopped by $(basename "$0")" 2>&1 | redact || true
    local i=0
    while bucardo_running && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
    bucardo_running && warn "Bucardo MCP still running after 60s"
  fi
  return 0
}

stop_watchdog() {
  mark_state watchdog.stop
  local pid
  pid="$(watchdog_pid)"
  if pid_alive "$pid"; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$STATE_DIR/watchdog.pid"
}

# Start the watchdog detached from this exec session.
start_watchdog() {
  clear_state watchdog.stop
  if watchdog_running; then
    return 0
  fi
  setsid nohup "$MIGRATE_HOME/watchdog.sh" >>"$LOG_DIR/watchdog.log" 2>&1 </dev/null 9>&- &
  local i=0
  while ! watchdog_running && [ $i -lt 10 ]; do sleep 1; i=$((i + 1)); done
  watchdog_running || warn "watchdog did not report a pid"
}

# Optional Slack notification. Never includes secrets.
notify() {
  local msg="$1"
  [ -n "${SLACK_WEBHOOK_URL:-}" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  jq -n --arg t "[pg-migrate ${SANDBOX_LABEL:-$SYNC_NAME}] $msg" '{text: $t}' |
    curl -fsS -m 10 -H 'Content-Type: application/json' --data @- "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ------------------------------------------------------- replication state --

# Bucardo's sync row in the control database: "onetimecopy<TAB>status", or
# nothing if the sync does not exist (or Bucardo is not installed).
ctl_sync_row() {
  bucardo_installed || return 0
  psql_ctl -c "SELECT onetimecopy, status FROM bucardo.sync WHERE name = '$(printf '%s' "$SYNC_NAME" | sed "s/'/''/g")'" 2>/dev/null || true
}

# Number of COPY commands currently running on the target for our role.
target_copies_running() {
  psql_dst -c "
    SELECT count(*) FROM pg_stat_activity
    WHERE usename = current_user AND pid <> pg_backend_pid()
      AND state <> 'idle' AND query ILIKE 'COPY %'" 2>/dev/null || echo 0
}

# The initial copy is done when: the sync exists and Bucardo has turned
# onetimecopy off in the control database, no COPY is running on the target,
# and every source table that has rows has at least one row on the target.
# Prints a one-line reason on failure.
copy_done() {
  has_state sync.started || { echo "sync not started"; return 1; }
  local row otc
  row="$(ctl_sync_row)"
  [ -n "$row" ] || { echo "sync $SYNC_NAME not found in Bucardo's control database"; return 1; }
  otc="${row%%	*}"
  [ "$otc" = 0 ] || { echo "Bucardo onetimecopy still $otc"; return 1; }
  [ "$(target_copies_running)" = 0 ] || { echo "COPY still running on target"; return 1; }
  local t missing=""
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ "$(psql_src -c "SELECT EXISTS (SELECT 1 FROM $t)" </dev/null)" = t ] &&
       [ "$(psql_dst -c "SELECT EXISTS (SELECT 1 FROM $t)" </dev/null)" != t ]; then
      missing="$missing $t"
    fi
  done <<EOF
$(list_tables psql_src)
EOF
  [ -z "$missing" ] || { echo "target still empty for:$missing"; return 1; }
  return 0
}

# ------------------------------------------------------------ SQL quoting --

# SQL string literal: lit "O'Brien" -> 'O''Brien'
lit() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }
# SQL identifier: ident 'My Table' -> "My Table"
ident() { printf '"%s"' "$(printf '%s' "$1" | sed 's/"/""/g')"; }

# CREATE statements for one extension, keeping its schema so qualified
# references (e.g. heroku_ext.gen_random_uuid()) still resolve on Render.
extension_sql() {
  local ext="$1" schema="$2"
  case "$schema" in
    pg_catalog|'') printf 'CREATE EXTENSION IF NOT EXISTS %s;\n' "$(ident "$ext")" ;;
    *) printf 'CREATE SCHEMA IF NOT EXISTS %s;\nCREATE EXTENSION IF NOT EXISTS %s WITH SCHEMA %s;\n' \
         "$(ident "$schema")" "$(ident "$ext")" "$(ident "$schema")" ;;
  esac
}
