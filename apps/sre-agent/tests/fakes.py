"""Cluster e Prometheus simulados para testar o loop autônomo sem Azure.

O FakeCluster imita o que importa do Kubernetes para os cenários:
revisões de Deployment, rolling update com maxUnavailable=0 (pods antigos
continuam servindo quando o template novo não sobe), CrashLoop por OOM,
ImagePullBackOff, scale e rollback.
"""

import copy
from datetime import datetime, timedelta, timezone

GOOD = {"image": "acr.io/orders-api:v1", "env": {"APP_VERSION": "v1"}, "memory_limit": "256Mi"}


def _mi(v):
    return int(v.replace("Mi", "")) if v else 9999


def broken(template) -> str | None:
    if template["image"].endswith(":does-not-exist"):
        return "ImagePullBackOff"
    if _mi(template.get("memory_limit")) < 50:
        return "CrashLoopBackOff"
    return None


class FakeCluster:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.revs = [{"revision": 1, "created": (now - timedelta(days=1)).isoformat(), **copy.deepcopy(GOOD)}]
        self.template = copy.deepcopy(GOOD)
        self.revision = 1
        self.replicas = 2
        self.actions = []

    # ---- mudanças "humanas" (injeção de falha) ----
    def apply_template(self, **changes):
        t = copy.deepcopy(self.template)
        env = changes.pop("env", None)
        if env is not None:
            t["env"] = env
        t.update(changes)
        self._new_revision(t)

    def _new_revision(self, t):
        existing = next((r for r in self.revs if {k: r[k] for k in GOOD} == t), None)
        self.revision = max(r["revision"] for r in self.revs) + 1
        if existing:  # como no k8s: o mesmo template reaproveita o RS e ganha número novo
            existing["revision"] = self.revision
        else:
            self.revs.append({"revision": self.revision, "created": datetime.now(timezone.utc).isoformat(),
                              **copy.deepcopy(t)})
        self.template = copy.deepcopy(t)

    def last_good(self):
        good = [r for r in sorted(self.revs, key=lambda r: r["revision"]) if not broken(r)]
        return good[-1]

    # ---- leitura ----
    def deployments(self):
        bad = broken(self.template)
        ready = self.replicas if not bad else self.replicas  # maxUnavailable=0: antigos continuam
        updated = (1 if self.replicas else 0) if bad else self.replicas
        return [{"name": "orders-api", "revision": self.revision, "replicas_desired": self.replicas,
                 "replicas_ready": ready if self.replicas else 0, "replicas_updated": updated,
                 "replicas_available": ready if self.replicas else 0, **copy.deepcopy(self.template)}]

    def pods(self):
        if not self.replicas:
            return []
        pods = [{"name": f"orders-api-{i}", "app": "orders-api", "phase": "Running", "ready": True, "restarts": 0,
                 "waiting_reasons": [], "last_terminated_reasons": []} for i in range(self.replicas)]
        bad = broken(self.template)
        if bad:
            pods.append({"name": "orders-api-new", "app": "orders-api", "phase": "Pending" if "Image" in bad else "Running",
                         "ready": False, "restarts": 4 if bad == "CrashLoopBackOff" else 0, "waiting_reasons": [bad],
                         "last_terminated_reasons": ["OOMKilled"] if bad == "CrashLoopBackOff" else []})
        return pods

    def warning_events(self, minutes=15):
        return []

    def logs(self, pod, lines=300):
        if float(self.template["env"].get("FAULT_ERROR_RATE", 0)) and not broken(self.template):
            return '{"level":"ERROR","msg":"payment dependency call failed: injected fault FAULT_ERROR_RATE"}'
        return '{"level":"INFO","msg":"ok"}'

    def revisions(self, deployment):
        return sorted(copy.deepcopy(self.revs), key=lambda r: r["revision"])

    # ---- ações ----
    def snapshot(self, deployment):
        return {"template": copy.deepcopy(self.template), "replicas": self.replicas}

    def restore(self, deployment, snap):
        self.actions.append(("restore",))
        self.replicas = snap["replicas"]
        if snap["template"] != self.template:
            self._new_revision(snap["template"])
        return "restaurado"

    def rollback(self, deployment):
        older = [r for r in self.revs if r["revision"] < self.revision]
        if not older:
            raise RuntimeError("sem revisão anterior")
        target = max(older, key=lambda r: r["revision"])
        number = target["revision"]
        self.actions.append(("rollback", number))
        self._new_revision({k: copy.deepcopy(target[k]) for k in GOOD})
        return f"rollback para o template da revisão {number}"

    def restart(self, deployment):
        self.actions.append(("restart",))
        return "restart"

    def scale(self, deployment, replicas):
        self.actions.append(("scale", replicas))
        self.replicas = replicas
        return f"scale {replicas}"

    def remove_env(self, deployment, name):
        self.actions.append(("remove_env", name))
        env = {k: v for k, v in self.template["env"].items() if k != name}
        self.apply_template(env=env)
        return f"removida {name}"

    def set_memory_limit(self, deployment, mi):
        self.actions.append(("set_memory_limit", mi))
        self.apply_template(memory_limit=f"{mi}Mi")
        return f"memória {mi}Mi"


class FakeProm:
    def __init__(self, cluster: FakeCluster):
        self.c = cluster

    def slis(self):
        if not self.c.replicas:
            return {"error_ratio_1m": None, "error_ratio_5m": None, "p95_latency_1m": None, "requests_per_second": 0.0}
        serving = self.c.template if not broken(self.c.template) else self.c.last_good()
        err = float(serving["env"].get("FAULT_ERROR_RATE", 0))
        p95 = 0.05 + int(serving["env"].get("FAULT_LATENCY_MS", 0)) / 1000
        return {"error_ratio_1m": err, "error_ratio_5m": err, "p95_latency_1m": p95, "requests_per_second": 10.0}


class RuleBasedPlanner:
    """Imita um bom LLM: segue o playbook a partir dos sinais."""

    def __init__(self):
        self.calls = 0

    def plan(self, alert, signals, evidence, history):
        self.calls += 1
        tried = [h["action"]["type"] for h in history]
        joined = " ".join(signals)
        if "RECENT CHANGE" in joined and "rollback" not in tried:
            action = {"type": "rollback", "deployment": "orders-api"}
        elif "scaled to 0" in joined:
            action = {"type": "scale", "deployment": "orders-api", "replicas": 2}
        else:
            action = {"type": "escalate", "reason": "no clear cause"}
        return ({"summary": "s", "root_cause": "r", "confidence": "high", "evidence": signals[:2], "action": action,
                 "expected_outcome": "", "risk": "low"}, {"inputTokens": 1000, "outputTokens": 100,
                                                          "latency_seconds": 0.8, "model": "fake"})


class ScriptedPlanner:
    """Devolve planos pré-definidos em sequência (para testar guardrails e retry)."""

    def __init__(self, *plans):
        self.plans = list(plans)

    def plan(self, alert, signals, evidence, history):
        p = self.plans.pop(0)
        return ({"summary": "s", "root_cause": "r", "evidence": [], "expected_outcome": "", "risk": "",
                 "confidence": "high", **p}, {"inputTokens": 500, "outputTokens": 50, "latency_seconds": 0.5, "model": "fake"})
