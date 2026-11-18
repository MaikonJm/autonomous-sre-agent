#!/usr/bin/env bash
# Roda NO SEU COMPUTADOR, na raiz do repositório. Controla o laboratório na AWS.
#   ./scripts/lab.sh status    estado da EC2
#   ./scripts/lab.sh stop      para a EC2 (sem custo de computação; o disco continua, ~US$ 2,40/mês)
#   ./scripts/lab.sh start     liga de novo (~3 min até tudo voltar)
#   ./scripts/lab.sh connect   abre um terminal no nó (SSM)
#   ./scripts/lab.sh tunnels   painel do agente :8080, Grafana :3000, alertas :9090 no seu navegador
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform"
ID=$($TF output -raw instance_id)
REGION=$($TF output -raw region)
case "${1:-status}" in
  status)  aws ec2 describe-instances --region "$REGION" --instance-ids "$ID" \
             --query 'Reservations[0].Instances[0].State.Name' --output text ;;
  stop)    aws ec2 stop-instances --region "$REGION" --instance-ids "$ID" >/dev/null
           aws ec2 wait instance-stopped --region "$REGION" --instance-ids "$ID"; echo "parada" ;;
  start)   aws ec2 start-instances --region "$REGION" --instance-ids "$ID" >/dev/null
           aws ec2 wait instance-status-ok --region "$REGION" --instance-ids "$ID"
           echo "ligada; aguarde ~2 min para os pods voltarem (kubectl get pods -A no nó)" ;;
  connect) aws ssm start-session --region "$REGION" --target "$ID" ;;
  tunnels)
    trap 'kill 0' EXIT
    for p in 8080 3000 9090; do
      aws ssm start-session --region "$REGION" --target "$ID" --document-name AWS-StartPortForwardingSession \
        --parameters "{\"portNumber\":[\"$p\"],\"localPortNumber\":[\"$p\"]}" >/dev/null &
    done
    echo "No nó, rode antes: /opt/lab/scripts/node-forward.sh"
    echo "Agente: http://localhost:8080 | Grafana: http://localhost:3000 (admin/admin) | Alertas: http://localhost:9090/alerts"
    wait ;;
  *) echo "uso: $0 status|stop|start|connect|tunnels"; exit 1 ;;
esac
