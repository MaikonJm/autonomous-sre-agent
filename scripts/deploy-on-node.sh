#!/usr/bin/env bash
# Roda DENTRO do nó (automático no primeiro boot, ou manual: sudo /opt/lab/scripts/deploy-on-node.sh).
# Idempotente: pode rodar de novo para atualizar depois de um git pull.
#   1. imagens: do ECR com assinatura VERIFICADA (padrão quando o pipeline já publicou) ou build local
#   2. Prometheus + Alertmanager + Grafana (kube-prometheus-stack)
#   3. orders-api + alertas de SLO + gerador de tráfego
#   4. agente SRE + autoteste (AWS, Bedrock, Kubernetes, Prometheus)
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export HOME="${HOME:-/root}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

REGION="${AWS_REGION:-us-east-1}"; export AWS_DEFAULT_REGION="$REGION"
PROJECT=autonomous-sre-agent

ensure_awscli() {
  command -v aws >/dev/null && return
  $SUDO apt-get install -y -qq unzip >/dev/null
  curl -sSLo /tmp/awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp && $SUDO /tmp/aws/install --update >/dev/null && rm -rf /tmp/aws /tmp/awscliv2.zip
}
ensure_notation() {
  command -v notation >/dev/null && return
  curl -sSLo /tmp/signer.deb https://d2hvyiie56hcat.cloudfront.net/linux/amd64/installer/deb/latest/aws-signer-notation-cli_amd64.deb
  $SUDO dpkg -i -E /tmp/signer.deb >/dev/null && rm -f /tmp/signer.deb
}

# Modo de imagens: "ecr" (assinadas pelo pipeline e VERIFICADAS aqui) ou "local" (build no nó).
# IMAGE_SOURCE=auto escolhe ecr se o pipeline de release já publicou imagens.
IMAGE_SOURCE="${IMAGE_SOURCE:-auto}"
ensure_awscli
REGISTRY=$(aws ssm get-parameter --name "/$PROJECT/ecr-registry" --query Parameter.Value --output text 2>/dev/null || true)
PROFILE=$(aws ssm get-parameter --name "/$PROJECT/signing-profile-arn" --query Parameter.Value --output text 2>/dev/null || true)
ECR_TAG=""
if [ -n "$REGISTRY" ] && [ "$IMAGE_SOURCE" != "local" ]; then
  ECR_TAG=$(aws ecr describe-images --repository-name "$PROJECT/orders-api" \
    --query "sort_by(imageDetails[?imageTags != null && !starts_with(imageTags[0], 'sha256-')], &imagePushedAt)[-1].imageTags[0]" \
    --output text 2>/dev/null || true)
  # ignora as assinaturas do Notation: o índice de referrers ganha a tag sha256-<digest> e é o último push
  [ "$ECR_TAG" = "None" ] && ECR_TAG=""
fi
[ "$IMAGE_SOURCE" = "auto" ] && { [ -n "$ECR_TAG" ] && IMAGE_SOURCE=ecr || IMAGE_SOURCE=local; }

if [ "$IMAGE_SOURCE" = "ecr" ]; then
  echo "==> 1/4 Imagens do ECR (tag $ECR_TAG): VERIFICANDO ASSINATURA antes do deploy"
  [ -n "$ECR_TAG" ] && [ -n "$PROFILE" ] || { echo "ECR sem imagens publicadas ou sem signing profile"; exit 1; }
  ensure_notation
  # certificado raiz do AWS Signer no trust store "aws-signer-ts" (o instalador configura por usuário)
  notation cert ls 2>/dev/null | grep -q aws-signer-ts || {
    curl -sSLo /tmp/aws-signer-notation-root.cert https://d2hvyiie56hcat.cloudfront.net/aws-signer-notation-root.cert
    notation cert add --type signingAuthority --store aws-signer-ts /tmp/aws-signer-notation-root.cert >/dev/null
  }
  cat > /tmp/trustpolicy.json <<JSON
{"version":"1.0","trustPolicies":[{"name":"$PROJECT","registryScopes":["*"],
  "signatureVerification":{"level":"strict"},"trustStores":["signingAuthority:aws-signer-ts"],
  "trustedIdentities":["$PROFILE"]}]}
JSON
  notation policy import --force /tmp/trustpolicy.json >/dev/null
  # notation login exige credential store seguro (o nó não tem): credencial só no ambiente
  export NOTATION_USERNAME=AWS NOTATION_PASSWORD="$(aws ecr get-login-password)"
  aws ecr get-login-password | $SUDO docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
  TAG="$ECR_TAG"
  for app in orders-api sre-agent; do
    DIGEST=$(aws ecr describe-images --repository-name "$PROJECT/$app" --image-ids imageTag="$TAG" \
               --query 'imageDetails[0].imageDigest' --output text)
    REF="$REGISTRY/$PROJECT/$app@$DIGEST"
    if ! notation verify "$REF"; then
      echo "DEPLOY BLOQUEADO: $app não tem assinatura válida do signing profile $PROFILE"; exit 1
    fi
    $SUDO docker pull -q "$REF" >/dev/null
    $SUDO docker tag "$REF" "$app:$TAG"
    $SUDO docker save "$app:$TAG" | $SUDO k3s ctr images import - >/dev/null
  done
else
  echo "==> 1/4 Imagens: build local no nó (o pipeline de release ainda não publicou imagens assinadas)"
  TAG="v-$(date +%Y%m%d%H%M)"
  for app in orders-api sre-agent; do
    $SUDO docker build -q -t "$app:$TAG" "apps/$app"
    $SUDO docker save "$app:$TAG" | $SUDO k3s ctr images import - >/dev/null
  done
fi
$SUDO docker image prune -af >/dev/null

echo "==> 2/4 Observabilidade (~5 min)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f k8s/observability/values.yaml --wait --timeout 15m
kubectl apply -f k8s/observability/dashboards.yaml     # dashboards como código (sidecar do Grafana)

echo "==> 3/4 Aplicação, alertas e tráfego"
sed "s#ORDERS_API_IMAGE#orders-api:$TAG#" k8s/orders/orders-api.yaml | kubectl apply -f -
kubectl apply -f k8s/orders/alerts.yaml
kubectl apply -f k8s/orders/networkpolicy.yaml          # firewall entre serviços
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
