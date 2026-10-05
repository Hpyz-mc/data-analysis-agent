#!/bin/sh
set -eu

# Run from the project root after `docker compose up -d --build`.
# This affects only the isolated Compose database, never local PostgreSQL.
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d agent_db \
    < examples/sql/sample_sales.sql
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d agent_db \
    -c 'GRANT SELECT ON public.agent_sales_sample TO agent_reader'
