#!/usr/bin/env bash
# Download the sandbox's work directory (schema dumps, reports, logs, state)
# to migrations/<name>/artifacts/<timestamp>/. Run it after each phase.
# The work directory never contains credentials (they live elsewhere).
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

id="$(require_running_sandbox)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$ARTIFACTS_DIR/$stamp"
tmp="$ARTIFACTS_DIR/.partial-$stamp"
mkdir -p "$tmp"
render sandboxes copy "$id:/var/lib/migrate" "$tmp" >/dev/null || { rm -rf "$tmp"; die "copy failed"; }
# A directory download may land as <tmp>/migrate/...; flatten that.
if [ -d "$tmp/migrate" ] && [ "$(find "$tmp" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = 1 ]; then
  mv "$tmp/migrate" "$dest"; rmdir "$tmp"
else
  mv "$tmp" "$dest"
fi
ln -sfn "$stamp" "$ARTIFACTS_DIR/latest"
log "artifacts in $dest"
echo "$dest"
