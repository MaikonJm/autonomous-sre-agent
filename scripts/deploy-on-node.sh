#!/usr/bin/env bash
# Roda DENTRO do nó (automático no primeiro boot, ou manual: sudo /opt/lab/scripts/deploy-on-node.sh).
# Idempotente: pode rodar de novo para atualizar depois de um git pull.
#   1. build das imagens e import direto no k3s (sem registry)
#   2. Prometheus + Alertmanager + Grafana (kube-prometheus-stack)
#   3. orders-api + alertas de SLO + gerador de tráfego
#   4. agente SRE + autoteste (AWS, Bedrock, Kubernetes, Prometheus)
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export HOME="${HOME:-/root}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "==> 1/4 Imagens (docker build -> import no containerd do k3s)"
TAG="v-$(date +%Y%m%d%H%M)"
for app in orders-api sre-agent; do
  $SUDO docker build -q -t "$app:$TAG" "apps/$app"
  $SUDO docker save "$app:$TAG" | $SUDO k3s ctr images import - >/dev/null
done
$SUDO docker image prune -af >/dev/null

echo "==> 2/4 Observabilidade (~5 min)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f k8s/observability/values.yaml --wait --timeout 15m

echo "==> 3/4 Aplicação, alertas e tráfego"
sed "s#ORDERS_API_IMAGE#orders-api:$TAG#" k8s/orders/orders-api.yaml | kubectl apply -f -
kubectl apply -f k8s/orders/alerts.yaml
sed "s#ORDERS_API_IMAGE#orders-api:$TAG#" k8s/loadtest/loadgen.yaml | kubectl apply -f -
kubectl -n orders rollout status deploy/orders-api --timeout=300s

echo "==> 4/4 Agente SRE"
sed "s#SRE_AGENT_IMAGE#sre-agent:$TAG#" k8s/sre-agent/sre-agent.yaml | kubectl apply -f -
kubectl -n sre-agent rollout status deploy/sre-agent --timeout=300s
sleep 10
echo "==> Autoteste"
kubectl -n sre-agent exec deploy/sre-agent -- python -c \
  "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8080/selftest')),indent=2))"
echo "DEPLOY OK. Espere ~3 min de tráfego antes dos cenários: python3 scripts/run_scenarios.py"
