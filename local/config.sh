# shellcheck shell=bash
#
# Operator settings, sourced by every script in local/.
# Precedence: environment > migrations/<name>/migration.conf >
# local/config.local.sh (gitignored) > the defaults below.
#
# Several migrations can run side by side. Each one lives in its own
# directory, migrations/<MIGRATION>/, holding:
#   migration.env   secrets passed to the sandbox with --env-file (0600)
#   migration.conf  per-migration, non-secret overrides (SANDBOX_PLAN, ...)
#   .sandbox_id     the sandbox this migration runs in
#   artifacts/      downloads from fetch_artifacts.sh
# Select one with MIGRATION=<name> (default: "default").
#
# Keep this file bash 3.2 compatible (macOS /bin/bash).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

# shellcheck source=local/lib.sh
. "$REPO_ROOT/local/lib.sh"

# ----------------------------------------------------------- migration
: "${MIGRATION:=default}"
case "$MIGRATION" in
  *[!a-z0-9-]*|-*|'') die "MIGRATION must be lowercase letters, digits and dashes (got '$MIGRATION')" ;;
esac
: "${MIGRATIONS_DIR:=$REPO_ROOT/migrations}"
MIGRATION_DIR="$MIGRATIONS_DIR/$MIGRATION"
# Per-migration settings fill in anything the environment did not set;
# the defaults below fill in the rest.
if [ -f "$MIGRATION_DIR/migration.conf" ]; then
  load_conf "$MIGRATION_DIR/migration.conf"
fi
# Operator defaults (gitignored). Use the `: "${VAR:=value}"` form there so
# the environment and migration.conf still win.
if [ -f "$REPO_ROOT/local/config.local.sh" ]; then
  # shellcheck disable=SC1091
  . "$REPO_ROOT/local/config.local.sh"
fi

# ---------------------------------------------------------------- image
# Registry repository the image is pushed to and imported from.
: "${IMAGE_REPO:=ghcr.io/CHANGE-ME/pg-migrate}"
# Platform Render sandboxes run on (docs/spike.md: uname -m).
: "${IMAGE_PLATFORM:=linux/amd64}"
# Postgres major for the image: Render's version, >= Heroku's.
: "${PG_MAJOR:=17}"
# Name the image gets inside the sandbox group (all tags share it).
: "${IMAGE_NAME:=pg-migrate}"
# IMAGE_TAG defaults to the git SHA when building/importing, and to the
# most recently imported tag when creating a sandbox. Set it to pin one.
: "${IMAGE_TAG:=}"

# -------------------------------------------------------------- sandbox
: "${SANDBOX_GROUP:=pg-migration}"
: "${SANDBOX_PLAN:=2c-4g}"
: "${SANDBOX_NAME:=pg-migrate-$MIGRATION}"
# Minimum Render CLI: 2.34.0 added image import and --image, but
# `sandboxes create --name` and `sandboxes list --name` arrived in 2.36.0.
RENDER_MIN_VERSION=2.36.0

MIGRATION_ENV="$MIGRATION_DIR/migration.env"
SANDBOX_ID_FILE="$MIGRATION_DIR/.sandbox_id"
ARTIFACTS_DIR="$MIGRATION_DIR/artifacts"
# Shared across migrations: records of imported images and the group ID.
: "${LOCAL_STATE:=$REPO_ROOT/.state}"

export MIGRATION MIGRATION_DIR MIGRATION_ENV SANDBOX_ID_FILE ARTIFACTS_DIR SANDBOX_NAME SANDBOX_PLAN SANDBOX_GROUP \
  IMAGE_REPO IMAGE_PLATFORM IMAGE_NAME PG_MAJOR LOCAL_STATE
