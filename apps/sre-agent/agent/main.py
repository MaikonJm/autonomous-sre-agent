"""API do agente: recebe webhooks do Alertmanager e roda o loop autônomo."""

import logging
import threading
import time
from collections import deque

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
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
        from .planner import BedrockPlanner
        from .prom import Prometheus

        _runner = IncidentRunner(KubeCluster(settings.namespace), Prometheus(settings.prometheus_url),
                                 BedrockPlanner(settings), Policy(settings), settings)
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
        for at in inc["attempts"]:
            ACTIONS.labels(at["plan"]["action"].get("type", "?"), at["decision"]["verdict"]).inc()
        log.info("incidente %s: %s", inc["id"], inc["outcome"])
    except Exception:
        log.exception("falha no tratamento do incidente")
    finally:
        busy.release()


@app.post("/alertmanager")
def alertmanager(payload: dict, background: BackgroundTasks):
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
    return {"status": "ok", "busy": busy.locked(), "dry_run": settings.dry_run}


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
