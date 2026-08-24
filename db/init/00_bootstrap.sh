#!/bin/bash
# Runs once, on first creation of the cluster. The Postgres entrypoint executes files placed
# directly in /docker-entrypoint-initdb.d; the ordered .sql files live in the ./sql subdirectory
# so that this script controls their order and can pass credentials in as psql variables
# instead of hard-coding them.
set -euo pipefail

SQL_DIR="/docker-entrypoint-initdb.d/sql"

: "${ANALYST_RO_USER:?ANALYST_RO_USER must be set}"
: "${ANALYST_RO_PASSWORD:?ANALYST_RO_PASSWORD must be set}"
STATEMENT_TIMEOUT_MS="${SQL_STATEMENT_TIMEOUT_MS:-15000}"
IDLE_TX_TIMEOUT_MS="${SQL_IDLE_TX_TIMEOUT_MS:-30000}"

run() {
  echo "[bootstrap] applying $(basename "$1")"
  psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
       --set ON_ERROR_STOP=1 --no-psqlrc --quiet \
       --set ro_user="$ANALYST_RO_USER" \
       --set ro_password="$ANALYST_RO_PASSWORD" \
       --set statement_timeout="$STATEMENT_TIMEOUT_MS" \
       --set idle_tx_timeout="$IDLE_TX_TIMEOUT_MS" \
       --file "$1"
}

run "$SQL_DIR/01_roles.sql"
run "$SQL_DIR/02_schema.sql"
run "$SQL_DIR/03_grants.sql"

echo "[bootstrap] done: schemas analytics + agent created, ${ANALYST_RO_USER} is read-only"
