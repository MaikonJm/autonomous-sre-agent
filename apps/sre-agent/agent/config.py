"""Configuração do agente, toda por variável de ambiente (ConfigMap)."""

import os
import json
from dataclasses import dataclass, field


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]

def _levels() -> dict[str, int]:
    # Configuration owned by the operator, not by the alert or LLM.
    try:
        raw = json.loads(os.getenv("SRE_ALERT_LEVELS", "{}"))
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and type(v) is int and 1 <= v <= 5}
    except (ValueError, TypeError):
        return {}


@dataclass
class Settings:
    namespace: str = field(default_factory=lambda: os.getenv("TARGET_NAMESPACE", "orders"))
    # Só estes Deployments podem ser alterados pelo agente
    target_deployments: list[str] = field(default_factory=lambda: _list("TARGET_DEPLOYMENTS", "orders-api"))
    prometheus_url: str = field(default_factory=lambda: os.getenv(
        "PROMETHEUS_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring:9090"))

    # --- autonomia e guardrails ---
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() != "false")
    autonomy: str = field(default_factory=lambda: os.getenv("SRE_AUTONOMY", "observe").lower())
    environment: str = field(default_factory=lambda: os.getenv("SRE_ENVIRONMENT", "unknown").lower())
    alert_levels: dict[str, int] = field(default_factory=_levels)
    auto_actions: list[str] = field(default_factory=lambda: _list("SRE_AUTO_ACTIONS", ""))
    webhook_token: str = field(default_factory=lambda: os.getenv("SRE_WEBHOOK_TOKEN", ""), repr=False)
    min_confidence: str = field(default_factory=lambda: os.getenv("MIN_CONFIDENCE", "medium"))
    max_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_ATTEMPTS", "2")))
    max_actions_per_hour: int = field(default_factory=lambda: int(os.getenv("MAX_ACTIONS_PER_HOUR", "6")))
    max_replicas: int = field(default_factory=lambda: int(os.getenv("MAX_REPLICAS", "5")))
    min_memory_mi: int = field(default_factory=lambda: int(os.getenv("MIN_MEMORY_MI", "64")))
    max_memory_mi: int = field(default_factory=lambda: int(os.getenv("MAX_MEMORY_MI", "1024")))
    # Variáveis de ambiente que o agente pode remover sozinho (o resto pede aprovação)
    removable_env_prefixes: list[str] = field(default_factory=lambda: _list("REMOVABLE_ENV_PREFIXES", "FAULT_"))
    cooldown_seconds: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_SECONDS", "300")))

    # --- time de agentes ---
    enable_reviewer: bool = field(default_factory=lambda: os.getenv("ENABLE_REVIEWER", "true").lower() == "true")
    enable_scribe: bool = field(default_factory=lambda: os.getenv("ENABLE_SCRIBE", "true").lower() == "true")

    # --- verificação pós-ação ---
    verify_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("VERIFY_TIMEOUT_SECONDS", "240")))
    verify_interval_seconds: int = field(default_factory=lambda: int(os.getenv("VERIFY_INTERVAL_SECONDS", "10")))
    healthy_error_ratio: float = field(default_factory=lambda: float(os.getenv("HEALTHY_ERROR_RATIO", "0.02")))
    healthy_p95_seconds: float = field(default_factory=lambda: float(os.getenv("HEALTHY_P95_SECONDS", "0.3")))

    # --- LLM (Amazon Bedrock, chamado do AKS via federação OIDC) ---
    bedrock_model_id: str = field(default_factory=lambda: os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-micro-v1:0"))
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-1"))
    # US$ por 1M tokens (Nova Micro); usados só para estimar custo no relatório
    price_input_per_m: float = field(default_factory=lambda: float(os.getenv("PRICE_INPUT_PER_M", "0.035")))
    price_output_per_m: float = field(default_factory=lambda: float(os.getenv("PRICE_OUTPUT_PER_M", "0.14")))

    # --- relatórios ---
    github_repository: str = field(default_factory=lambda: os.getenv("GITHUB_REPOSITORY", ""))
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
