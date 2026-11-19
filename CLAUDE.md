# Contexto para o Claude Code

Responda sempre em **português do Brasil**. O dono do projeto é o Maikon, que se prepara para a entrevista técnica de **DevOps/SRE na Verity**, remarcada para **segunda-feira, 23/11, às 17h** (horário de Brasília). A vaga é para cuidar sozinho de DevOps e observabilidade do Verity Quantum, a plataforma de agentes multi-LLM da empresa.

Este repositório é a demo da entrevista: um agente SRE autônomo com guardrails num k3s numa EC2, usando Amazon Bedrock (Nova Micro). Leia `README.md`, `docs/overview.md` e `docs/runbooks.md` antes de agir.

## Estado atual

- A infra principal (`infra/terraform`) **já existe** na AWS e a EC2 fica **parada** entre sessões. O state é local em `infra/terraform/`.
- O lab já rodou os 5 cenários: 5 de 5 resolvidos (relatório em `reports/`, que não é versionado).
- O repositório é **privado**: o nó não consegue `git pull`. Para atualizar o código no nó, use `./scripts/sync-code-to-node.sh` (envia por SSM).
- O usuário `maikon-cli` ainda tem AdministratorAccess (passo 6 resolve).

## Plano da próxima sessão: aplicar as melhorias

Cada passo: mostre o resultado em poucas linhas e espere o "ok" antes dos que geram custo ou mudam permissões.

1. **Ligar:** `git pull` e `./scripts/lab.sh start` (pedir confirmação). Esperar ~3 min.
2. **Infra nova (supply chain):** rode `aws iam list-open-id-connect-providers`. Se já existir o provider `token.actions.githubusercontent.com`, use `create_github_oidc_provider=false` (padrão); senão, `true`. Rode `terraform plan` em `infra/terraform` com `-var repo_url=https://github.com/MaikonJm/autonomous-sre-agent.git -var alert_email=maikonjm@gmail.com`.
   - **Esperado:** cerca de 10 recursos novos (2 ECR, 2 lifecycle, signing profile, role e policy do GitHub, policy do nó, 2 parâmetros SSM e, talvez, o provider OIDC). **0 para destruir.**
   - **Se o plano quiser substituir ou destruir a `aws_instance`, PARE e explique.** Não pode acontecer.
   - Mostre o resumo, peça confirmação e faça o `apply`.
3. **Pipeline de release:** configure as variáveis do repositório com `gh variable set AWS_RELEASE_ROLE_ARN` e `gh variable set SIGNING_PROFILE_ARN`, usando os outputs do Terraform. Se o `gh` não estiver logado, mostre ao Maikon onde configurar: GitHub → Settings → Secrets and variables → Actions → Variables. Dispare com `gh workflow run release.yml` (ou pela aba Actions) e acompanhe: testes → build → push → `notation sign` → `notation verify`.
4. **Atualizar o nó:** `./scripts/sync-code-to-node.sh`, depois `sudo /opt/lab/scripts/deploy-on-node.sh` no nó (via send-command, em background com log, porque leva alguns minutos). O passo 1 do deploy deve dizer **"Imagens do ECR … VERIFICANDO ASSINATURA"**. Confira o autoteste.
5. **Validar as demos novas** (no nó):
   - `/opt/lab/scripts/signature-demo.sh`: imagem do pipeline **VÁLIDA**, busybox público **REJEITADA**.
   - `/opt/lab/scripts/netpol-demo.sh`: loadtest **PERMITIDO**, default **BLOQUEADO**. Se o orders-api ficar não-pronto depois da NetworkPolicy, as probes estão sendo bloqueadas: diagnostique antes de remover qualquer coisa.
   - Dashboards: peça ao Maikon para rodar `/opt/lab/scripts/node-forward.sh` no nó (sessão `lab.sh connect`) e `./scripts/lab.sh tunnels` no computador. No Grafana (http://localhost:3000, admin/admin) devem aparecer "orders-api: SLO e golden signals" e "Agente SRE e LLM".
   - Rode um cenário (`python3 scripts/run_scenarios.py api-errors`) e confira se o dashboard do agente mostra tokens, custo e a anotação da ação.
6. **IAM de menor privilégio** (só depois de tudo acima funcionar): `infra/operator-iam` → `terraform init` + `plan` + `apply` (confirmar). Depois o Maikon remove a AdministratorAccess do `maikon-cli` no console. Valide com `terraform plan` na stack principal, `./scripts/lab.sh status` e um `send-command` simples. Se algo for negado, mostre a ação exata e proponha a correção mínima na policy. Se travar, o Maikon reanexa a AdministratorAccess e seguimos.
7. **Desligar:** `./scripts/lab.sh stop` (confirmar).

## Regras

- **Peça confirmação antes de qualquer ação que gere custo, mude permissões ou seja destrutiva:** `terraform apply`, `destroy`, `lab.sh stop/start`, mudanças de IAM, apagar arquivos.
- **Custo mínimo:** nada de NAT Gateway, EKS, Load Balancer ou instância maior que o necessário.
- **Segurança:** nunca abra portas de entrada no Security Group; acesso só por SSM. Nunca imprima nem salve credenciais. Não faça commit de `terraform.tfstate`, `terraform.tfvars`, `tfplan` nem de `reports/`.
- Não apague `infra/terraform/` (o state está lá).
- Se algo falhar, diagnostique pela causa (logs, `kubectl describe`, eventos) antes de tentar de novo, e explique em linguagem simples: o Maikon está treinando para explicar isso na entrevista.
- Não faça `git push` sem o Maikon pedir.

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
