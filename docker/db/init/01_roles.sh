#!/bin/sh

# This runs only when the isolated Docker PostgreSQL volume is first created.
# Passwords are URL-safe tokens supplied through the existing project .env.
# The official entrypoint sources non-executable .sh files and already uses
# error-exit mode; do not change its shell options here.
psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    -v reader_password="$DOCKER_READER_PASSWORD" \
    -v session_password="$DOCKER_SESSION_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE agent_reader LOGIN PASSWORD %L', :'reader_password')
\gexec
SELECT format('CREATE ROLE agent_session_writer LOGIN PASSWORD %L', :'session_password')
\gexec
SQL
