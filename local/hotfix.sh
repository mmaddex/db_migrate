#!/usr/bin/env bash
# Emergency only: copy one changed script from sandbox/ into the running
# sandbox's /opt/migrate. Logs, locally and in the sandbox, that the running
# code no longer matches the image tag.
#
#   local/hotfix.sh sandbox/cutover.sh
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] || die "usage: local/hotfix.sh sandbox/<script>"
name="$(basename "$file")"
[ -f "$REPO_ROOT/sandbox/$name" ] || die "$name is not a file in sandbox/"
case "$name" in *.sh) bash -n "$file" || die "$file has a syntax error" ;; esac

id="$(require_running_sandbox)"
tag="$(sed -n 's/^IMAGE_TAG=//p' "$MIGRATION_DIR/sandbox.info" 2>/dev/null || echo unknown)"
sum="$(shasum -a 256 "$file" 2>/dev/null || sha256sum "$file")"; sum="${sum%% *}"
msg="$(ts) HOTFIX $name sha256=$sum from git $(git_tag); running code no longer matches image tag $tag"

render sandboxes copy "$file" "$id:/opt/migrate/$name" >/dev/null
case "$name" in *.sh) render sandboxes exec "$id" -- chmod 0755 "/opt/migrate/$name" ;; esac
render sandboxes exec "$id" -- sh -c "echo '$msg' >> /var/lib/migrate/logs/hotfix.log"
printf '%s\n' "$msg" >>"$MIGRATION_DIR/hotfix.log"
warn "$msg"
