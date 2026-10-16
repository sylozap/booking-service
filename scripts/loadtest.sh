#!/usr/bin/env bash
# The load test against the platform in kind.
#
#   scripts/loadtest.sh users   register and confirm the clients of k6, sign them in
#   scripts/loadtest.sh run     sign them in again, run loadtest/k6/booking_flow.js
#
# "users" reaches auth through a port-forward, as the seed does, past the
# limit the gateway puts on registrations, and reads the confirmation letters
# from the log of notification. "run" starts k6 from its image, on the network
# of the host, so that it goes in through the Ingress as a browser would.
# RATE, RAMP, HOLD and DAYS_AHEAD change the profile; k6 options after "run"
# are passed on.
set -euo pipefail

cd "$(dirname "$0")/.."

NAMESPACE=barber
AUTH_PORT=${LOADTEST_AUTH_PORT:-18001}
K6_IMAGE=grafana/k6:1.3.0
K6_DIR=loadtest/k6

users() {
    kubectl port-forward --namespace "${NAMESPACE}" service/auth "${AUTH_PORT}:http" >/dev/null &
    FORWARD=$!
    trap 'kill ${FORWARD} 2>/dev/null || true' EXIT
    local ready=0
    for _ in $(seq 50); do
        if curl --silent --fail "http://localhost:${AUTH_PORT}/health/ready" >/dev/null; then
            ready=1
            break
        fi
        sleep 0.2
    done
    if [[ ${ready} == 0 ]]; then
        echo "auth does not answer on localhost:${AUTH_PORT} through the port-forward" >&2
        exit 1
    fi
    LOADTEST_AUTH_URL="http://localhost:${AUTH_PORT}" uv run python scripts/loadtest_users.py
    kill "${FORWARD}" 2>/dev/null || true
    trap - EXIT
}

run() {
    # Fresh tokens: an access token lives fifteen minutes.
    users
    mkdir -p "${K6_DIR}/results"
    local stamp
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    docker run --rm --network host \
        --user "$(id -u):$(id -g)" \
        --add-host barber.local:127.0.0.1 \
        --volume "${PWD}/${K6_DIR}:/scripts" --workdir /scripts \
        --env BASE_URL="${BASE_URL:-http://barber.local}" \
        --env RATE="${RATE:-50}" --env RAMP="${RAMP:-2m}" --env HOLD="${HOLD:-5m}" \
        --env DAYS_AHEAD="${DAYS_AHEAD:-13}" \
        --env SUMMARY="results/summary-${stamp}.json" \
        "${K6_IMAGE}" run "$@" booking_flow.js
}

case "${1:-}" in
    users) users ;;
    run) shift; run "$@" ;;
    *) echo "usage: $0 users|run [k6 options]" >&2; exit 2 ;;
esac
