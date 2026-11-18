#!/usr/bin/env bash
# Create (or reuse) the sandbox for this migration.
#
#   MIGRATION=<name> local/create_sandbox.sh
#
# Reuses a running/creating sandbox named $SANDBOX_NAME. Otherwise creates
# one from the imported image with no lifetime or idle timeout, passing
# migration.env with --env-file. Does not use --wait (it gives up after 60s
# and leaves the sandbox starting); polls `sandboxes list` until running.
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"
require_render

[ -f "$MIGRATION_ENV" ] || die "missing $MIGRATION_ENV (copy migration.env.example)"
for k in HEROKU_DATABASE_URL RENDER_DATABASE_URL; do
  env_file_has "$MIGRATION_ENV" "$k" || die "$k is not set in $MIGRATION_ENV"
done
chmod 600 "$MIGRATION_ENV" 2>/dev/null || true

gid="$(require_group_id)"

# -------------------------------------------------------------- reuse
existing="$(render_json sandboxes list --name "$SANDBOX_NAME" --group "$gid" |
  jq -c '[.[] | select(.status == "running" or .status == "creating" or .status == "resuming")] | sort_by(.createdAt) | last // empty')"
if [ -n "$existing" ]; then
  id="$(jq -r .id <<<"$existing")"
  log "reusing sandbox $id ($(jq -r .status <<<"$existing")) named $SANDBOX_NAME"
else
  # ------------------------------------------------------------ image
  RECORDS="$(image_record_dir)"
  tag="${IMAGE_TAG:-$(cat "$RECORDS/latest" 2>/dev/null || true)}"
  [ -n "$tag" ] || die "no imported image; run local/import_image.sh"
  [ -s "$RECORDS/$tag.id" ] || die "image tag $tag was not imported; run IMAGE_TAG=$tag local/import_image.sh"
  image_id="$(cat "$RECORDS/$tag.id")"

  log "creating sandbox $SANDBOX_NAME from $IMAGE_REPO:$tag ($image_id), plan $SANDBOX_PLAN"
  created="$(render_json sandboxes create \
    --group "$gid" --image "$image_id" --name "$SANDBOX_NAME" \
    --timeout=0 --idle-timeout=0 --plan "$SANDBOX_PLAN" \
    --env-file "$MIGRATION_ENV" --env-var "SANDBOX_LABEL=$MIGRATION")" \
    || die "sandboxes create failed"
  id="$(jq -r '.id // empty' <<<"$created")"
  [ -n "$id" ] || die "sandboxes create returned no ID"
  printf 'IMAGE_TAG=%s\nIMAGE_ID=%s\nCREATED_AT=%s\n' "$tag" "$image_id" "$(ts)" >"$MIGRATION_DIR/sandbox.info"
fi
printf '%s\n' "$id" >"$SANDBOX_ID_FILE"

# --------------------------------------------------------------- wait
start=$(date +%s)
deadline=$(( start + ${CREATE_TIMEOUT_S:-900} ))
while :; do
  st="$(sandbox_status "$id" || echo unknown)"
  case "$st" in
    running) break ;;
    errored|terminated|missing) die "sandbox $id is $st" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || die "sandbox $id still $st after ${CREATE_TIMEOUT_S:-900}s (it may still start; rerun to keep waiting)"
  sleep 5
done
log "sandbox $id running (waited $(( $(date +%s) - start ))s)"
echo "$id"
