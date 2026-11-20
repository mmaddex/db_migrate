#!/bin/bash
# Render-like target: the migration role owns the database but is not a
# superuser. GRANT_REPLICA_ROLE=yes gives it the one thing the migration needs
# (GRANT SET ON PARAMETER session_replication_role). That grant needs PG 15+;
# on PG 14 the only way is superuser, so the role gets that instead. With
# GRANT_REPLICA_ROLE=no, preflight creates migration_super_user as this role,
# as it does on Render, hence CREATEROLE.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
CREATE ROLE migrator LOGIN CREATEROLE PASSWORD 'migrator-pass';
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
