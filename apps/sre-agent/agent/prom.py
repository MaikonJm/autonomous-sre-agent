"""Consultas ao Prometheus (SLIs). Best effort: erro vira None, nunca derruba a investigação."""

import httpx

SLI_QUERIES = {
    # janelas curtas: o agente precisa ver a melhora em ~1 min depois da ação
    "error_ratio_1m": 'sum(rate(http_requests_total{namespace="orders",status=~"5.."}[1m])) / sum(rate(http_requests_total{namespace="orders"}[1m]))',
    "error_ratio_5m": "orders_api:error_ratio:rate5m",
    "p95_latency_1m": 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{namespace="orders"}[1m])))',
    "requests_per_second": 'sum(rate(http_requests_total{namespace="orders"}[1m]))',
}


class Prometheus:
    def __init__(self, url: str):
        self.url = url

    def query(self, q: str) -> float | None:
        try:
            r = httpx.get(f"{self.url}/api/v1/query", params={"query": q}, timeout=5)
            result = r.json()["data"]["result"]
            if not result:
                return None
            value = float(result[0]["value"][1])
            return None if value != value else round(value, 4)  # NaN -> None
        except Exception:
            return None

    def slis(self) -> dict:
        return {name: self.query(q) for name, q in SLI_QUERIES.items()}
