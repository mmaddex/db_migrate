#!/bin/bash
# Render-like target: the migration role owns the database but is not a
# superuser. GRANT_REPLICA_ROLE=yes mirrors the production requirement
# (GRANT SET ON PARAMETER session_replication_role). That grant needs PG 15+;
# on PG 14 the only way is superuser, so the role gets that instead.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
CREATE ROLE migrator LOGIN PASSWORD 'migrator-pass';
CREATE DATABASE app OWNER migrator;
SQL
if [ "${GRANT_REPLICA_ROLE:-no}" = yes ]; then
  if [ "$(psql -At --username postgres --dbname postgres -c 'SHOW server_version_num')" -ge 150000 ]; then
    sql="GRANT SET ON PARAMETER session_replication_role TO migrator"
  else
    sql="ALTER ROLE migrator SUPERUSER"
  fi
  psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres -c "$sql"
fi
