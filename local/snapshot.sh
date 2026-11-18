#!/usr/bin/env bash
# Optional: take a runtime snapshot (filesystem + memory) of the sandbox
# before a risky step. Restore with:
#   render sandboxes create --snapshot-id <snp-...> --plan <same plan> --group <group>
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

id="$(require_running_sandbox)"
name="pgm-$MIGRATION-$(date -u +%Y%m%d%H%M%S)"
log "snapshotting $id as $name (runtime)"
out="$(render_json sandboxes snapshots create "$id" --kind runtime --name "$name" --wait)" || die "snapshot failed"
jq -r '"\(.id) \(.status // "") \(.name // "")"' <<<"$out"
printf '%s %s\n' "$(ts)" "$(jq -r .id <<<"$out")" >>"$MIGRATION_DIR/snapshots.log"
