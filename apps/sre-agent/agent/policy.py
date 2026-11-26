"""Política de guardrails: decide, em código, se a ação escolhida pelo LLM pode rodar.

Veredictos:
  execute            ação de baixo risco, reversível, dentro dos limites -> roda sozinha
  approval_required  ação válida mas de risco maior -> vira pedido de aprovação humana
  escalate           ação inválida, confiança baixa ou limites estourados -> humano assume
  dry_run            modo observação: registra o que faria, não muda nada
"""

import time
from dataclasses import dataclass, field

CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
ACTIONS = {"rollback", "remove_env", "scale", "set_memory_limit", "restart", "escalate"}


@dataclass
class Decision:
    verdict: str
    reasons: list[str] = field(default_factory=list)


class Policy:
    def __init__(self, settings, clock=time.time):
        self.s = settings
        self.clock = clock
        self.executed: list[float] = []  # timestamps de ações executadas (rate limit global)

    def severity(self, alert: dict | None) -> int:
        labels = (alert or {}).get("labels") or {}
        if not isinstance(labels, dict) or labels.get("namespace") != self.s.namespace:
            return 5
        name = labels.get("alertname")
        if not isinstance(name, str):
            return 5
        configured = self.s.alert_levels.get(name, 5)
        if type(configured) is not int or not 1 <= configured <= 5:
            return 5
        # Origin labels may only increase risk. An unknown explicit label is critical.
        reported = labels.get("severity")
        if reported is not None:
            mapped = {"info": 1, "informational": 1, "low": 2, "warning": 3,
                      "moderate": 3, "high": 4, "critical": 5}
            label = str(reported).lower()
            configured = max(configured, mapped.get(label, int(label) if label in ("1", "2", "3", "4", "5") else 5))
        level = labels.get("sre_level")
        if level is not None:
            configured = max(configured, int(level) if str(level) in ("1", "2", "3", "4", "5") else 5)
        return configured

    def evaluate(self, plan: dict, evidence: dict, attempts: list[dict], alert: dict | None = None) -> Decision:
        action = plan.get("action") or {}
        kind = action.get("type")
        reasons = []
        level = self.severity(alert)
        if level >= 4:
            return Decision("escalate", [f"N{level}: investigação e escalonamento; nenhuma alteração autorizada"])

        if kind == "escalate":
            return Decision("escalate", [f"o modelo pediu escalonamento: {action.get('reason', '')}"])
        if kind not in ACTIONS:
            return Decision("escalate", [f"ação '{kind}' não existe no catálogo"])
        if CONFIDENCE.get(plan.get("confidence"), 0) < CONFIDENCE[self.s.min_confidence]:
            return Decision("escalate", [f"confiança '{plan.get('confidence')}' abaixo do mínimo '{self.s.min_confidence}'"])

        target = action.get("deployment")
        if target not in self.s.target_deployments:
            return Decision("escalate", [f"deployment '{target}' fora da allowlist {self.s.target_deployments}"])
        deploy = next((d for d in evidence.get("deployments", []) if d["name"] == target), None)
        if not deploy:
            return Decision("escalate", [f"deployment '{target}' não encontrado no namespace"])

        if len(attempts) >= self.s.max_attempts:
            return Decision("escalate", [f"limite de {self.s.max_attempts} tentativas por incidente atingido"])
        if any(a["plan"].get("action") == action for a in attempts):
            return Decision("escalate", ["a mesma ação já foi tentada neste incidente e não resolveu"])
        now = self.clock()
        self.executed = [t for t in self.executed if now - t < 3600]
        if len(self.executed) >= self.s.max_actions_per_hour:
            return Decision("escalate", [f"limite global de {self.s.max_actions_per_hour} ações/hora atingido (proteção contra loop)"])

        # --- regras por tipo de ação ---
        if kind == "rollback":
            rc = next((r for r in evidence.get("recent_changes", []) if r["deployment"] == target), None)
            if not rc:
                return Decision("escalate", ["rollback pedido, mas não há revisão anterior"])
            if rc.get("current_is_agent_remediation"):
                return Decision("escalate", [f"rollback reintroduziria a revisão {rc['previous_revision']}, que o agente já corrigiu antes"])
            reasons.append(f"rollback para a revisão {rc['previous_revision']} (reversível)")
        elif kind == "scale":
            replicas = action.get("replicas")
            if not isinstance(replicas, int) or not 1 <= replicas <= self.s.max_replicas:
                return Decision("escalate", [f"réplicas={replicas} fora do intervalo 1..{self.s.max_replicas}"])
            if replicas < (deploy["replicas_desired"] or 0):
                return Decision("approval_required", ["reduzir réplicas durante incidente exige aprovação"])
            reasons.append(f"escalar para {replicas} réplicas (dentro do limite)")
        elif kind == "remove_env":
            name = str(action.get("name", ""))
            if name not in deploy["env"]:
                return Decision("escalate", [f"variável '{name}' não existe no deployment"])
            if not any(name.startswith(p) for p in self.s.removable_env_prefixes):
                return Decision("approval_required", [f"remover '{name}' pode quebrar a aplicação: só {self.s.removable_env_prefixes} são removíveis sozinho"])
            reasons.append(f"remover variável de fault injection '{name}'")
        elif kind == "set_memory_limit":
            mi = action.get("memory_mi")
            if not isinstance(mi, int) or not self.s.min_memory_mi <= mi <= self.s.max_memory_mi:
                return Decision("approval_required", [f"memória {mi}Mi fora do intervalo seguro {self.s.min_memory_mi}..{self.s.max_memory_mi}Mi"])
            reasons.append(f"ajustar limite de memória para {mi}Mi (dentro do intervalo seguro)")
        elif kind == "restart":
            reasons.append("rollout restart (sem mudança de configuração)")

        if (self.s.dry_run or level == 1 or self.s.autonomy != "lab"
                or self.s.environment not in ("lab", "test", "sandbox")):
            return Decision("dry_run", reasons + ["Política de observação: nada é alterado"])
        if not self.s.webhook_token or not self.s.enable_reviewer:
            return Decision("approval_required", reasons + ["Remediação exige autenticação do webhook e revisor habilitado"])
        if kind not in self.s.auto_actions:
            return Decision("approval_required", reasons + ["Ação não autorizada pelo operador"])
        return Decision("execute", reasons + [f"N{level}: runbook autorizado em laboratório"])

    def record_execution(self):
        self.executed.append(self.clock())
