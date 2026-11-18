#!/usr/bin/env bash
# Build the migration image for the sandbox platform, tag it with the git
# SHA and push it.
#
#   local/build_image.sh            build and push $IMAGE_REPO:<git sha>
#   local/build_image.sh --load     build into the local Docker only (no push)
#
# Refuses to build from a dirty tree (the tag would not describe the code);
# set ALLOW_DIRTY=1 to override.
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"

command -v docker >/dev/null 2>&1 || die "docker is required"
TAG="${IMAGE_TAG:-$(git_tag)}"
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  die "working tree has uncommitted changes; commit first or set ALLOW_DIRTY=1"
fi
case "$IMAGE_REPO" in *CHANGE-ME*) die "set IMAGE_REPO in local/config.local.sh (e.g. ghcr.io/<org>/pg-migrate)" ;; esac

output=(--push)
has_flag --load "$@" && output=(--load)

log "building $IMAGE_REPO:$TAG for $IMAGE_PLATFORM (PG_MAJOR=$PG_MAJOR)"
# Plain single-platform image: no provenance/SBOM attestation manifests,
# which keeps what the sandbox importer sees simple.
docker buildx build \
  --platform "$IMAGE_PLATFORM" \
  --provenance=false --sbom=false \
  -f "$REPO_ROOT/image/Dockerfile" \
  --build-arg "PG_MAJOR=$PG_MAJOR" \
  --build-arg "GIT_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "$TAG")" \
  ${BASE_IMAGE:+--build-arg "BASE_IMAGE=$BASE_IMAGE"} \
  -t "$IMAGE_REPO:$TAG" \
  "${output[@]}" \
  "$REPO_ROOT"

mkdir -p "$(image_record_dir)"
ts >"$(image_record_dir)/$TAG.built"
log "built $IMAGE_REPO:$TAG"
echo "$IMAGE_REPO:$TAG"
