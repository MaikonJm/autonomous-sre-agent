#!/usr/bin/env bash
# Roda NO NÓ. Prova a NetworkPolicy: o mesmo teste de dois namespaces diferentes.
#   loadtest -> orders-api : PERMITIDO (cliente autorizado)
#   default  -> orders-api : BLOQUEADO (qualquer outro pod)
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
probe() {
  local ns=$1 name="netpol-test-$RANDOM"
  kubectl -n "$ns" run "$name" --image=busybox:1.36 --restart=Never --command -- \
    sh -c 'wget -q -T 3 -O- http://orders-api.orders/healthz >/dev/null 2>&1 && echo PERMITIDO || echo BLOQUEADO' >/dev/null
  kubectl -n "$ns" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=90s >/dev/null
  printf '%-10s -> orders-api : %s\n' "$ns" "$(kubectl -n "$ns" logs "$name")"
  kubectl -n "$ns" delete pod "$name" --wait=false >/dev/null
}
probe loadtest
probe default
