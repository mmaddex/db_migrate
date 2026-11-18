#!/bin/bash
# Render-like target: the migration role owns the database but is not a
# superuser. GRANT_REPLICA_ROLE=yes mirrors the production requirement
# (GRANT SET ON PARAMETER session_replication_role, PG 15+).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
CREATE ROLE migrator LOGIN PASSWORD 'migrator-pass';
CREATE DATABASE app OWNER migrator;
SQL
if [ "${GRANT_REPLICA_ROLE:-no}" = yes ]; then
  psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres \
    -c "GRANT SET ON PARAMETER session_replication_role TO migrator"
fi
