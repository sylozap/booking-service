#!/usr/bin/env bash
# Four databases and four roles, created once when the data directory is empty.
#
# Every service gets a role that can connect to its own database and to no
# other. The passwords come from the environment -- <SERVICE>_DB_PASSWORD --
# so the compose stack and the cluster run this same file: compose passes its
# throwaway passwords, the cluster takes them from a Secret.
#
# Run by the entrypoint of the postgres image from /docker-entrypoint-initdb.d,
# with POSTGRES_USER and POSTGRES_DB set.
set -euo pipefail

SERVICES=(auth catalog booking notification)

for service in "${SERVICES[@]}"; do
    variable="${service^^}_DB_PASSWORD"
    password="${!variable:?${variable} is not set}"

    # psql interpolates :"role" as an identifier and :'password' as a literal,
    # so neither needs quoting by hand.
    psql --no-psqlrc -v ON_ERROR_STOP=1 \
        --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB:-postgres}" \
        -v role="${service}" -v password="${password}" <<'SQL'
CREATE ROLE :"role" WITH LOGIN PASSWORD :'password';
CREATE DATABASE :"role" OWNER :"role";
-- PUBLIC may connect to any database by default, which would make the roles
-- decorative.
REVOKE ALL ON DATABASE :"role" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"role" TO :"role";
SQL
done
