#!/usr/bin/env bash
# Roda NO NÓ. Prova a verificação de assinatura com o mesmo comando que protege o deploy:
#   imagem publicada e assinada pelo pipeline  -> VÁLIDA
#   imagem pública qualquer, sem assinatura      -> REJEITADA
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
PROJECT=autonomous-sre-agent
# certificado raiz do AWS Signer no trust store "aws-signer-ts" (o instalador configura por usuário)
notation cert ls 2>/dev/null | grep -q aws-signer-ts || {
  curl -sSLo /tmp/aws-signer-notation-root.cert https://d2hvyiie56hcat.cloudfront.net/aws-signer-notation-root.cert
  notation cert add --type signingAuthority --store aws-signer-ts /tmp/aws-signer-notation-root.cert >/dev/null
}
cat > /tmp/trustpolicy.json <<JSON
{"version":"1.0","trustPolicies":[{"name":"$PROJECT","registryScopes":["*"],
  "signatureVerification":{"level":"strict"},"trustStores":["signingAuthority:aws-signer-ts"],
  "trustedIdentities":["$(aws ssm get-parameter --name "/$PROJECT/signing-profile-arn" --query Parameter.Value --output text)"]}]}
JSON
notation policy import --force /tmp/trustpolicy.json >/dev/null
REGISTRY=$(aws ssm get-parameter --name "/$PROJECT/ecr-registry" --query Parameter.Value --output text)
aws ecr get-login-password | notation login --username AWS --password-stdin "$REGISTRY" >/dev/null
DIGEST=$(aws ecr describe-images --repository-name "$PROJECT/orders-api" \
  --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageDigest' --output text)

echo "== 1) Imagem do pipeline (assinada pelo AWS Signer)"
notation verify "$REGISTRY/$PROJECT/orders-api@$DIGEST" && echo ">>> VÁLIDA: deploy permitido"
echo
echo "== 2) Imagem pública sem assinatura"
notation verify public.ecr.aws/docker/library/busybox:1.36 || echo ">>> REJEITADA: o deploy seria bloqueado"
