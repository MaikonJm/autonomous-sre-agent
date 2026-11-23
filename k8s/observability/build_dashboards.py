"""Dashboards do Grafana como código.

Gera k8s/observability/dashboards.yaml com dois ConfigMaps (namespace monitoring,
label grafana_dashboard=1). O sidecar do Grafana do kube-prometheus-stack carrega
qualquer ConfigMap com esse label, sem clicar em nada.

Uso: python3 k8s/observability/build_dashboards.py
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
ORDERS = 'namespace="orders"'
GREEN, ORANGE, RED, BLUE = "green", "orange", "red", "blue"


def target(expr, legend="", ref="A"):
    return {"refId": ref, "expr": expr, "legendFormat": legend, "datasource": DS}


def stat(title, expr, unit, x, y, steps, w=6, h=4, decimals=None, desc=""):
    p = {
        "type": "stat", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}},
        "options": {"colorMode": "background", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}},
        "targets": [target(expr)],
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def ts(title, targets, unit, x, y, w=12, h=8, threshold=None, desc="", stack=False):
    defaults = {"unit": unit, "custom": {"fillOpacity": 10, "showPoints": "never"}}
    if stack:
        defaults["custom"].update({"stacking": {"mode": "normal"}, "fillOpacity": 60, "drawStyle": "bars"})
    if threshold is not None:
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": RED, "value": threshold}]}
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [target(e, leg, chr(65 + i)) for i, (e, leg) in enumerate(targets)],
    }


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False, "panels": []}


def dashboard(uid, title, panels, annotations, tags):
    return {
        "uid": uid, "title": title, "tags": tags, "timezone": "browser", "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"}, "schemaVersion": 39, "editable": True,
        "templating": {"list": [{"name": "datasource", "label": "Fonte", "type": "datasource", "query": "prometheus",
                                 "current": {"text": "Prometheus", "value": "prometheus"}}]},
        "annotations": {"list": annotations},
        "panels": panels,
    }


ANN_DEPLOY = {"name": "Deploy / mudança no orders-api", "datasource": DS, "enable": True, "iconColor": BLUE,
              "expr": f'changes(kube_deployment_status_observed_generation{{{ORDERS},deployment="orders-api"}}[1m]) > 0'}
ANN_AGENT = {"name": "Ação do agente SRE", "datasource": DS, "enable": True, "iconColor": "purple",
             # a 2ª parte cobre a primeira ação: a série nasce já em 1 e increase() não enxerga esse salto
             "expr": '(increase(sre_agent_actions_total{verdict="execute"}[1m]) > 0) or '
                     '(sre_agent_actions_total{verdict="execute"} unless sre_agent_actions_total{verdict="execute"} offset 1m)'}
ANN_ALERT = {"name": "Alerta disparado", "datasource": DS, "enable": True, "iconColor": RED,
             "expr": 'ALERTS{alertstate="firing",agent="sre"}'}

# ------------------------------------------------------------------ orders-api
ok_steps = [{"color": RED, "value": None}, {"color": ORANGE, "value": 0.99}, {"color": GREEN, "value": 0.995}]
orders_panels = [
    row("SLO: 99,5% sem erro · p95 < 300 ms", 0),
    stat("Disponibilidade (1h)",
         f'1 - ((sum(rate(http_requests_total{{{ORDERS},status=~"5.."}}[1h])) or vector(0)) / sum(rate(http_requests_total{{{ORDERS}}}[1h])))',
         "percentunit", 0, 1, ok_steps, decimals=2, desc="Fração de requisições sem 5xx. SLO: 99,5%."),
    stat("Burn rate (5m)", "orders_api:error_ratio:rate5m / 0.005", "x", 6, 1,
         [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": RED, "value": 14.4}], decimals=1,
         desc="Velocidade de consumo do error budget. 1x = sustentável; 14,4x = acaba em ~2 dias (alerta)."),
    stat("Latência p95 (2m)", "orders_api:p95_latency:rate2m", "s", 12, 1,
         [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 0.2}, {"color": RED, "value": 0.3}], decimals=3),
    stat("Alertas ativos", 'count(ALERTS{alertstate="firing",agent="sre"}) or vector(0)', "none", 18, 1,
         [{"color": GREEN, "value": None}, {"color": RED, "value": 1}], desc="Alertas de SLO disparados agora."),
    row("Golden signals", 5),
    ts("Tráfego (req/s por rota e status)",
       [(f"sum by (route, status) (rate(http_requests_total{{{ORDERS}}}[1m]))", "{{route}} {{status}}")], "reqps", 0, 6),
    ts("Erros: taxa de 5xx (limiar do alerta: 7,2%)",
       [("orders_api:error_ratio:rate1m", "janela 1m (verificação)"), ("orders_api:error_ratio:rate5m", "janela 5m (alerta)")],
       "percentunit", 12, 6, threshold=0.072),
    ts("Latência p50 / p95 / p99 (SLO: p95 < 300 ms)",
       [(f'histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{ORDERS}}}[2m])))', f"p{int(q * 100)}")
        for q in (0.5, 0.95, 0.99)], "s", 0, 14, threshold=0.3),
    ts("Saturação: CPU e memória por pod",
       [(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{ORDERS},container="app"}}[2m]))', "CPU {{pod}} (cores)"),
        (f'sum by (pod) (container_memory_working_set_bytes{{{ORDERS},container="app"}}) / 1024 / 1024', "Mem {{pod}} (MiB)")],
       "short", 12, 14),
    row("Kubernetes", 22),
    ts("Réplicas: desejadas x disponíveis",
       [(f'kube_deployment_spec_replicas{{{ORDERS},deployment="orders-api"}}', "desejadas"),
        (f'kube_deployment_status_replicas_available{{{ORDERS},deployment="orders-api"}}', "disponíveis")], "short", 0, 23),
    ts("Pods com problema (CrashLoop, ImagePull, OOM)",
       [(f'sum by (reason) (kube_pod_container_status_waiting_reason{{{ORDERS}}})', "aguardando: {{reason}}"),
        (f'sum(increase(kube_pod_container_status_restarts_total{{{ORDERS}}}[5m]))', "restarts (5m)")], "short", 12, 23),
]

# ------------------------------------------------------------------ agente
agent_panels = [
    row("Resultado do agente", 0),
    stat("Resolvidos sozinho (24h)", 'sum(increase(sre_agent_incidents_total{outcome="resolved"}[24h]))', "none", 0, 1,
         [{"color": BLUE, "value": None}], decimals=0),
    stat("Escalados a humano (24h)", 'sum(increase(sre_agent_incidents_total{outcome=~"escalated|approval_required"}[24h]))',
         "none", 4, 1, [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}], w=5, decimals=0,
         desc="Não é falha: é a política sendo conservadora. Cada um tem relatório no painel do agente."),
    stat("MTTR mediano", "histogram_quantile(0.5, sum by (le) (rate(sre_agent_mttr_seconds_bucket[24h])))", "s", 9, 1,
         [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 300}, {"color": RED, "value": 900}], w=5, decimals=0,
         desc="Do início do alerta até a correção verificada."),
    stat("Custo de LLM (acumulado)", "sum(sre_agent_llm_cost_usd_total)", "currencyUSD", 14, 1,
         [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}], w=5, decimals=5),
    stat("Agente no ar", 'max(up{job="sre-agent"}) or vector(0)', "none", 19, 1,
         [{"color": RED, "value": None}, {"color": GREEN, "value": 1}], w=5,
         desc="Quem vigia o vigia: se cair, o alerta SreAgentDown vai para humanos."),
    row("Decisões", 5),
    ts("Incidentes por resultado", [('sum by (outcome) (increase(sre_agent_incidents_total[5m]))', "{{outcome}}")],
       "none", 0, 6, stack=True),
    ts("Ações por tipo e veredito da política",
       [('sum by (type, verdict) (increase(sre_agent_actions_total[5m]))', "{{type}} → {{verdict}}")], "none", 12, 6, stack=True,
       desc="execute = rodou sozinho; approval_required / escalate = a política barrou."),
    row("LLM (Amazon Bedrock · Nova Micro)", 14),
    ts("Latência do LLM p50 / p95",
       [("histogram_quantile(0.5, sum by (le) (rate(sre_agent_llm_latency_seconds_bucket[30m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(sre_agent_llm_latency_seconds_bucket[30m])))", "p95")], "s", 0, 15, w=8),
    ts("Tokens por minuto", [('sum by (direction) (rate(sre_agent_llm_tokens_total[5m])) * 60', "{{direction}}")],
       "short", 8, 15, w=8),
    ts("Chamadas ao LLM: ok x erro",
       [('sum by (outcome) (increase(sre_agent_llm_calls_total[5m]))', "{{outcome}}")], "none", 16, 15, w=8, stack=True,
       desc="Erro aqui = falha segura: o agente escala em vez de agir."),
    row("Time de agentes: quem fez o quê", 23),
    ts("Chamadas ao LLM por agente",
       [('sum by (agent) (increase(sre_agent_team_llm_calls_total{outcome="ok"}[5m]))', "{{agent}}")], "none", 0, 24, w=8,
       stack=True, desc="Diagnosticador decide, Revisor confere, Documentador escreve o postmortem."),
    ts("Custo por agente (US$, acumulado)",
       [("sum by (agent) (sre_agent_team_llm_cost_usd_total)", "{{agent}}")], "currencyUSD", 8, 24, w=8),
    ts("Vereditos do Revisor de segurança",
       [('sum by (verdict) (increase(sre_agent_reviews_total[5m]))', "{{verdict}}")], "none", 16, 24, w=8, stack=True,
       desc="reject = o Revisor barrou o plano e o caso foi para um humano (mais conservador, nunca menos)."),
    ts("Latência do LLM por agente (p95)",
       [("histogram_quantile(0.95, sum by (le, agent) (rate(sre_agent_team_llm_latency_seconds_bucket[30m])))", "{{agent}}")],
       "s", 0, 32, w=12),
    ts("Duração do tratamento (p50 / p95)",
       [("histogram_quantile(0.5, sum by (le) (rate(sre_agent_incident_seconds_bucket[30m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(sre_agent_incident_seconds_bucket[30m])))", "p95")], "s", 12, 32, w=12),
]

boards = {
    "orders-api-slo.json": dashboard("orders-api-slo", "orders-api: SLO e golden signals", orders_panels,
                                     [ANN_DEPLOY, ANN_AGENT, ANN_ALERT], ["slo", "orders"]),
    "sre-agent-llm.json": dashboard("sre-agent-llm", "Agente SRE e LLM", agent_panels, [ANN_AGENT, ANN_ALERT], ["agente", "llm"]),
}

docs = []
for fname, board in boards.items():
    body = json.dumps(board, indent=2, ensure_ascii=False)
    docs.append(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        f"  name: dashboard-{fname.removesuffix('.json')}\n  namespace: monitoring\n"
        '  labels:\n    grafana_dashboard: "1"\n'
        f"data:\n  {fname}: |\n" + "\n".join("    " + line for line in body.splitlines()) + "\n"
    )
out = Path(__file__).parent / "dashboards.yaml"
out.write_text("# GERADO por build_dashboards.py: edite o script, não este arquivo.\n" + "---\n".join(docs))
print(f"ok: {out} ({sum(len(b['panels']) for b in boards.values())} painéis)")
