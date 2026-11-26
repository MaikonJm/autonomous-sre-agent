"""API do agente: recebe webhooks do Alertmanager e roda o loop autônomo."""

import logging
import hmac
import threading
import time
from collections import deque

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from . import report
from .config import Settings
from .incident import IncidentRunner
from .policy import Policy

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("sre-agent")

INCIDENTS = Counter("sre_agent_incidents_total", "Incidentes tratados", ["alertname", "outcome"])
ACTIONS = Counter("sre_agent_actions_total", "Ações decididas pela política", ["type", "verdict"])
TOKENS = Counter("sre_agent_llm_tokens_total", "Tokens do LLM", ["direction"])
DURATION = Histogram("sre_agent_incident_seconds", "Duração do tratamento", buckets=(30, 60, 120, 180, 300, 600))
LLM_LATENCY = Histogram("sre_agent_llm_latency_seconds", "Latência de cada chamada ao LLM",
                        buckets=(0.25, 0.5, 1, 2, 4, 8, 16))
LLM_CALLS = Counter("sre_agent_llm_calls_total", "Chamadas ao LLM", ["outcome"])
LLM_COST = Counter("sre_agent_llm_cost_usd_total", "Custo estimado do LLM em US$")
MTTR = Histogram("sre_agent_mttr_seconds", "Do início do alerta até a correção verificada",
                 buckets=(30, 60, 90, 120, 180, 300, 600, 1200))
AGENT_CALLS = Counter("sre_agent_team_llm_calls_total", "Chamadas ao LLM por agente do time", ["agent", "outcome"])
AGENT_TOKENS = Counter("sre_agent_team_llm_tokens_total", "Tokens por agente do time", ["agent", "direction"])
AGENT_COST = Counter("sre_agent_team_llm_cost_usd_total", "Custo estimado por agente do time", ["agent"])
AGENT_LATENCY = Histogram("sre_agent_team_llm_latency_seconds", "Latência do LLM por agente", ["agent"],
                          buckets=(0.25, 0.5, 1, 2, 4, 8, 16))
REVIEWS = Counter("sre_agent_reviews_total", "Veredito do agente Revisor", ["verdict"])

for _o in ("resolved", "escalated", "approval_required", "dry_run"):
    INCIDENTS.labels("-", _o)
for _o in ("ok", "error"):
    LLM_CALLS.labels(_o)
for _d in ("input", "output"):
    TOKENS.labels(_d)
for _a in ("diagnosticador", "revisor", "documentador"):
    for _o in ("ok", "error"):
        AGENT_CALLS.labels(_a, _o)
    AGENT_COST.labels(_a)
for _v in ("approve", "reject", "abstain"):
    REVIEWS.labels(_v)

settings = Settings()
app = FastAPI(title="Agente SRE autônomo")
incidents: deque = deque(maxlen=50)
markdown: dict[str, str] = {}
last_seen: dict[str, float] = {}
busy = threading.Lock()  # um incidente por vez: evita duas ações concorrentes no mesmo serviço
_runner: IncidentRunner | None = None


def runner() -> IncidentRunner:
    global _runner
    if _runner is None:
        from .kube import KubeCluster
        from .planner import BedrockLLM, DiagnosticianAgent
        from .prom import Prometheus
        from .team import SafetyReviewerAgent, ScribeAgent

        llm = BedrockLLM(settings)  # um cliente, vários agentes com prompts e papéis diferentes
        _runner = IncidentRunner(KubeCluster(settings.namespace), Prometheus(settings.prometheus_url),
                                 DiagnosticianAgent(llm), Policy(settings), settings,
                                 reviewer=SafetyReviewerAgent(llm) if settings.enable_reviewer else None,
                                 scribe=ScribeAgent(llm) if settings.enable_scribe else None)
    return _runner


def handle(alert: dict) -> None:
    if not busy.acquire(blocking=False):
        log.info("já existe incidente em andamento; alerta %s ignorado", alert.get("labels", {}).get("alertname"))
        return
    try:
        inc = runner().run(alert)
        body = report.render_markdown(inc)
        inc["issue_url"] = report.publish_issue(inc, body, settings)
        markdown[inc["id"]] = body
        incidents.appendleft(inc)
        INCIDENTS.labels(inc["alertname"], inc["outcome"]).inc()
        DURATION.observe(inc["agent_seconds"])
        TOKENS.labels("input").inc(inc["usage"]["inputTokens"])
        TOKENS.labels("output").inc(inc["usage"]["outputTokens"])
        LLM_COST.inc(inc["usage"]["cost_usd"])
        if inc.get("mttr_seconds") is not None:
            MTTR.observe(inc["mttr_seconds"])
        for agent, a in inc["usage"].get("by_agent", {}).items():
            AGENT_CALLS.labels(agent, "ok").inc(a["calls"] - a["errors"])
            AGENT_CALLS.labels(agent, "error").inc(a["errors"])
            AGENT_TOKENS.labels(agent, "input").inc(a["inputTokens"])
            AGENT_TOKENS.labels(agent, "output").inc(a["outputTokens"])
            AGENT_COST.labels(agent).inc(a.get("cost_usd", 0))
            for lat in a.get("latencies", []):
                AGENT_LATENCY.labels(agent).observe(lat)
        for at in inc["attempts"]:
            if at.get("review"):
                REVIEWS.labels(at["review"]["verdict"]).inc()
            ACTIONS.labels(at["plan"]["action"].get("type", "?"), at["decision"]["verdict"]).inc()
            LLM_CALLS.labels("ok" if at.get("llm_ok") else "error").inc()
            if at.get("usage", {}).get("latency_seconds"):
                LLM_LATENCY.observe(at["usage"]["latency_seconds"])
        log.info("incidente %s: %s", inc["id"], inc["outcome"])
    except Exception:
        log.exception("falha no tratamento do incidente")
    finally:
        busy.release()


@app.post("/alertmanager")
def alertmanager(payload: dict, background: BackgroundTasks, x_sre_webhook_token: str = Header(default="")):
    if settings.webhook_token and not hmac.compare_digest(
            x_sre_webhook_token.encode("utf-8"), settings.webhook_token.encode("utf-8")):
        raise HTTPException(401, "Webhook não autorizado")
    # Sem token configurado, a política bloqueia qualquer remediação.
    started = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        if alert.get("status") != "firing" or labels.get("namespace") != settings.namespace:
            continue
        key = labels.get("alertname", "")
        if time.time() - last_seen.get(key, 0) < settings.cooldown_seconds:
            continue
        last_seen[key] = time.time()
        background.add_task(handle, alert)
        started.append(key)
    return {"investigating": started}


@app.get("/", response_class=HTMLResponse)
def index():
    return report.render_index(list(incidents))


@app.get("/reports")
def reports():
    return [{k: v for k, v in i.items() if k != "alert"} for i in incidents]


@app.get("/reports/{incident_id}", response_class=PlainTextResponse)
def report_md(incident_id: str):
    if incident_id not in markdown:
        raise HTTPException(404)
    return markdown[incident_id]


@app.get("/healthz")
def healthz():
    return {"status": "ok", "busy": busy.locked(), "dry_run": settings.dry_run,
            "autonomy": settings.autonomy, "environment": settings.environment}


@app.get("/selftest")
def selftest():
    """Checagem antes da demo: identidade AWS (federação OIDC), Bedrock, Kubernetes e Prometheus."""
    import boto3

    out = {}
    try:
        ident = boto3.client("sts", region_name=settings.aws_region).get_caller_identity()
        out["aws_identity"] = ident["Arn"].split(":")[-1]
    except Exception as exc:
        out["aws_identity"] = f"ERRO: {exc}"
    try:
        resp = boto3.client("bedrock-runtime", region_name=settings.aws_region).converse(
            modelId=settings.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
            inferenceConfig={"maxTokens": 5})
        out["bedrock"] = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:
        out["bedrock"] = f"ERRO: {exc}"
    try:
        out["kubernetes_deployments"] = [d["name"] for d in runner().cluster.deployments()]
    except Exception as exc:
        out["kubernetes_deployments"] = f"ERRO: {exc}"
    try:
        out["prometheus_slis"] = runner().prom.slis()
    except Exception as exc:
        out["prometheus_slis"] = f"ERRO: {exc}"
    return out


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
