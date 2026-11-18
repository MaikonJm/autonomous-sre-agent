"""Acesso ao Kubernetes. Leituras normalizadas em dicts + as ações permitidas.

O RBAC do agente (k8s/sre-agent.yaml) só dá escrita em Deployments do
namespace orders. Mesmo que o código tivesse um bug, o cluster recusaria
qualquer outra coisa: defesa em profundidade.
"""

import copy
import re
from datetime import datetime, timedelta, timezone

from kubernetes import client, config

REVISION = "deployment.kubernetes.io/revision"


def _mi(value: str | None) -> int | None:
    """'256Mi' -> 256, '1Gi' -> 1024, '300M' -> 286."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)(Mi|Gi|M|G|Ki)?", str(value))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2) or ""
    return {"Mi": n, "Gi": n * 1024, "M": int(n * 0.9537), "G": int(n * 953.7), "Ki": n // 1024, "": n // (1024 * 1024)}[unit]


def summarize_template(template) -> dict:
    c = template.spec.containers[0]
    limits = (c.resources.limits or {}) if c.resources else {}
    return {
        "image": c.image,
        # só env literal: valores vindos de Secret (secretKeyRef) nunca são lidos
        "env": {e.name: e.value for e in (c.env or []) if e.value is not None},
        "memory_limit": limits.get("memory"),
    }


class KubeCluster:
    def __init__(self, namespace: str):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.ns = namespace
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    # ------------------------------------------------------------------ leitura
    def deployments(self) -> list[dict]:
        out = []
        for d in self.apps.list_namespaced_deployment(self.ns).items:
            s = d.status
            out.append({
                "name": d.metadata.name,
                "revision": int((d.metadata.annotations or {}).get(REVISION, "0")),
                "replicas_desired": d.spec.replicas,
                "replicas_ready": s.ready_replicas or 0,
                "replicas_updated": s.updated_replicas or 0,
                "replicas_available": s.available_replicas or 0,
                **summarize_template(d.spec.template),
            })
        return out

    def pods(self) -> list[dict]:
        out = []
        for p in self.core.list_namespaced_pod(self.ns).items:
            statuses = p.status.container_statuses or []
            out.append({
                "name": p.metadata.name,
                "app": (p.metadata.labels or {}).get("app", ""),
                "phase": p.status.phase,
                "ready": bool(statuses) and all(cs.ready for cs in statuses),
                "restarts": sum(cs.restart_count for cs in statuses),
                "waiting_reasons": [cs.state.waiting.reason for cs in statuses if cs.state and cs.state.waiting],
                "last_terminated_reasons": [
                    cs.last_state.terminated.reason for cs in statuses
                    if cs.last_state and cs.last_state.terminated
                ],
            })
        return out

    def warning_events(self, minutes: int = 15) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        rows = []
        for e in self.core.list_namespaced_event(self.ns).items:
            when = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
            if e.type == "Warning" and when and when >= cutoff:
                rows.append({"at": when.isoformat(), "reason": e.reason, "object": e.involved_object.name,
                             "message": (e.message or "")[:300], "count": e.count})
        return sorted(rows, key=lambda r: r["at"])[-15:]

    def logs(self, pod: str, lines: int = 300) -> str:
        try:
            return self.core.read_namespaced_pod_log(pod, self.ns, tail_lines=lines)
        except client.ApiException as exc:
            return f"<logs unavailable: {exc.reason}>"

    def _replicasets(self, deployment: str):
        d = self.apps.read_namespaced_deployment(deployment, self.ns)
        owned = [
            rs for rs in self.apps.list_namespaced_replica_set(self.ns).items
            if any(o.uid == d.metadata.uid for o in (rs.metadata.owner_references or []))
        ]
        return d, sorted(owned, key=lambda rs: int((rs.metadata.annotations or {}).get(REVISION, "0")))

    def revisions(self, deployment: str) -> list[dict]:
        _, rss = self._replicasets(deployment)
        return [{
            "revision": int((rs.metadata.annotations or {}).get(REVISION, "0")),
            "created": rs.metadata.creation_timestamp.isoformat(),
            **summarize_template(rs.spec.template),
        } for rs in rss]

    # ------------------------------------------------------------------ ações
    def snapshot(self, deployment: str) -> dict:
        d = self.apps.read_namespaced_deployment(deployment, self.ns)
        return {"template": copy.deepcopy(d.spec.template), "replicas": d.spec.replicas}

    def restore(self, deployment: str, snap: dict) -> str:
        d = self.apps.read_namespaced_deployment(deployment, self.ns)
        d.spec.template = snap["template"]
        d.spec.replicas = snap["replicas"]
        self.apps.replace_namespaced_deployment(deployment, self.ns, d)
        return "estado anterior à ação restaurado"

    def rollback(self, deployment: str) -> str:
        """Mesmo efeito de `kubectl rollout undo`: volta o template da revisão anterior."""
        d, rss = self._replicasets(deployment)
        current = int((d.metadata.annotations or {}).get(REVISION, "0"))
        previous = [rs for rs in rss if int((rs.metadata.annotations or {}).get(REVISION, "0")) < current]
        if not previous:
            raise RuntimeError("não há revisão anterior para rollback")
        target = previous[-1]
        template = copy.deepcopy(target.spec.template)
        template.metadata.labels.pop("pod-template-hash", None)
        d.spec.template = template
        self.apps.replace_namespaced_deployment(deployment, self.ns, d)
        return f"rollback da revisão {current} para o template da revisão {target.metadata.annotations[REVISION]}"

    def restart(self, deployment: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        self.apps.patch_namespaced_deployment(deployment, self.ns, body)
        return "rollout restart"

    def scale(self, deployment: str, replicas: int) -> str:
        self.apps.patch_namespaced_deployment_scale(deployment, self.ns, {"spec": {"replicas": replicas}})
        return f"escalado para {replicas} réplicas"

    def remove_env(self, deployment: str, name: str) -> str:
        d = self.apps.read_namespaced_deployment(deployment, self.ns)
        c = d.spec.template.spec.containers[0]
        before = len(c.env or [])
        c.env = [e for e in (c.env or []) if e.name != name]
        if len(c.env) == before:
            raise RuntimeError(f"variável {name} não existe no deployment")
        self.apps.replace_namespaced_deployment(deployment, self.ns, d)
        return f"variável {name} removida"

    def set_memory_limit(self, deployment: str, mi: int) -> str:
        d = self.apps.read_namespaced_deployment(deployment, self.ns)
        c = d.spec.template.spec.containers[0]
        c.resources = c.resources or client.V1ResourceRequirements()
        c.resources.limits = {**(c.resources.limits or {}), "memory": f"{mi}Mi"}
        requests = c.resources.requests or {}
        if _mi(requests.get("memory")) and _mi(requests.get("memory")) > mi:
            c.resources.requests = {**requests, "memory": f"{mi}Mi"}
        self.apps.replace_namespaced_deployment(deployment, self.ns, d)
        return f"limite de memória ajustado para {mi}Mi"
