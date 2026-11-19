# shellcheck shell=bash
#
# Sourced by every script in /opt/migrate. Sets PATH, directories and
# settings, then loads the helpers in lib.sh.
#
# Do not rely on image ENV, ENTRYPOINT or CMD: a Render sandbox may not apply
# them. Everything a script needs is set here.
#
# Also sourced outside the image by test/unit_lib.sh.

MIGRATE_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MIGRATE_HOME
# Who the sandbox's exec ran as, before ensure_run_user drops to postgres.
: "${MIGRATE_EXEC_USER:=$(id -un 2>/dev/null || id -u)}"
export MIGRATE_EXEC_USER

# Build-time facts (PG_MAJOR, PG_CLIENT_MAJORS, BUCARDO_VERSION,
# IMAGE_REVISION). Missing when the helpers are loaded from a git checkout
# (tests).
if [ -r "$MIGRATE_HOME/image.env" ]; then
  # shellcheck disable=SC1091
  . "$MIGRATE_HOME/image.env"
fi
: "${PG_MAJOR:=17}"
: "${PG_CLIENT_MAJORS:=$PG_MAJOR}"
: "${BUCARDO_VERSION:=5.6.0}"
: "${IMAGE_REVISION:=dev}"
export PG_MAJOR PG_CLIENT_MAJORS BUCARDO_VERSION IMAGE_REVISION

# The control database's binaries come first, so psql is that major's. psql
# works against any server version; pg_dump does not, so schema.sh runs the
# one use_client_tools (lib.sh) picks. Outside the image the directory does
# not exist and the system psql is used instead.
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
# The user preflight tells people to create on Render when the role in
# RENDER_DATABASE_URL can't set session_replication_role. They pass it as
# RENDER_SUPERUSER_URL (optional): every step then logs in with that URL, and
# cutover gives what it created to the user in RENDER_DATABASE_URL.
: "${MIGRATION_SUPER_USER:=migration_super_user}"
: "${PGCONNECT_TIMEOUT:=15}"
: "${PGAPPNAME:=pg-migrate}"
: "${WATCHDOG_INTERVAL:=60}"
: "${LOG_ROTATE_MB:=200}"
: "${INDEX_MAINTENANCE_WORK_MEM:=512MB}"
: "${CUTOVER_KICK_TIMEOUT:=0}"
export SYNC_NAME SCHEMAS PGCONNECT_TIMEOUT PGAPPNAME WATCHDOG_INTERVAL \
  LOG_ROTATE_MB INDEX_MAINTENANCE_WORK_MEM CUTOVER_KICK_TIMEOUT

# Locale for tools; C.UTF-8 always exists in Debian.
LC_ALL="${LC_ALL:-C.UTF-8}"
export LC_ALL

# shellcheck source=sandbox/lib.sh
. "$MIGRATE_HOME/lib.sh"

if [ -n "${MIGRATE_ENV_FALLBACK:-}" ]; then
  load_env_file "$MIGRATE_ENV_FALLBACK"
fi
