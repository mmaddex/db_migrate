#!/usr/bin/env bash
# Platform facts from inside a migration sandbox, for docs/spike.md and for
# setting up the Render database's inbound IP rules. Read-only.
#
# Run it after start_sync.sh to confirm that detached processes (Bucardo,
# the watchdog) survived the exec session that started them.
set -uo pipefail
# shellcheck source=sandbox/env.sh
. "$(dirname "$0")/env.sh"

yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
say "exec user:           $MIGRATE_EXEC_USER (scripts run as $(id -un 2>/dev/null))"
say "arch / kernel:       $(uname -m) / $(uname -r)"
say "os:                  $( (. /etc/os-release && echo "$PRETTY_NAME") 2>/dev/null)"
say "image revision:      $IMAGE_REVISION (PG $PG_MAJOR, Bucardo $BUCARDO_VERSION)"
say "PID 1:               $(tr '\0' ' ' </proc/1/cmdline 2>/dev/null | cut -c1-80)"
say "zombie processes:    $(ps -eo stat= 2>/dev/null | grep -c '^Z' || true)"
say "umask / HOME:        $(umask) / ${HOME:-unset}"
say "/run filesystem:     $(df -T /run 2>/dev/null | awk 'NR == 2 {print $2}')"
say "Heroku URL in env:   $([ -n "${HEROKU_DATABASE_URL:-}" ] && echo yes || echo no)"
say "Render URL in env:   $([ -n "${RENDER_DATABASE_URL:-}" ] && echo yes || echo no)"
say "control Postgres up: $(yesno local_pg_running)"
say "Bucardo running:     $(yesno bucardo_running)"
say "watchdog running:    $(yesno watchdog_running)"
ip="$(curl -fsS -m 10 https://api.ipify.org 2>/dev/null || echo unknown)"
say "outbound IP:         $ip   (allow it in the Render database's inbound IP rules)"
for var in HEROKU_DATABASE_URL RENDER_DATABASE_URL; do
  url="${!var:-}"
  [ -n "$url" ] || continue
  host="$(url_field "$url" host)"; port="$(url_field "$url" port)"; port="${port:-5432}"
  say "TCP $host:$port: $(yesno timeout 5 bash -c "</dev/tcp/$host/$port")"
done
