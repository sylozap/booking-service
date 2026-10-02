#!/usr/bin/env bash
# Check that each service role reaches its own database and no other.
#
# Connects as every role to every database over TCP, with the role's own
# password, from inside the postgres pod of the cluster. Its own database must
# answer; any other must refuse. Exits non-zero on the first surprise.
set -euo pipefail

NAMESPACE=${NAMESPACE:-barber-infra}
POD=${POD:-postgres-0}
SERVICES=(auth catalog booking notification)

status=0
for role in "${SERVICES[@]}"; do
    password_variable="${role^^}_DB_PASSWORD"
    for database in "${SERVICES[@]}"; do
        # The password is read inside the pod, from the Secret it already has,
        # rather than passed on the command line of kubectl.
        if kubectl exec --namespace "${NAMESPACE}" "${POD}" -- \
            sh -c "PGPASSWORD=\"\${${password_variable}}\" psql --no-psqlrc -h 127.0.0.1 -U ${role} -d ${database} -tAc 'select 1'" \
            >/dev/null 2>&1; then
            outcome=connected
        else
            outcome=refused
        fi

        expected=refused
        [[ ${role} == "${database}" ]] && expected=connected

        if [[ ${outcome} == "${expected}" ]]; then
            echo "ok    ${role} -> ${database}: ${outcome}"
        else
            echo "FAIL  ${role} -> ${database}: ${outcome}, expected ${expected}" >&2
            status=1
        fi
    done
done

exit "${status}"
