#!/usr/bin/env bash
# Run one of the /opt/migrate scripts in this migration's sandbox and pass
# its exit code through.
#
#   local/run.sh preflight.sh
#   local/run.sh cutover.sh --confirm
#   local/run.sh --detach build_indexes.sh   # survives a dropped connection
#
# --detach starts the script with /opt/migrate/job.sh, then polls it with
# short exec calls and prints new log lines until it finishes.
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

DETACH=0
if [ "${1:-}" = --detach ]; then DETACH=1; shift; fi
script="${1:-}"
[ -n "$script" ] || die "usage: local/run.sh [--detach] <script.sh> [args...]"
shift
case "$script" in
  *[!a-z_.]*|.*) die "script must be a plain /opt/migrate script name, e.g. preflight.sh" ;;
  *.sh) ;;
  *) script="$script.sh" ;;
esac

id="$(require_running_sandbox)"

if [ $DETACH = 0 ]; then
  exec render sandboxes exec "$id" -- "/opt/migrate/$script" "$@"
fi

render sandboxes exec "$id" -- /opt/migrate/job.sh start "$script" "$@"
shown=0
while :; do
  sleep "${POLL_S:-15}"
  st="$(render sandboxes exec "$id" -- /opt/migrate/job.sh status "$script" 2>/dev/null || echo unknown)"
  # Print log lines we have not shown yet.
  lines="$(render sandboxes exec "$id" -- /opt/migrate/job.sh tail "$script" 100000 2>/dev/null || true)"
  total="$(printf '%s\n' "$lines" | grep -c . || true)"
  if [ "$total" -gt "$shown" ]; then
    printf '%s\n' "$lines" | tail -n "$((total - shown))"
    shown=$total
  fi
  case "$st" in
    running*) ;;
    succeeded) exit 0 ;;
    "failed (rc "*) rc="${st#failed (rc }"; exit "${rc%)}" ;;
    *) die "job $script: $st" ;;
  esac
done
