#!/usr/bin/env bash
# Keep the local control Postgres and Bucardo running.
#
# Every $WATCHDOG_INTERVAL seconds (default 60): if the local Postgres is
# down, run start_local.sh; if Bucardo's MCP is down, run `bucardo start`.
# Every restart is logged to $LOG_DIR/watchdog.log and, if SLACK_WEBHOOK_URL
# is set, posted to Slack.
#
# Exits when $STATE_DIR/watchdog.stop exists (cutover.sh, reset_target.sh).
# Leaves Bucardo alone while $STATE_DIR/bucardo.stopped_intentionally exists.
# Started detached by start_sync.sh; only one instance runs at a time.
set -uo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs

if watchdog_running && [ "$(watchdog_pid)" != "$$" ]; then
  log "watchdog already running (pid $(watchdog_pid)); exiting"
  exit 0
fi
echo $$ >"$STATE_DIR/watchdog.pid"
trap 'if [ "$(watchdog_pid)" = "$$" ]; then rm -f "$STATE_DIR/watchdog.pid"; fi' EXIT
trap 'log "watchdog stopping on signal"; exit 0' TERM INT

restart_event() {
  local what="$1"
  printf '%s RESTART %s\n' "$(ts)" "$what" >>"$STATE_DIR/watchdog.restarts"
  warn "restarting $what"
  notify "watchdog restarted $what"
}

log "watchdog started (pid $$, interval ${WATCHDOG_INTERVAL}s)"
while true; do
  if [ -f "$STATE_DIR/watchdog.stop" ]; then
    log "watchdog.stop found; exiting"
    exit 0
  fi

  if ! local_pg_running; then
    restart_event "local Postgres"
    "$MIGRATE_HOME/start_local.sh" || warn "start_local.sh failed; will retry"
  fi

  if [ ! -f "$STATE_DIR/bucardo.stopped_intentionally" ] && local_pg_running && ! bucardo_running; then
    restart_event "Bucardo"
    bucardo_cli start "restarted by watchdog" 2>&1 | redact >&2 || warn "bucardo start failed; will retry"
  fi

  date -u +%s >"$STATE_DIR/watchdog.heartbeat"
  sleep "$WATCHDOG_INTERVAL" &
  wait $! || true
done
