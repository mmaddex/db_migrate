#!/usr/bin/env bash
# Terminate this migration's sandbox.
#
#   local/teardown.sh --confirm
#   local/teardown.sh --confirm --force   # skip the cleanup check (e.g. the
#                                         # Heroku database is already gone)
#
# Refuses until cleanup_source.sh has succeeded: either a local marker from
# local/cleanup_source.sh, or a read-only check run in the sandbox. Fetches
# artifacts one last time before terminating.
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render
require_confirm "$@"

id="$(sandbox_id)"
st="$(sandbox_status "$id" || echo unknown)"

if has_flag --force "$@"; then
  warn "--force: not checking that Bucardo objects were removed from Heroku"
elif [ -f "$MIGRATION_DIR/cleanup_source.done" ]; then
  log "cleanup_source succeeded at $(cat "$MIGRATION_DIR/cleanup_source.done")"
elif [ "$st" = running ] && render sandboxes exec "$id" -- /opt/migrate/cleanup_source.sh --verify-only; then
  log "verified in the sandbox: no Bucardo objects remain"
else
  die "cleanup_source.sh has not succeeded; run: MIGRATION=$MIGRATION local/run.sh cleanup_source.sh --confirm"
fi

if [ "$st" = running ]; then
  "$REPO_ROOT/local/fetch_artifacts.sh" || warn "final artifact fetch failed"
fi
if [ "$st" != terminated ] && [ "$st" != missing ]; then
  render sandboxes stop "$id" --confirm >/dev/null || die "could not terminate $id"
fi
mv "$SANDBOX_ID_FILE" "$MIGRATION_DIR/.sandbox_id.terminated"
ts >"$MIGRATION_DIR/teardown.done"
log "sandbox $id terminated"
