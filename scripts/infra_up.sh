#!/usr/bin/env bash
# Install the infrastructure of the platform into the current cluster.
#
# Creates the three namespaces, installs PostgreSQL, Redis and Kafka into
# barber-infra and waits for them, then creates the topics. The services and
# observability are installed separately: on a machine short of memory the
# namespaces come up one at a time.
#
# Idempotent: a second run upgrades the release to the same state and leaves
# existing topics alone.
set -euo pipefail

cd "$(dirname "$0")/.."

INFRA_NAMESPACE=barber-infra
NAMESPACES=(barber "${INFRA_NAMESPACE}" observability)

for namespace in "${NAMESPACES[@]}"; do
    kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

helm upgrade --install infra deploy/helm/infra \
    --namespace "${INFRA_NAMESPACE}" \
    --wait --timeout 5m

# --wait covers a Deployment, not the pods of a StatefulSet being Ready.
kubectl rollout status --namespace "${INFRA_NAMESPACE}" statefulset/postgres --timeout=3m
kubectl rollout status --namespace "${INFRA_NAMESPACE}" statefulset/kafka --timeout=5m

kubectl exec -i --namespace "${INFRA_NAMESPACE}" kafka-0 -- bash -s < scripts/create_topics.sh
