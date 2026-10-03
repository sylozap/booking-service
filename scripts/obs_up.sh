#!/usr/bin/env bash
# Install the observability namespace into the current cluster.
#
# kube-prometheus-stack (the operator, Prometheus, Alertmanager, Grafana), then
# deploy/helm/observability: Tempo, Loki, Alloy, and the alerting rules,
# dashboards and routing of deploy/observability. The services pick it up on
# their next upgrade: the ServiceMonitor of the service chart renders only
# where the operator is, and tracing is turned on by
# deploy/helm/values/observability.yaml.
#
# Separate from the platform: on a machine short of memory it is simply not
# installed. Idempotent: a second run upgrades the releases to the same state.
set -euo pipefail

cd "$(dirname "$0")/.."

NAMESPACE=observability
# Pinned: a chart that moves on its own between two runs of the same commit
# is a stand nobody can reproduce.
KUBE_PROMETHEUS_STACK_VERSION=${KUBE_PROMETHEUS_STACK_VERSION:-91.9.0}

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo update prometheus-community >/dev/null

# The operator first: the rules of the platform are a PrometheusRule, which
# exists only once its CRD does. Alertmanager waits for its routing Secret,
# which the second release creates; helm does not wait for Alertmanager, whose
# pods the operator makes, not the chart.
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
    --version "${KUBE_PROMETHEUS_STACK_VERSION}" \
    --namespace "${NAMESPACE}" \
    --values deploy/helm/observability/kube-prometheus-stack.yaml \
    --wait --timeout 10m

helm upgrade --install observability deploy/helm/observability \
    --namespace "${NAMESPACE}" \
    --wait --timeout 5m 2> >(grep -v "found symbolic link" >&2)

echo "Grafana: make kind-grafana, then http://localhost:3000"
