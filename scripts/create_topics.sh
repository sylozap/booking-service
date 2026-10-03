#!/usr/bin/env bash
# Create the topics of the platform and their dead letter topics.
#
# Runs where kafka-topics is: inside the broker container. From the host,
# pipe it in rather than copying it:
#
#   kubectl exec -i -n barber-infra kafka-0 -- bash -s < scripts/create_topics.sh
#   docker compose -f deploy/compose/docker-compose.yml exec -T kafka bash -s < scripts/create_topics.sh
#
# Idempotent: a topic that exists is left alone, unless it has fewer
# partitions than it should -- then it gets more. Partitions are never taken
# away; Kafka cannot do that.
set -euo pipefail

BOOTSTRAP_SERVER=${BOOTSTRAP_SERVER:-localhost:9092}
PARTITIONS=${PARTITIONS:-3}
REPLICATION_FACTOR=${REPLICATION_FACTOR:-1}

# The topics of barber_common.events; tests/deploy/test_infra.py keeps the two
# lists equal. Every one of them is consumed, so every one has a dead letter
# topic next to it.
TOPICS=(
    auth.users.v1
    catalog.masters.v1
    catalog.services.v1
    booking.bookings.v1
    booking.reminders.v1
)
DLQ_SUFFIX=.dlq

topics() {
    kafka-topics --bootstrap-server "${BOOTSTRAP_SERVER}" "$@"
}

# The broker accepts connections a little before it answers metadata requests.
for attempt in $(seq 1 60); do
    if topics --list >/dev/null 2>&1; then
        break
    fi
    if [[ ${attempt} -eq 60 ]]; then
        echo "broker at ${BOOTSTRAP_SERVER} did not answer" >&2
        exit 1
    fi
    sleep 2
done

for base in "${TOPICS[@]}"; do
    for topic in "${base}" "${base}${DLQ_SUFFIX}"; do
        topics --create --if-not-exists --topic "${topic}" \
            --partitions "${PARTITIONS}" --replication-factor "${REPLICATION_FACTOR}" >/dev/null

        current=$(topics --describe --topic "${topic}" | grep -c "Partition: ")
        if ((current < PARTITIONS)); then
            topics --alter --topic "${topic}" --partitions "${PARTITIONS}" >/dev/null
            echo "${topic}: ${current} -> ${PARTITIONS} partitions"
        else
            echo "${topic}: ${current} partitions"
        fi
    done
done
