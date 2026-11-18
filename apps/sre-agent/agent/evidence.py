"""Coleta evidências (só leitura) e calcula sinais determinísticos.

Lição do projeto AWS: o modelo errou o diagnóstico quando a causa estava
enterrada num JSON de 20 KB. Aqui o código calcula primeiro o que um
engenheiro olharia antes de tudo, principalmente "o que mudou?".
"""

from datetime import datetime, timezone

CRITICAL_WAITING = ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError")


def condense_log(raw: str, errors: int = 10, tail: int = 5) -> str:
    lines = raw.splitlines()
    err = [line for line in lines if "ERROR" in line or "Traceback" in line][-errors:]
    recent = [line for line in lines[-tail:] if line not in err]
    return "\n".join(err + recent)[-3000:]


def diff_revisions(prev: dict, cur: dict) -> list[str]:
    changes = []
    if prev["image"] != cur["image"]:
        changes.append(f"image: {prev['image']} -> {cur['image']}")
    for key in sorted(set(prev["env"]) | set(cur["env"])):
        a, b = prev["env"].get(key), cur["env"].get(key)
        if a != b:
            changes.append(f"env {key}: {a if a is not None else '(unset)'} -> {b if b is not None else '(unset)'}")
    if prev.get("memory_limit") != cur.get("memory_limit"):
        changes.append(f"memory limit: {prev.get('memory_limit')} -> {cur.get('memory_limit')}")
    return changes


def recent_changes(cluster, deployments: list[dict], targets: list[str], remediated=frozenset()) -> list[dict]:
    out = []
    for d in deployments:
        if d["name"] not in targets:
            continue
        revs = cluster.revisions(d["name"])
        current = next((r for r in revs if r["revision"] == d["revision"]), None)
        older = [r for r in revs if r["revision"] < d["revision"]]
        if not current or not older:
            continue
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(current["created"])).total_seconds() / 60
        out.append({
            "deployment": d["name"],
            "current_revision": current["revision"],
            "previous_revision": older[-1]["revision"],
            "minutes_since_change": round(age, 1),
            "changes": diff_revisions(older[-1], current),
            # revisão criada pelo próprio agente ao remediar: a anterior é sabidamente ruim
            "current_is_agent_remediation": (d["name"], current["revision"]) in remediated,
        })
    return out


def collect(cluster, prom, settings, remediated=frozenset()) -> dict:
    deployments = cluster.deployments()
    pods = cluster.pods()
    unhealthy = [p["name"] for p in pods if not p["ready"] or p["waiting_reasons"]]
    sample = unhealthy[:2] + [p["name"] for p in pods if p["name"] not in unhealthy][:1]
    return {
        "slis": prom.slis(),
        "deployments": deployments,
        "pods": pods,
        "recent_changes": recent_changes(cluster, deployments, settings.target_deployments, remediated),
        "warning_events": cluster.warning_events(),
        "logs": {name: condense_log(cluster.logs(name)) for name in sample},
    }


def detect_signals(ev: dict, settings) -> list[str]:
    """Fatos calculados por código. Vão primeiro no prompt."""
    signals = []
    for rc in ev.get("recent_changes", []):
        if rc.get("current_is_agent_remediation"):
            signals.append(
                f"Deployment {rc['deployment']} revision {rc['current_revision']} is a FIX previously applied by this agent; "
                f"revision {rc['previous_revision']} is known bad. Do NOT roll back: the cause is something else."
            )
        elif rc["changes"] and rc["minutes_since_change"] <= 60:
            signals.append(
                f"RECENT CHANGE: deployment {rc['deployment']} revision {rc['current_revision']} was rolled out "
                f"{rc['minutes_since_change']} min ago, changing: {'; '.join(rc['changes'])}"
            )
    for d in ev.get("deployments", []):
        for key, value in d["env"].items():
            if key.startswith("FAULT_") and value not in ("", "0", "0.0"):
                signals.append(f"Deployment {d['name']} has fault injection enabled: {key}={value}")
        if d["replicas_desired"] == 0:
            signals.append(f"Deployment {d['name']} is scaled to 0 replicas: nothing is serving traffic")
        elif d["replicas_ready"] < d["replicas_desired"]:
            signals.append(f"Deployment {d['name']} has {d['replicas_ready']}/{d['replicas_desired']} replicas ready")
    for p in ev.get("pods", []):
        for reason in p["waiting_reasons"]:
            if reason in CRITICAL_WAITING:
                signals.append(f"Pod {p['name']} is waiting: {reason}")
        if "OOMKilled" in p.get("last_terminated_reasons", []):
            signals.append(f"Pod {p['name']} was OOMKilled (container exceeded its memory limit)")
    slis = ev.get("slis", {})
    if (slis.get("error_ratio_1m") or 0) > settings.healthy_error_ratio:
        signals.append(f"Error ratio (1m) is {slis['error_ratio_1m']:.1%}")
    if (slis.get("p95_latency_1m") or 0) > settings.healthy_p95_seconds:
        signals.append(f"p95 latency (1m) is {slis['p95_latency_1m']:.2f}s")
    for pod, text in ev.get("logs", {}).items():
        errs = [line for line in text.splitlines() if "ERROR" in line]
        if errs:
            signals.append(f"{len(errs)} error lines in {pod} logs, latest: {errs[-1][:200]}")
    return signals
