#!/usr/bin/env bash
# Import the pushed image into the sandbox group, creating the group if
# needed. Refuses to continue unless the import succeeded.
#
#   local/import_image.sh
#
# Private registries: set REGISTRY_USERNAME and REGISTRY_PASSWORD (a token);
# the password goes to the CLI on stdin and Render does not store it.
# The image ID is recorded in .state/images/<tag>.id; create_sandbox.sh
# uses the most recent import unless IMAGE_TAG is set.
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

TAG="${IMAGE_TAG:-$(git_tag)}"
REF="$IMAGE_REPO:$TAG"
RECORDS="$(image_record_dir)"
mkdir -p "$RECORDS"

# ------------------------------------------------------------ group
gid="$(group_id)" || die "could not list sandbox groups (is the CLI logged in? render login / RENDER_API_KEY)"
if [ -z "$gid" ]; then
  log "creating sandbox group $SANDBOX_GROUP"
  gid="$(render_json sandbox-groups create --name "$SANDBOX_GROUP" | jq -r .id)"
  [ -n "$gid" ] && [ "$gid" != null ] || die "could not create sandbox group $SANDBOX_GROUP"
fi
log "sandbox group $SANDBOX_GROUP is $gid"
printf '%s\n' "$gid" >"$LOCAL_STATE/sandbox_group.id"

image_get() { render_json sandbox-groups images get "$1" --group "$gid"; }

# ------------------------------------------------------- already done?
if [ -s "$RECORDS/$TAG.id" ]; then
  prev="$(cat "$RECORDS/$TAG.id")"
  if [ "$(image_get "$prev" 2>/dev/null | jq -r '.status // empty')" = succeeded ]; then
    log "$REF already imported as $prev"
    printf '%s\n' "$TAG" >"$RECORDS/latest"
    echo "$prev"
    exit 0
  fi
fi

# --------------------------------------------------------------- import
args=(sandbox-groups images import "$REF" --name "$IMAGE_NAME" --group "$gid" --wait)
log "importing $REF (waits up to 10 minutes, then keeps polling)"
rc=0
if [ -n "${REGISTRY_USERNAME:-}" ]; then
  [ -n "${REGISTRY_PASSWORD:-}" ] || die "REGISTRY_USERNAME is set but REGISTRY_PASSWORD is not"
  out="$(printf '%s' "$REGISTRY_PASSWORD" | render_json "${args[@]}" --registry-username "$REGISTRY_USERNAME" --registry-password-stdin)" || rc=$?
else
  out="$(render_json "${args[@]}")" || rc=$?
fi
id="$(jq -r '.id // empty' <<<"$out" 2>/dev/null || true)"
[ -n "$id" ] || die "import did not return an image ID (exit $rc)"

# --wait gives up after 10 minutes without cancelling the import; keep polling.
status="$(jq -r '.status // empty' <<<"$out")"
deadline=$(( $(date +%s) + ${IMPORT_TIMEOUT_S:-3600} ))
while [ "$status" = pending ] || [ "$status" = running ]; do
  [ "$(date +%s)" -lt "$deadline" ] || die "import $id still $status after ${IMPORT_TIMEOUT_S:-3600}s"
  sleep 15
  out="$(image_get "$id")" || continue
  status="$(jq -r .status <<<"$out")"
  log "import $id: $status"
done

if [ "$status" != succeeded ]; then
  jq -r '"import \(.id) \(.status): \(.failureCode // "") \(.failureReason // "")"' <<<"$out" >&2
  die "image import did not succeed; not continuing"
fi
printf '%s\n' "$id" >"$RECORDS/$TAG.id"
printf '%s\n' "$TAG" >"$RECORDS/latest"
log "imported $REF as $id"
echo "$id"
