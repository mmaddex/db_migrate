#!/usr/bin/env bash
# Run a migration script detached from the exec session, so a long step
# (build_indexes.sh can take hours) survives a dropped `render sandboxes
# exec` connection.
#
#   job.sh start <script> [args...]   start /opt/migrate/<script> in the background
#   job.sh status <script>            print: running | succeeded | failed (rc N) | unknown
#   job.sh wait <script> [timeout_s]  wait for it; exit with its exit code
#   job.sh tail <script> [lines]      print the end of its log
#
# Output goes to $JOB_DIR/<name>.log; the exit code to $JOB_DIR/<name>.rc.
set -euo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"
ensure_run_user "$@"
ensure_dirs

cmd="${1:-}"; script="${2:-}"
if [ -z "$cmd" ] || [ -z "$script" ]; then die "usage: job.sh start|status|wait|tail <script> [args]"; fi
shift 2
name="$(basename "$script" .sh)"
case "$name" in *[!A-Za-z0-9_-]*) die "bad script name" ;; esac
path="$MIGRATE_HOME/$name.sh"
pidf="$JOB_DIR/$name.pid" rcf="$JOB_DIR/$name.rc" logf="$JOB_DIR/$name.log"

is_running() { [ -f "$pidf" ] && pid_alive "$(cat "$pidf")"; }

state() {
  if is_running; then echo running
  elif [ -f "$rcf" ]; then
    local rc; rc="$(cat "$rcf")"
    if [ "$rc" = 0 ]; then echo succeeded; else echo "failed (rc $rc)"; fi
  elif [ -f "$pidf" ]; then echo "unknown (process gone without an exit code)"
  else echo "not started"
  fi
}

case "$cmd" in
  start)
    [ -x "$path" ] || die "no such script: $name.sh"
    is_running && die "$name is already running (pid $(cat "$pidf"))"
    rm -f "$rcf"
    printf '%s %s\n' "$name.sh" "$*" >"$JOB_DIR/$name.cmd"
    ts >"$JOB_DIR/$name.started"
    # The child records its own exit code; setsid detaches it from the
    # exec session so a dropped connection does not kill it.
    setsid nohup bash -c '
      "$1" "${@:4}" >>"$2" 2>&1 </dev/null
      echo $? >"$3"
    ' job "$path" "$logf" "$rcf" "$@" >/dev/null 2>&1 </dev/null &
    echo $! >"$pidf"
    log "started $name (pid $!); log: $logf"
    echo "started"
    ;;
  status)
    state
    ;;
  wait)
    timeout_s="${1:-0}" waited=0
    while is_running; do
      sleep 5; waited=$((waited + 5))
      if [ "$timeout_s" -gt 0 ] && [ "$waited" -ge "$timeout_s" ]; then
        echo "still running after ${timeout_s}s"; exit 124
      fi
    done
    [ -f "$rcf" ] || die "$name exited without recording an exit code"
    exit "$(cat "$rcf")"
    ;;
  tail)
    tail -n "${1:-50}" "$logf" 2>/dev/null | redact || true
    ;;
  *)
    die "unknown command $cmd"
    ;;
esac
