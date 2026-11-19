# Runbooks

Um runbook por alerta. Cada alerta do Prometheus aponta para a sua seção (`runbook_url`), e o relatório do agente traz o mesmo link. Estrutura fixa: **o que significa → impacto → como confirmar → como mitigar → quando escalar**.

Regra geral: **mitigar primeiro, investigar depois**. A primeira pergunta é sempre "o que mudou?".

Comandos rodam no nó (`./scripts/lab.sh connect`) com `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`.

---

## OrdersApiHighErrorRate

**Significa:** mais de 7,2% das requisições do orders-api retornando 5xx no último minuto e nos últimos 5 minutos. É 14,4 vezes o erro permitido pelo SLO de 99,5%: nesse ritmo, o error budget do mês acaba em ~2 dias.

**Impacto:** clientes recebendo erro ao listar ou criar pedidos.

**Confirmar:**
```bash
kubectl -n orders rollout history deploy/orders-api        # teve deploy recente?
kubectl -n orders get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].env}'
kubectl -n orders logs -l app=orders-api --since=10m | grep ERROR | tail
```

**Mitigar:**
- Deploy recente → `kubectl -n orders rollout undo deploy/orders-api`
- Variável `FAULT_*` ligada → `kubectl -n orders set env deploy/orders-api FAULT_ERROR_RATE-`
- Dependência externa fora → degradar a funcionalidade e avisar o dono da dependência

**Escalar se:** não houve mudança recente e os erros vêm de uma dependência fora do cluster.

---

## OrdersApiHighLatency

**Significa:** o p95 de latência passou de 300 ms por mais de 1 minuto (SLO de latência).

**Impacto:** clientes esperando; timeouts em quem chama a API.

**Confirmar:**
```bash
kubectl -n orders rollout history deploy/orders-api
kubectl top pods -n orders                                  # saturação de CPU/memória?
kubectl -n orders get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].env}'
```

**Mitigar:**
- Deploy recente ou `FAULT_LATENCY_MS` → rollback ou remover a variável
- Saturação → `kubectl -n orders scale deploy/orders-api --replicas=3`

**Escalar se:** a latência vem de uma dependência ou do nó (CPU steal, disco).

---

## OrdersApiPodsFailing

**Significa:** algum pod no namespace orders está preso em CrashLoopBackOff, ImagePullBackOff, ErrImagePull ou CreateContainerConfigError.

**Impacto:** com `maxUnavailable: 0`, os pods antigos continuam servindo; o risco é o próximo evento (um nó caindo) deixar o serviço sem réplicas. O deploy está travado.

**Confirmar:**
```bash
kubectl -n orders get pods
kubectl -n orders describe pod <pod>                        # Events e Last State
kubectl -n orders logs <pod> --previous                     # log da execução que morreu
```
- `OOMKilled` (exit 137) → limite de memória baixo
- `ImagePullBackOff` → tag inexistente ou sem permissão no registry

**Mitigar:** `kubectl -n orders rollout undo deploy/orders-api`. Se o limite de memória estava errado desde sempre: `kubectl -n orders set resources deploy/orders-api -c app --limits=memory=256Mi`.

**Escalar se:** o problema é de configuração que só o time da aplicação conhece (variável obrigatória, Secret ausente).

---

## OrdersApiNoReplicas

**Significa:** o Deployment do orders-api está com 0 réplicas desejadas. O serviço está fora do ar.

**Impacto:** total. Nenhuma requisição é atendida.

**Confirmar:** `kubectl -n orders get deploy orders-api`

**Mitigar:** `kubectl -n orders scale deploy/orders-api --replicas=2`

**Depois:** descobrir quem escalou para 0 e por quê (auditoria do Kubernetes). Em produção, uma policy de admissão impediria `replicas: 0` em serviço crítico.

---

## SreAgentDown

**Significa:** o Prometheus não consegue coletar métricas do agente há 2 minutos.

**Impacto:** incidentes voltam a depender só de humanos. Os alertas continuam disparando normalmente.

**Confirmar:**
```bash
kubectl -n sre-agent get pods
kubectl -n sre-agent logs deploy/sre-agent --tail=50
```

**Mitigar:** `kubectl -n sre-agent rollout restart deploy/sre-agent`. Enquanto isso, trate os alertas pelos runbooks acima.

---

## SreAgentLlmErrors

**Significa:** chamadas ao Bedrock falharam nos últimos 15 minutos. O agente está escalando tudo (falha segura).

**Confirmar:**
```bash
kubectl -n sre-agent exec deploy/sre-agent -- python -c \
  "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8080/selftest')),indent=2))"
```
- `AccessDenied` → permissão da role do nó ou modelo não habilitado
- `ThrottlingException` → quota do Bedrock; esperar ou pedir aumento
- Erro de rede → saída do nó para a internet

**Mitigar:** corrigir a causa. Nenhuma ação de remediação está sendo feita sem o LLM.

---

## SreAgentEscalating

**Significa:** o agente decidiu NÃO agir sozinho (confiança baixa, ação fora da política, limite de tentativas ou LLM fora) e entregou o incidente a um humano.

**O que fazer:** abrir o painel do agente (`./scripts/lab.sh tunnels` → http://localhost:8080) e ler o relatório: ele traz os sinais, o diagnóstico e o motivo exato da política. Seguir o runbook do alerta original.

**Depois:** se a decisão do agente era segura e ele foi conservador demais, isso vira candidato a nova regra na política (com teste automatizado), nunca um afrouxamento sem teste.
