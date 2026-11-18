# Autonomous SRE Agent: um agente de IA que resolve incidentes sozinho, com guardrails

Evolução do [aws-sre-ai-platform](https://github.com/MaikonJm/aws-sre-ai-platform). Lá, o agente **diagnosticava** e um humano aplicava a correção. Aqui ele **fecha o ciclo**: diagnostica, escolhe uma ação, executa, verifica se o SLO voltou, tenta de novo ou desfaz se piorar, e escreve o relatório. Tudo dentro de guardrails escritos em código, não em prompt.

```
 falha ─▶ Prometheus (SLO) ─▶ Alertmanager ─webhook─▶ Agente SRE (pod no k3s)
                                                          │
  1. Evidências (só leitura): deployments, revisões, pods, eventos, logs, SLIs
  2. Sinais determinísticos (código): "o que mudou?", OOMKilled, CrashLoop, 0 réplicas
  3. Plano (LLM: Amazon Bedrock Nova Micro, credencial temporária da role do nó)
       └─ UMA ação tipada: rollback | remove_env | scale | set_memory_limit | restart | escalate
  4. Política (código): execute | approval_required | escalate | dry_run
  5. Ação (snapshot antes) ─▶ 6. Verificação (rollout + SLIs de 1 min)
       ok ─▶ RESOLVIDO   piorou ─▶ restaura snapshot   não resolveu ─▶ 2ª tentativa ─▶ escala
  7. Relatório: painel web, Markdown, issue no GitHub (opcional), métricas do próprio agente
```

**Infra na AWS:** VPC própria (1 subnet pública, sem NAT) · EC2 `c7i-flex.large` com k3s · **nenhuma porta aberta** (acesso só por SSM Session Manager) · IMDSv2 · disco criptografado · role com `bedrock:InvokeModel` em **um** modelo · budget com alertas por e-mail.

## Guardrails

| Risco | Controle |
| --- | --- |
| LLM executa comando perigoso | O modelo não escreve comandos: escolhe de um **catálogo fechado** de 6 ações tipadas. Qualquer outra coisa vira `escalate`. |
| Ação errada | **Política em código** (`policy.py`): confiança mínima, allowlist de deployments, limites (réplicas 1..5, memória 64..1024Mi), só `FAULT_*` removíveis sozinho; reduzir réplicas ou remover outra variável exige aprovação. |
| Bug no agente | **RBAC mínimo**: leitura no namespace `orders` + escrita só em `deployments`. Sem delete, sem Secrets, sem exec. O cluster recusa o resto. |
| Ação piora o incidente | **Snapshot antes**; se a verificação mostra piora, restaura. |
| Loop de remediação | Máx. 2 tentativas por incidente, sem repetir ação, 10 ações/hora, 1 incidente por vez, cooldown por alerta. |
| Voltar para uma revisão ruim | O agente lembra as revisões que ele mesmo corrigiu e a política bloqueia rollback para elas (bug real encontrado nos testes). |
| LLM fora do ar / resposta inválida | Escala para humano (falha segura). |
| Prompt injection via logs | Logs tratados como dados não confiáveis; saída validada contra schema e catálogo. |
| Credencial vazada | Nenhuma chave estática: credencial temporária da role do nó via IMDSv2, permissão para um único modelo. |
| Testar sem risco | `DRY_RUN=true`: registra o que faria, não altera nada. |

## Subir (≈15 min, quase tudo automático)

No seu computador: `aws` (logado), `terraform`, [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).

```bash
cd infra/terraform
terraform init
terraform apply -var repo_url=https://github.com/MaikonJm/autonomous-sre-agent.git -var alert_email=seu@email.com
cd ../..
./scripts/lab.sh connect          # terminal no nó
sudo tail -f /var/log/sre-lab-deploy.log    # espere "PRIMEIRO BOOT CONCLUIDO" (~10 min) e o autoteste
```

O primeiro boot instala k3s, Docker, Helm, clona este repositório em `/opt/lab` e roda `scripts/deploy-on-node.sh`: build das imagens (importadas direto no k3s, sem registry), Prometheus/Alertmanager/Grafana, app, alertas, tráfego e agente. Termina com o **autoteste** (`aws_identity`, `bedrock: OK`, Kubernetes, Prometheus).

## Usar

```bash
# no nó (./scripts/lab.sh connect), dentro de /opt/lab:
python3 scripts/run_scenarios.py              # suíte completa, ~30 min -> reports/test-run-*.md
python3 scripts/run_scenarios.py api-errors   # um cenário só (demo ao vivo)
./scripts/node-forward.sh                     # para abrir no navegador (deixe a sessão aberta)

# no seu computador, em outro terminal:
./scripts/lab.sh tunnels    # agente :8080, Grafana :3000 (admin/admin), alertas :9090
```

## Ligar e desligar (sem destruir)

```bash
./scripts/lab.sh stop      # EC2 parada: sem custo de computação; disco ~US$ 2,40/mês
./scripts/lab.sh start     # liga de novo; em ~3 min tudo volta sozinho (imagens e Helm ficam no disco)
./scripts/lab.sh status
terraform -chdir=infra/terraform destroy   # quando não precisar mais
```

**Custo ligado:** c7i-flex.large ≈ US$ 0,09/h (elegível aos créditos do Free plan) + Bedrock (frações de centavo por incidente). VPC, IGW, IAM e budget não têm custo; o IPv4 público custa US$ 0,005/h só enquanto ligada.

## Cenários de teste

| Cenário | Falha injetada | Alerta | Correção esperada |
| --- | --- | --- | --- |
| `api-errors` | deploy com `FAULT_ERROR_RATE=0.5` | `OrdersApiHighErrorRate` | rollback ou remover a variável |
| `oom` | limite de memória 20Mi | `OrdersApiPodsFailing` | rollback ou ajustar memória |
| `scale-zero` | `kubectl scale --replicas=0` | `OrdersApiNoReplicas` | scale para 2 (não há mudança para reverter) |
| `api-latency` | deploy com `FAULT_LATENCY_MS=600` | `OrdersApiHighLatency` | rollback ou remover a variável |
| `bad-image` | tag de imagem inexistente | `OrdersApiPodsFailing` | rollback |

Testes automatizados (sem nuvem, cluster simulado): `cd apps/sre-agent && pip install -r requirements.txt pytest && pytest -q`: 20 testes cobrindo os 5 cenários, retry, reversão, guardrails e a regressão do rollback. Rodam no CI junto com `terraform validate`, kubeconform, Semgrep (SAST) e Trivy.

## Equivalências no Azure

O laboratório roda na AWS, mas cada peça tem par direto no Azure. O Terraform de referência para AKS está em [`infra/azure-aks-reference/`](infra/azure-aks-reference/) (com federação OIDC AKS → AWS para o Bedrock).

| Aqui (AWS) | No Azure |
| --- | --- |
| EC2 + k3s | AKS (control plane gerenciado) |
| Role do nó via IMDSv2 | Managed Identity / Workload Identity |
| SSM Session Manager | Azure Bastion, `az aks command invoke` |
| Security Group sem entrada | NSG |
| Bedrock (Nova Micro) | Azure OpenAI (gpt-4.1-mini) |
| AWS Budgets | Cost Management budgets |
| CloudWatch / Container Insights | Azure Monitor / Container Insights |
| Prometheus + Grafana no cluster | Managed Prometheus + Azure Managed Grafana |

## Troubleshooting

| Sintoma | Causa / correção |
| --- | --- |
| `terraform apply`: `Unsupported` / sem capacidade | Troque `-var instance_type=m7i-flex.large` ou a região. |
| `lab.sh connect` não conecta | A instância leva ~1 min para registrar no SSM após ligar. Confira o plugin instalado. |
| Autoteste: `bedrock: ERRO AccessDenied` | Modelo não habilitado na conta, ou região diferente de us-east-1. |
| Deploy falhou no meio | `sudo /opt/lab/scripts/deploy-on-node.sh` (é idempotente). Log: `/var/log/sre-lab-deploy.log`. |
| Alertas não disparam | Sem tráfego: `kubectl -n loadtest get pods`. Espere ~3 min após o deploy. |
| Agente escalou em vez de agir | Leia o relatório: a política explica o motivo (é o comportamento seguro). |
| Atualizar o código no nó | `cd /opt/lab && git pull && sudo ./scripts/deploy-on-node.sh` |

## Decisões e trade-offs

- **k3s numa EC2 em vez de EKS:** EKS cobra US$ 0,10/h só pelo control plane. Para um laboratório de uma pessoa, k3s dá o mesmo Kubernetes por uma fração. Produção: EKS com nós em 3 AZs.
- **Imagens importadas no nó em vez de ECR:** sem registry nem pull secret para gerenciar. Produção: ECR com scan e imagens assinadas, build no CI.
- **Nova Micro (modelo pequeno):** barato e rápido; a qualidade vem dos sinais determinísticos, lição do projeto anterior ("o modelo não era o problema, a evidência era").
- **Ações tipadas em vez de comandos livres:** perde flexibilidade, ganha auditabilidade e segurança.
- **Alertas ajustados para demo** (janelas de 1 a 5 min). Produção: burn rate multi-janela de 1h/6h.
- **Scanners em modo relatório no CI:** calibrar falsos positivos antes de transformar em gate.
