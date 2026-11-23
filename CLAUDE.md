# Contexto para o Claude Code

Responda sempre em **português do Brasil**.

Este repositório é um laboratório de SRE: um time de agentes de IA que resolve incidentes com guardrails, num k3s numa EC2, usando Amazon Bedrock (Nova Micro). Leia `README.md`, `docs/overview.md` e `docs/runbooks.md` antes de agir.

## Estado

- O laboratório está **desmontado** (`terraform destroy`). Para recriar, siga a seção "Subir" do README.
- O state do Terraform é local em `infra/terraform/` e não é versionado.
- O time de agentes é ligado por `ENABLE_REVIEWER` e `ENABLE_SCRIBE` no ConfigMap do agente; com `"false"` volta ao agente único.

## Regras

- **Peça confirmação antes de qualquer ação que gere custo, mude permissões ou seja destrutiva:** `terraform apply`, `destroy`, `lab.sh stop/start`, mudanças de IAM, apagar arquivos.
- **Custo mínimo:** nada de NAT Gateway, EKS, Load Balancer ou instância maior que o necessário.
- **Segurança:** nunca abra portas de entrada no Security Group; acesso só por SSM. Nunca imprima nem salve credenciais. Não faça commit de `terraform.tfstate`, `terraform.tfvars`, `tfplan` nem de `reports/`.
- `k8s/observability/dashboards.yaml` é gerado: edite `build_dashboards.py` e rode o script (o CI compara os dois).
- Se algo falhar, diagnostique pela causa (logs, `kubectl describe`, eventos) antes de tentar de novo, e explique em linguagem simples.
- Não faça `git push` sem o dono do projeto pedir.

## Rodar comandos no nó sem terminal interativo

```bash
ID=$(terraform -chdir=infra/terraform output -raw instance_id)
CMD=$(aws ssm send-command --region us-east-1 --instance-ids "$ID" --document-name AWS-RunShellScript \
  --parameters 'commands=["sudo tail -n 40 /var/log/sre-lab-deploy.log"]' --query Command.CommandId --output text)
sleep 5
aws ssm get-command-invocation --region us-east-1 --command-id "$CMD" --instance-id "$ID" \
  --query StandardOutputContent --output text
```

Para `kubectl` no nó, prefixe com `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml;`. Para comandos longos, rode em background no nó (`nohup ... > /opt/lab/reports/<nome>.log 2>&1 &`) e consulte o log. No PowerShell 5.1, JSON passado ao `aws` perde as aspas: use o Bash (Git Bash).
