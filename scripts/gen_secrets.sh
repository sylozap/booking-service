#!/usr/bin/env bash
# Generate the secrets of the platform straight into the current cluster.
#
#   scripts/gen_secrets.sh            # create what is missing, keep the rest
#   scripts/gen_secrets.sh --rotate   # new values for everything
#
# Nothing is written to disk: every value is generated in memory, encoded and
# piped into "kubectl apply". The repository keeps only the names of the keys
# (deploy/helm/values), and tests/deploy/test_secrets.py holds this script to
# them: every key a release reads is a key created here.
#
# Idempotent: a value that already exists in the cluster is read back and kept.
# A second run therefore changes nothing -- which matters, because a new
# signing key strands every token issued, and a new database password locks
# the services out of a database initialised with the old one. --rotate is for
# a stand about to be rebuilt: PostgreSQL takes new passwords only when its
# data directory is created again.
#
# TELEGRAM_BOT_TOKEN, when set in the environment, replaces the stored token.
set -euo pipefail

cd "$(dirname "$0")/.."

NAMESPACE=${NAMESPACE:-barber}
INFRA_NAMESPACE=${INFRA_NAMESPACE:-barber-infra}
POSTGRES_HOST=postgres.${INFRA_NAMESPACE}.svc.cluster.local

ROTATE=false
case "${1:-}" in
    "") ;;
    --rotate) ROTATE=true ;;
    *)
        echo "usage: $0 [--rotate]" >&2
        exit 2
        ;;
esac

# 24 random bytes as hex: safe inside a DSN, a JSON string and a shell word.
random_secret() {
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

# A value of a key of a Secret, or nothing when either is absent.
stored() {
    local namespace=$1 secret=$2 key=$3 encoded
    encoded=$(kubectl get secret "${secret}" --namespace "${namespace}" \
        -o "jsonpath={.data.${key//./\\.}}" 2>/dev/null) || return 0
    printf '%s' "${encoded}" | base64 -d
}

# The stored value of a key, or a fresh one when there is none or --rotate.
# Called with the command that makes a fresh value.
kept_or_new() {
    local namespace=$1 secret=$2 key=$3
    shift 3
    local value=""
    if [[ ${ROTATE} == false ]]; then
        value=$(stored "${namespace}" "${secret}" "${key}")
    fi
    if [[ -z ${value} ]]; then
        value=$("$@")
    fi
    printf '%s' "${value}"
}

# Apply a Secret from KEY VALUE pairs. The values travel base64 encoded on the
# standard input of kubectl -- never on a command line, where any process of
# the machine could read them. Server-side apply, so the Secret carries no
# last-applied-configuration annotation with a second copy of the values.
apply_secret() {
    local namespace=$1 name=$2
    shift 2
    {
        printf 'apiVersion: v1\nkind: Secret\ntype: Opaque\n'
        printf 'metadata:\n  name: %s\n  namespace: %s\n' "${name}" "${namespace}"
        printf '  labels:\n    app.kubernetes.io/part-of: barber\n'
        printf '    app.kubernetes.io/managed-by: gen_secrets\n'
        printf 'data:\n'
        while (($# > 0)); do
            printf '  %s: "%s"\n' "$1" "$(printf '%s' "$2" | base64 | tr -d '\n')"
            shift 2
        done
    } | kubectl apply --server-side --field-manager=gen_secrets --force-conflicts -f - >/dev/null
    echo "secret ${namespace}/${name}"
}

dsn() {
    local role=$1 password=$2
    printf 'postgresql+asyncpg://%s:%s@%s:5432/%s' "${role}" "${password}" "${POSTGRES_HOST}" "${role}"
}

signing_key() {
    # The generator of the local stack: one place decides the size and format
    # of the key. The private half goes to stdout; the kid and the public half
    # go to stderr, which is where they are wanted.
    uv run --quiet python scripts/gen_keys.py 2>/dev/null
}

for namespace in "${NAMESPACE}" "${INFRA_NAMESPACE}"; do
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "${namespace}" \
        | kubectl apply --server-side --field-manager=gen_secrets -f - >/dev/null
done

# --- PostgreSQL: the superuser and one role per service ----------------------
# Read by the infra chart (postgres.existingSecret) and by the init script.

declare -A DB_PASSWORDS
for role in postgres auth catalog booking notification; do
    key=POSTGRES_PASSWORD
    [[ ${role} != postgres ]] && key="${role^^}_DB_PASSWORD"
    DB_PASSWORDS[${role}]=$(kept_or_new "${INFRA_NAMESPACE}" postgres-credentials "${key}" random_secret)
done

apply_secret "${INFRA_NAMESPACE}" postgres-credentials \
    POSTGRES_PASSWORD "${DB_PASSWORDS[postgres]}" \
    AUTH_DB_PASSWORD "${DB_PASSWORDS[auth]}" \
    CATALOG_DB_PASSWORD "${DB_PASSWORDS[catalog]}" \
    BOOKING_DB_PASSWORD "${DB_PASSWORDS[booking]}" \
    NOTIFICATION_DB_PASSWORD "${DB_PASSWORDS[notification]}"

# --- the services --------------------------------------------------------------

# The secret booking exchanges for a service token: stored on the side of
# booking, and registered in auth under the same value.
booking_client_secret=$(kept_or_new "${NAMESPACE}" booking-env SERVICE_CLIENT_SECRET random_secret)

apply_secret "${NAMESPACE}" auth-env \
    DATABASE_DSN "$(dsn auth "${DB_PASSWORDS[auth]}")" \
    SERVICE_CLIENTS "{\"booking\": {\"secret\": \"${booking_client_secret}\", \"scopes\": [\"catalog:read\"]}}"

private_key=$(kept_or_new "${NAMESPACE}" auth-signing-key private.pem signing_key)
apply_secret "${NAMESPACE}" auth-signing-key private.pem "${private_key}"
unset private_key

apply_secret "${NAMESPACE}" catalog-env \
    DATABASE_DSN "$(dsn catalog "${DB_PASSWORDS[catalog]}")"

apply_secret "${NAMESPACE}" booking-env \
    DATABASE_DSN "$(dsn booking "${DB_PASSWORDS[booking]}")" \
    SERVICE_CLIENT_SECRET "${booking_client_secret}"

# Empty without a bot: notification then writes the telegram channel to its
# log. Kept across runs unless the environment names a token.
telegram_token=${TELEGRAM_BOT_TOKEN:-$(stored "${NAMESPACE}" notification-env TELEGRAM_BOT_TOKEN)}
apply_secret "${NAMESPACE}" notification-env \
    DATABASE_DSN "$(dsn notification "${DB_PASSWORDS[notification]}")" \
    TELEGRAM_BOT_TOKEN "${telegram_token}"
