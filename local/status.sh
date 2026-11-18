#!/usr/bin/env bash
# Show the sandbox state, then the migration status from inside it.
#
#   local/status.sh            human-readable
#   local/status.sh --exact    exact row counts
#   local/status.sh --json     {"sandbox": {...}, "status": {...}, "error": ...}
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

JSON=0
has_flag --json "$@" && JSON=1

if [ ! -s "$SANDBOX_ID_FILE" ]; then
  [ $JSON = 1 ] && { jq -n '{sandbox: null, status: null, error: "no sandbox created yet"}'; exit 0; }
  die "no sandbox for migration '$MIGRATION'; run local/create_sandbox.sh"
fi
id="$(sandbox_id)"
sb="$(sandbox_json "$id" || true)"
st=missing
[ -n "$sb" ] && st="$(jq -r '.status // "missing"' <<<"$sb")"

if [ $JSON = 1 ]; then
  remote=null err=null
  if [ "$st" = running ]; then
    if out="$(render sandboxes exec "$id" -- /opt/migrate/status.sh "$@" 2>/dev/null)" && jq -e . >/dev/null 2>&1 <<<"$out"; then
      remote="$out"
    else
      err='"status.sh failed in the sandbox"'
    fi
  else
    err="$(jq -Rn --arg s "$st" '"sandbox is \($s)"')"
  fi
  jq -n --argjson sb "${sb:-null}" --argjson st "$remote" --argjson err "$err" '{sandbox: $sb, status: $st, error: $err}'
  exit 0
fi

echo "migration: $MIGRATION   sandbox: $id ($st)   name: $SANDBOX_NAME"
[ -f "$MIGRATION_DIR/sandbox.info" ] && sed 's/^/  /' "$MIGRATION_DIR/sandbox.info"
[ "$st" = running ] || die "sandbox is $st"
echo
exec render sandboxes exec "$id" -- /opt/migrate/status.sh "$@"
