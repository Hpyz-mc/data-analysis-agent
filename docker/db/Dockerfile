FROM postgres:17-bookworm

# Copy init scripts into the image. On macOS, executing a bind-mounted shell
# script can fail even when the file appears executable inside the container.
# PostgreSQL's entrypoint sources a non-executable .sh file on first init.
COPY --chmod=0644 docker/db/init/01_roles.sh /docker-entrypoint-initdb.d/01_roles.sh
COPY --chmod=0644 docker/db/session_schema.sql /docker-entrypoint-initdb.d/02_session_schema.sql
COPY --chmod=0644 docker/db/init/03_grants.sql /docker-entrypoint-initdb.d/03_grants.sql
