#!/bin/sh
set -eu

# Re-apply idempotent session DDL and grants on an existing Compose volume.
# The first-start /docker-entrypoint-initdb.d scripts do not run again.
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d agent_db \
    < docker/db/session_schema.sql
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d agent_db \
    < docker/db/init/03_grants.sql
