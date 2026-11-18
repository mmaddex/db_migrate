# shellcheck shell=bash
#
# Sourced by every script in /opt/migrate. Sets PATH, directories and
# settings, then loads the helpers in lib.sh.
#
# Do not rely on image ENV, ENTRYPOINT or CMD: a Render sandbox may not apply
# them. Everything a script needs is set here.
#
# Keep this file bash 3.2 compatible: cleanup_source.sh may run on macOS.

MIGRATE_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MIGRATE_HOME

# Build-time facts (PG_MAJOR, BUCARDO_VERSION, IMAGE_REVISION). Missing when
# the scripts run from a git checkout on the operator machine.
if [ -r "$MIGRATE_HOME/image.env" ]; then
  # shellcheck disable=SC1091
  . "$MIGRATE_HOME/image.env"
fi
: "${PG_MAJOR:=17}"
: "${BUCARDO_VERSION:=5.6.0}"
: "${IMAGE_REVISION:=dev}"
export PG_MAJOR BUCARDO_VERSION IMAGE_REVISION

# Postgres binaries for the pinned major come first. Outside the image the
# directory does not exist and the system psql is used instead.
if [ -d "/usr/lib/postgresql/$PG_MAJOR/bin" ]; then
  PATH="/usr/lib/postgresql/$PG_MAJOR/bin:$PATH"
fi
PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

# Directories. WORK_DIR holds everything fetch_artifacts.sh copies out:
# artifacts (schema dumps, reports), logs, state markers and job output.
: "${WORK_DIR:=/var/lib/migrate}"
LOG_DIR="$WORK_DIR/logs"
ARTIFACT_DIR="$WORK_DIR/artifacts"
STATE_DIR="$WORK_DIR/state"
JOB_DIR="$WORK_DIR/jobs"
export WORK_DIR LOG_DIR ARTIFACT_DIR STATE_DIR JOB_DIR

# Local control cluster for Bucardo. Kept outside WORK_DIR so artifact
# downloads never include it.
: "${PGDATA_LOCAL:=/var/lib/postgresql/migrate-control}"
: "${PG_SOCKET_DIR:=/var/run/postgresql}"
: "${BUCARDO_PIDDIR:=/var/run/bucardo}"
: "${BUCARDORC:=/etc/bucardorc}"
: "${RUN_USER:=postgres}"
export PGDATA_LOCAL PG_SOCKET_DIR BUCARDO_PIDDIR BUCARDORC RUN_USER

# Connection details live in a libpq service file, so passwords never appear
# in argv, in Bucardo's tables or in output. lib.sh writes it from the URLs.
# It lives outside WORK_DIR so fetch_artifacts.sh never downloads it.
: "${SECRETS_DIR:=/var/lib/postgresql/.pg-migrate}"
PGSERVICEFILE="$SECRETS_DIR/pg_service.conf"
export SECRETS_DIR PGSERVICEFILE
SRC_SERVICE="service=heroku"
DST_SERVICE="service=render"

# Fallback for sandboxes where --env-file values do not reach exec sessions
# (see docs/spike.md): an env file copied to /etc/pg-migrate/migration.env.
: "${MIGRATE_ENV_FALLBACK_FILE:=/etc/pg-migrate/migration.env}"
if [ -z "${HEROKU_DATABASE_URL:-}" ] && [ -r "$MIGRATE_ENV_FALLBACK_FILE" ]; then
  MIGRATE_ENV_FALLBACK="$MIGRATE_ENV_FALLBACK_FILE"
fi

# exec sessions may start with umask 000; nothing we write needs to be
# world-writable. Secret files set their own stricter umask.
umask 022

# Migration settings (normally from migration.env via --env-file).
: "${SYNC_NAME:=migrate}"
: "${SCHEMAS:=public}"
: "${PGCONNECT_TIMEOUT:=15}"
: "${PGAPPNAME:=pg-migrate}"
: "${WATCHDOG_INTERVAL:=60}"
: "${LOG_ROTATE_MB:=200}"
: "${INDEX_MAINTENANCE_WORK_MEM:=512MB}"
: "${CUTOVER_KICK_TIMEOUT:=0}"
export SYNC_NAME SCHEMAS PGCONNECT_TIMEOUT PGAPPNAME WATCHDOG_INTERVAL \
  LOG_ROTATE_MB INDEX_MAINTENANCE_WORK_MEM CUTOVER_KICK_TIMEOUT

# Locale for tools; C.UTF-8 exists in Debian and macOS understands it.
LC_ALL="${LC_ALL:-C.UTF-8}"
export LC_ALL

# shellcheck source=sandbox/lib.sh
. "$MIGRATE_HOME/lib.sh"

if [ -n "${MIGRATE_ENV_FALLBACK:-}" ]; then
  load_env_file "$MIGRATE_ENV_FALLBACK"
fi
