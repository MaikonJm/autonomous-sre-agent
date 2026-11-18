"""orders-api: microsserviço alvo dos cenários de falha.

Falhas são injetadas por variáveis de ambiente do Deployment (FAULT_*), como
no projeto AWS. Assim toda falha vira uma nova revisão do Deployment, que é
exatamente o tipo de mudança que causa incidentes reais ("o que mudou?").
"""

import asyncio
import logging
import os
import random
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

VERSION = os.getenv("APP_VERSION", "dev")
FAULT_ERROR_RATE = float(os.getenv("FAULT_ERROR_RATE", "0") or 0)
FAULT_LATENCY_MS = int(os.getenv("FAULT_LATENCY_MS", "0") or 0)

logging.basicConfig(level="INFO", format='{"level":"%(levelname)s","service":"orders-api","msg":"%(message)s"}')
log = logging.getLogger("orders-api")

REQUESTS = Counter("http_requests_total", "Requisições HTTP", ["method", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Latência HTTP", ["method", "route"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5),
)
BUILD = Gauge("app_build_info", "Versão em execução", ["version"])
BUILD.labels(VERSION).set(1)

app = FastAPI(title="orders-api", version=VERSION)
ORDERS: dict[str, dict] = {}


class OrderIn(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=1000)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route not in ("/metrics", "/healthz", "/readyz"):
            REQUESTS.labels(request.method, route, str(status)).inc()
            LATENCY.labels(request.method, route).observe(time.perf_counter() - start)


async def faults():
    if FAULT_LATENCY_MS:
        await asyncio.sleep(FAULT_LATENCY_MS / 1000)
    if random.random() < FAULT_ERROR_RATE:
        log.error("payment dependency call failed: injected fault FAULT_ERROR_RATE")
        raise HTTPException(503, "dependency unavailable")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready", "version": VERSION}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/orders")
async def list_orders():
    await faults()
    await asyncio.sleep(random.uniform(0.005, 0.04))
    return {"orders": list(ORDERS.values())[-20:]}


@app.post("/orders", status_code=201)
async def create_order(order: OrderIn):
    await faults()
    await asyncio.sleep(random.uniform(0.01, 0.06))
    oid = str(uuid.uuid4())
    ORDERS[oid] = {"id": oid, **order.model_dump()}
    return ORDERS[oid]
