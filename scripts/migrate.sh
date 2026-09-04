#!/usr/bin/env bash
# Apply the migrations of every service that owns a schema.
#
# Runs against whatever DATABASE_DSN of each service points at: locally the
# compose stack, in a cluster the Helm pre-upgrade Job. Stops at the first
# failure -- a half-migrated set of databases is worse than a stopped script.
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=(auth catalog booking notification)
COMMAND=${1:-upgrade}
REVISION=${2:-head}

for service in "${SERVICES[@]}"; do
    config="services/${service}/alembic.ini"
    if [[ ! -f ${config} ]]; then
        echo "skipping ${service}: no ${config}"
        continue
    fi

    # Each service has its own database and its own role, so each one gets its
    # own DSN. The variable is named after the service; the plain DATABASE_DSN
    # is the fallback for a single-database local run.
    variable="$(echo "${service}" | tr '[:lower:]-' '[:upper:]_')_DATABASE_DSN"
    dsn="${!variable:-${DATABASE_DSN:-}}"
    if [[ -z ${dsn} ]]; then
        echo "${variable} is not set and DATABASE_DSN is empty" >&2
        exit 1
    fi

    echo "==> ${service}: alembic ${COMMAND} ${REVISION}"
    DATABASE_DSN="${dsn}" uv run alembic -c "${config}" "${COMMAND}" "${REVISION}"
done
