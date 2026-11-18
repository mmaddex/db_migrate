#!/usr/bin/env bash
# Run cleanup_source.sh from the operator machine, for when the sandbox is
# gone. Uses the same script as the image.
#
#   local/cleanup_source.sh --confirm            plain psql + perl on this machine
#   local/cleanup_source.sh --docker --confirm   inside the migration image
#   local/cleanup_source.sh --verify-only        read-only check
#
# CLEANUP_DOCKER_ARGS adds `docker run` options (e.g. --network host).
set -euo pipefail
# shellcheck source=local/config.sh
. "$(dirname "$0")/config.sh"

[ -f "$MIGRATION_ENV" ] || die "missing $MIGRATION_ENV"
args=()
use_docker=0
for a in "$@"; do
  if [ "$a" = --docker ]; then use_docker=1; else args+=("$a"); fi
done

rc=0
if [ $use_docker = 1 ]; then
  tag="${IMAGE_TAG:-$(cat "$(image_record_dir)/latest" 2>/dev/null || git_tag)}"
  # Not --env-file: Docker keeps quotes that the Render CLI (and the UI's
  # single-quoted values) rely on being stripped. Load, then pass through.
  (
    load_conf "$MIGRATION_ENV"
    export HEROKU_DATABASE_URL RENDER_DATABASE_URL SYNC_NAME SCHEMAS
    exec docker run --rm -e HEROKU_DATABASE_URL -e RENDER_DATABASE_URL -e SYNC_NAME -e SCHEMAS \
      ${CLEANUP_DOCKER_ARGS:-} "$IMAGE_REPO:$tag" /opt/migrate/cleanup_source.sh ${args[@]+"${args[@]}"}
  ) || rc=$?
else
  command -v psql >/dev/null 2>&1 || die "psql is required (or use --docker)"
  command -v perl >/dev/null 2>&1 || die "perl is required (or use --docker)"
  work="$MIGRATION_DIR/local-cleanup"
  mkdir -p "$work"
  (
    # Work and secrets directories on this machine instead of the sandbox's.
    export WORK_DIR="$work" SECRETS_DIR="$work/.secrets"
    load_conf "$MIGRATION_ENV"
    export HEROKU_DATABASE_URL RENDER_DATABASE_URL SYNC_NAME SCHEMAS
    exec "$REPO_ROOT/sandbox/cleanup_source.sh" ${args[@]+"${args[@]}"}
  ) || rc=$?
fi
if [ $rc -eq 0 ] && has_flag --confirm "$@"; then
  ts >"$MIGRATION_DIR/cleanup_source.done"
fi
exit $rc
