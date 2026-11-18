# Overview: o que faz cada coisa

Guia para se situar no projeto inteiro em 10 minutos. Ordem: do mais externo (nuvem) ao mais interno (o cérebro do agente).

## 1. Infra na AWS: `infra/terraform/`

| Arquivo | O que cria | Por quê |
| --- | --- | --- |
| `main.tf`, rede | VPC `10.20.0.0/16`, 1 subnet pública, Internet Gateway, rota | Rede isolada do resto da conta. Sem NAT Gateway (custaria ~US$ 32/mês). |
| `main.tf`, Security Group | **Nenhuma regra de entrada**, saída liberada | Ninguém alcança a máquina pela internet. Acesso só por SSM. |
| `main.tf`, IAM | Role do nó com SSM + `bedrock:InvokeModel` em 1 modelo | Menor privilégio. O pod do agente usa essa role via IMDSv2. |
| `main.tf`, EC2 | `c7i-flex.large` Ubuntu 24.04, IMDSv2 obrigatório, disco 30 GB criptografado | O único recurso com custo por hora. |
| `main.tf`, Budget | Alerta por e-mail em 50/80/100% de US$ 10/mês | Guarda de custo. |
| `user-data.sh.tftpl` | Primeiro boot: k3s, Docker, Helm, clone do repo, deploy | Um `terraform apply` e em ~10 min está tudo rodando. |
| `variables.tf`, `outputs.tf` | Parâmetros (tipo de instância, repo, e-mail) e saídas (ID, comando de conexão) | |

`infra/azure-aks-reference/`: a versão AKS (não aplicada), para mostrar o equivalente no Azure.

## 2. Dentro do cluster (k3s, Kubernetes de 1 nó)

| Namespace | O que roda | Arquivo |
| --- | --- | --- |
| `orders` | **orders-api** (2 réplicas): a aplicação "de negócio" que vai quebrar nos testes. Expõe `/metrics`. | `k8s/orders/orders-api.yaml` |
| `orders` | **Regras de SLO e alertas** (PrometheusRule): taxa de erro, latência, pods falhando, 0 réplicas. Todo alerta leva `agent=sre`. | `k8s/orders/alerts.yaml` |
| `loadtest` | **loadgen**: ~10 req/s contra a API. Sem tráfego não há SLI e nenhum alerta dispara. | `k8s/loadtest/loadgen.yaml` |
| `monitoring` | **Prometheus** (coleta e avalia regras), **Alertmanager** (roteia alertas `agent=sre` para o webhook do agente), **Grafana**, **kube-state-metrics** (estado dos pods e deployments). | `k8s/observability/values.yaml` |
| `sre-agent` | **O agente**: Deployment, ServiceAccount, RBAC mínimo, ConfigMap com os guardrails. | `k8s/sre-agent/sre-agent.yaml` |

Fluxo de um alerta: `orders-api` gera erros → Prometheus calcula a taxa de erro (regra de gravação) → a regra de alerta fica `firing` após 1 min → Alertmanager agrupa e envia POST para `http://sre-agent.sre-agent:8080/alertmanager` → o agente trata.

## 3. As aplicações: `apps/`

**`apps/orders-api/`**
- `main.py`: API FastAPI com métricas RED (requisições, erros, latência em histograma). Falhas injetadas por variáveis de ambiente `FAULT_ERROR_RATE` e `FAULT_LATENCY_MS`: toda falha vira uma nova revisão do Deployment, como um deploy ruim de verdade.
- `loadgen.py`: o gerador de tráfego (mesma imagem, outro comando).

**`apps/sre-agent/agent/`**: o cérebro, um arquivo por responsabilidade:

| Arquivo | Papel | Analogia com um SRE humano |
| --- | --- | --- |
| `main.py` | API: recebe o webhook, cooldown, 1 incidente por vez, painel `/`, relatórios `/reports`, `/selftest`, `/metrics` | O pager tocando |
| `incident.py` | O **loop**: evidência → plano → política → ação → verificação → retry/reversão/escala | O processo de resposta a incidente |
| `evidence.py` | Coleta (só leitura) e **sinais determinísticos**: "o que mudou na última revisão?", OOMKilled, CrashLoop, 0 réplicas, erro nos logs | Abrir o dashboard e perguntar "o que mudou?" |
| `planner.py` | Prompt e chamada ao **Bedrock**; o modelo devolve JSON com diagnóstico e **uma** ação do catálogo | O raciocínio |
| `policy.py` | **Guardrails**: decide `execute`, `approval_required`, `escalate` ou `dry_run` | O processo de mudança da empresa |
| `kube.py` | Leitura do Kubernetes e as ações (rollback igual ao `kubectl rollout undo`, scale, restart, etc.) | As mãos no `kubectl` |
| `prom.py` | Consultas PromQL dos SLIs | Olhar o Grafana |
| `report.py` | Relatório Markdown, painel HTML, issue no GitHub | O postmortem |
| `config.py` | Todos os limites, lidos do ConfigMap | O runbook com as regras |

`apps/sre-agent/tests/`: cluster e Prometheus **simulados** (`fakes.py`) e 20 testes (`test_agent.py`).

## 4. Scripts: `scripts/`

| Script | Onde roda | O que faz |
| --- | --- | --- |
| `lab.sh` | Seu computador | `status`, `stop`, `start`, `connect` (terminal SSM), `tunnels` (abre 8080/3000/9090 no navegador) |
| `deploy-on-node.sh` | No nó | Build das imagens, Prometheus, app, agente, autoteste. Idempotente. |
| `run_scenarios.py` | No nó | Injeta cada falha, mede MTTD e MTTR, coleta o relatório do agente, gera `reports/test-run-*.md` |
| `node-forward.sh` | No nó | Publica agente, Grafana e Prometheus no localhost do nó, para os túneis |

## 5. CI: `.github/workflows/ci.yml`

A cada push: testes do agente, `terraform fmt` + `validate`, kubeconform nos manifests (bloqueiam). Semgrep (SAST) e Trivy (dependências e misconfig) em modo relatório.

## 6. Onde olhar durante a demo

| URL (com túnel) | O que mostrar |
| --- | --- |
| `localhost:9090/alerts` | O alerta passando de `pending` para `firing` |
| `localhost:8080` | O painel do agente: incidente, resultado, ação, MTTR, tokens |
| `localhost:8080/reports/<id>` | O relatório completo (sinais, tentativas, política, SLIs antes e depois, linha do tempo, custo) |
| `localhost:3000` | Grafana (dashboards padrão do Kubernetes) |
