#!/usr/bin/env bash
# Roda NO SEU COMPUTADOR (Git Bash/WSL/macOS/Linux), na raiz do repositório.
# Envia o código da branch atual para /opt/lab no nó pelo SSM, sem credencial do GitHub no nó
# (o repositório é privado). Depois: sudo /opt/lab/scripts/deploy-on-node.sh no nó.
set -euo pipefail
cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform"
ID=$($TF output -raw instance_id)
REGION=$($TF output -raw region)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# No Git Bash o aws.exe é um binário Windows e não enxerga /tmp/...: converte o caminho.
PARAMS="$WORK/params.json"
command -v cygpath >/dev/null && PARAMS_URI="file://$(cygpath -m "$PARAMS")" || PARAMS_URI="file://$PARAMS"

git bundle create "$WORK/lab.bundle" HEAD main >/dev/null 2>&1
base64 "$WORK/lab.bundle" | tr -d '\n' > "$WORK/lab.b64"
SHA=$(sha256sum "$WORK/lab.bundle" | cut -d' ' -f1)
split -b 12000 "$WORK/lab.b64" "$WORK/chunk."

run() {  # $1 = comando shell a executar no nó como root; espera terminar e imprime a saída
  printf '{"commands":["%s"]}' "$1" > "$PARAMS"
  local cmd
  cmd=$(aws ssm send-command --region "$REGION" --instance-ids "$ID" --document-name AWS-RunShellScript \
        --parameters "$PARAMS_URI" --query Command.CommandId --output text)
  aws ssm wait command-executed --region "$REGION" --command-id "$cmd" --instance-id "$ID" 2>/dev/null || true
  aws ssm get-command-invocation --region "$REGION" --command-id "$cmd" --instance-id "$ID" \
    --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
}

echo "Enviando $(ls "$WORK"/chunk.* | wc -l) partes para $ID..."
run "rm -f /tmp/lab.b64" >/dev/null
for c in "$WORK"/chunk.*; do
  run "printf %s $(cat "$c") >> /tmp/lab.b64" >/dev/null
done
run "base64 -d /tmp/lab.b64 > /tmp/lab.bundle && echo \\\"$SHA  /tmp/lab.bundle\\\" | sha256sum -c && git -C /opt/lab fetch -q /tmp/lab.bundle main && git -C /opt/lab reset -q --hard FETCH_HEAD && git -C /opt/lab log --oneline -1 && chmod -R a+rwX /opt/lab && rm -f /tmp/lab.b64 /tmp/lab.bundle"
echo "Código no nó atualizado. Próximo: sudo /opt/lab/scripts/deploy-on-node.sh (no nó)"
