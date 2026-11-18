#!/usr/bin/env bash
# Roda NO NÓ: publica painel do agente, Grafana e Prometheus em localhost do nó,
# para o seu computador alcançar via túnel SSM (./scripts/lab.sh tunnels). Nada fica exposto na internet.
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
trap 'kill 0' EXIT
kubectl -n sre-agent port-forward svc/sre-agent 8080:8080 >/dev/null &
kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null &
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null &
echo "Encaminhando 8080 (agente), 3000 (Grafana), 9090 (Prometheus). Deixe esta sessão aberta."
wait
