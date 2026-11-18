#!/usr/bin/env bash
# Initialise (if missing) and start the local Postgres cluster that holds
# Bucardo's control database, then install Bucardo into it (if missing).
# Idempotent. Called by start_sync.sh and watchdog.sh.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs

if [ ! -s "$PGDATA_LOCAL/PG_VERSION" ]; then
  log "initialising local control cluster in $PGDATA_LOCAL"
  mkdir -p "$PGDATA_LOCAL"
  chmod 0700 "$PGDATA_LOCAL"
  initdb -D "$PGDATA_LOCAL" -U postgres --auth-local=trust --auth-host=reject \
    --encoding=UTF8 --locale=C.UTF-8 >"$LOG_DIR/initdb.log" 2>&1 \
    || { cat "$LOG_DIR/initdb.log" >&2; die "initdb failed"; }
  cat >>"$PGDATA_LOCAL/postgresql.conf" <<EOF

# pg-migrate: Bucardo control database. Unix socket only.
listen_addresses = ''
unix_socket_directories = '$PG_SOCKET_DIR'
port = 5432
max_connections = 100
shared_buffers = 128MB
jit = off
log_line_prefix = '%m [%p] '
EOF
fi

if local_pg_running; then
  log "local control cluster already running"
else
  # A stale postmaster.pid from a dead sandbox process is handled by pg_ctl.
  log "starting local control cluster"
  pg_ctl -D "$PGDATA_LOCAL" -l "$LOG_DIR/postgres.log" -w -t 60 start >/dev/null \
    || { tail -n 20 "$LOG_DIR/postgres.log" >&2 || true; die "local Postgres did not start"; }
fi

if bucardo_installed; then
  log "Bucardo already installed in the control database"
else
  log "installing Bucardo $BUCARDO_VERSION into the control database"
  (cd "$LOG_DIR" && bucardo install --batch --quiet \
     --dbuser postgres --dbname postgres --dbhost "$PG_SOCKET_DIR" \
     --piddir "$BUCARDO_PIDDIR") >"$LOG_DIR/bucardo-install.log" 2>&1 \
    || { cat "$LOG_DIR/bucardo-install.log" >&2; die "bucardo install failed"; }
  bucardo_installed || die "bucardo install reported success but the bucardo database is missing"
fi

# Bucardo keeps a few file paths in its control database (not in bucardorc),
# defaulting to /var/log/bucardo. Point them at the work directory so the
# files are writable and fetch_artifacts.sh picks them up.
psql_ctl -c "
  UPDATE bucardo.bucardo_config SET setting = CASE name
      WHEN 'reason_file'       THEN $(lit "$LOG_DIR/restart.reason")
      WHEN 'warning_file'      THEN $(lit "$LOG_DIR/warning.log")
      WHEN 'log_conflict_file' THEN $(lit "$LOG_DIR/conflict.log")
      WHEN 'piddir'            THEN $(lit "$BUCARDO_PIDDIR")
    END
  WHERE name IN ('reason_file', 'warning_file', 'log_conflict_file', 'piddir')" >/dev/null \
  || die "could not set Bucardo file paths"
