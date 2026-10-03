#!/usr/bin/env bash
# The whole platform in a local kind cluster, in one command: "make kind-up".
#
#   1. the cluster (deploy/kind/cluster.yaml), unless it exists;
#   2. Traefik as the Ingress controller, metrics-server for the autoscalers;
#   3. the images of the compose stack, built and loaded into the node;
#   4. the secrets and the infrastructure (scripts/infra_up.sh);
#   5. with OBSERVABILITY=1, Prometheus, Grafana, Tempo, Loki and Alloy
#      (scripts/obs_up.sh), and the services send them their traces;
#   6. the five services, each migrated by its Job before its pods change;
#   7. the demo data, by the seed Job of the gateway.
#
# Idempotent: every step is an "upgrade --install" or an "apply", so a second
# run on a live cluster brings it to the same state and breaks nothing. A
# rebuilt image is rolled out, an unchanged one is not.
#
# Needs docker, kind, kubectl, helm and uv; versions in docs/10-infrastructure.md.
set -euo pipefail

cd "$(dirname "$0")/.."

CLUSTER=barber
NAMESPACE=barber
OBSERVABILITY=${OBSERVABILITY:-0}
# Pinned: a chart that moves on its own between two runs of the same commit
# is a stand nobody can reproduce.
TRAEFIK_VERSION=41.6.1
METRICS_SERVER_VERSION=3.14.0
# The gateway last: it carries the seed Job, which needs the other four.
SERVICES=(auth catalog booking notification api-gateway)
VALUES=deploy/helm/values

started=${SECONDS}

step() {
    printf '\n==> %s (%ss)\n' "$*" "$((SECONDS - started))"
}

step "cluster ${CLUSTER}"
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER}"; then
    echo "exists, reused"
    if [[ -z $(docker port "${CLUSTER}-control-plane" 80 2>/dev/null) ]]; then
        echo "warning: the cluster maps no port 80 of the host, so barber.local will not answer;" >&2
        echo "         it was not created from deploy/kind/cluster.yaml -- 'make kind-down' first" >&2
    fi
else
    kind create cluster --config deploy/kind/cluster.yaml --wait 3m
fi
kubectl config use-context "kind-${CLUSTER}" >/dev/null

step "Traefik and metrics-server"
helm repo add traefik https://traefik.github.io/charts --force-update >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update >/dev/null
helm repo update traefik metrics-server >/dev/null
helm upgrade --install traefik traefik/traefik \
    --version "${TRAEFIK_VERSION}" \
    --namespace traefik --create-namespace \
    --values deploy/kind/traefik.yaml \
    --wait --timeout 5m
helm upgrade --install metrics-server metrics-server/metrics-server \
    --version "${METRICS_SERVER_VERSION}" \
    --namespace kube-system \
    --values deploy/kind/metrics-server.yaml \
    --wait --timeout 5m

step "images"
# The images of the compose stack, barber/<service>:local, which
# deploy/helm/values/local.yaml names. Never pulled: there is no registry
# behind them, so they are put into the node.
docker compose -f deploy/compose/docker-compose.yml build "${SERVICES[@]}"
for service in "${SERVICES[@]}"; do
    kind load docker-image "barber/${service}:local" --name "${CLUSTER}"
done

step "secrets and infrastructure"
./scripts/infra_up.sh

overlays=()
if [[ ${OBSERVABILITY} == 1 ]]; then
    step "observability"
    ./scripts/obs_up.sh
    overlays=(-f "${VALUES}/observability.yaml")
fi

for service in "${SERVICES[@]}"; do
    step "${service}"
    extra=()
    if [[ ${service} == api-gateway ]]; then
        extra=(--set seed.enabled=true --set-file seed.script=scripts/seed.py)
    fi
    # The tag stays "local" across builds, so the id of the image is what
    # tells a new build from the old one: put on the pods, it rolls them out
    # when the code changed and leaves them alone when it did not.
    image_id=$(docker image inspect --format '{{.Id}}' "barber/${service}:local")
    helm upgrade --install "${service}" deploy/helm/service \
        --namespace "${NAMESPACE}" \
        -f "${VALUES}/common.yaml" -f "${VALUES}/${service}.yaml" -f "${VALUES}/local.yaml" \
        "${overlays[@]}" \
        --set-string "podAnnotations.barber\.local/image-id=${image_id#sha256:}" \
        "${extra[@]}" \
        --wait --timeout 15m
done

step "check"
# Through the Ingress, as a browser would: the host name resolved to the
# address kind listens on, whatever /etc/hosts says.
if curl --silent --fail --max-time 10 --resolve barber.local:80:127.0.0.1 \
    http://barber.local/health/ready >/dev/null; then
    echo "http://barber.local answers"
else
    echo "warning: http://barber.local does not answer through the Ingress" >&2
fi

printf '\nDone in %ss.\n' "$((SECONDS - started))"
grep -q "barber.local" /etc/hosts 2>/dev/null \
    || echo "Add '127.0.0.1 barber.local' to /etc/hosts to open http://barber.local in a browser."
if [[ ${OBSERVABILITY} == 1 ]]; then
    echo "Grafana: make kind-grafana, then http://localhost:3000"
fi
