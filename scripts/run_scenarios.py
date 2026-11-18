#!/usr/bin/env python3
"""Suíte de testes de caos do agente autônomo. Só usa a biblioteca padrão + kubectl.

Para cada cenário: injeta a falha, mede o MTTD (até o alerta disparar),
espera o agente agir, mede o MTTR (até o alerta resolver) e gera
reports/test-run-<data>.md com a tabela de resultados e o relatório de
cada incidente escrito pelo agente.

Uso:
  python3 scripts/run_scenarios.py                 # todos
  python3 scripts/run_scenarios.py api-errors oom  # só alguns
"""

import json
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

NS = "orders"
PROM = "http://localhost:19090"
AGENT = "http://localhost:18080"


def sh(cmd: str, check=True) -> str:
    # Sem shell: os argumentos vão direto ao kubectl (nada é interpretado por um shell)
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout.strip()


def image() -> str:
    return sh(f"kubectl -n {NS} get deploy orders-api -o jsonpath='{{.spec.template.spec.containers[0].image}}'")


SCENARIOS = {
    "api-errors": ("OrdersApiHighErrorRate", "50% de erros 5xx (deploy com FAULT_ERROR_RATE)",
                   lambda: sh(f"kubectl -n {NS} set env deploy/orders-api FAULT_ERROR_RATE=0.5")),
    "oom": ("OrdersApiPodsFailing", "limite de memória de 20Mi: OOMKilled / CrashLoopBackOff",
            lambda: sh(f"kubectl -n {NS} set resources deploy/orders-api -c app --limits=memory=20Mi --requests=memory=20Mi")),
    "scale-zero": ("OrdersApiNoReplicas", "deployment escalado para 0 réplicas (erro operacional)",
                   lambda: sh(f"kubectl -n {NS} scale deploy/orders-api --replicas=0")),
    "api-latency": ("OrdersApiHighLatency", "+600 ms de latência (deploy com FAULT_LATENCY_MS)",
                    lambda: sh(f"kubectl -n {NS} set env deploy/orders-api FAULT_LATENCY_MS=600")),
    "bad-image": ("OrdersApiPodsFailing", "deploy de uma tag de imagem que não existe: ImagePullBackOff",
                  lambda: sh(f"kubectl -n {NS} set image deploy/orders-api app={image().rsplit(':', 1)[0]}:does-not-exist")),
}


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def firing(alert: str) -> bool:
    q = urllib.parse.quote(f'ALERTS{{alertname="{alert}",alertstate="firing",namespace="{NS}"}}')
    return bool(get(f"{PROM}/api/v1/query?query={q}")["data"]["result"])


def any_firing() -> list[str]:
    q = urllib.parse.quote(f'ALERTS{{alertstate="firing",agent="sre"}}')
    return [r["metric"]["alertname"] for r in get(f"{PROM}/api/v1/query?query={q}")["data"]["result"]]


def wait(cond, timeout: int, label: str) -> int | None:
    start = time.time()
    while time.time() - start < timeout:
        if cond():
            return round(time.time() - start)
        print(".", end="", flush=True)
        time.sleep(10)
    print(f" timeout ({label})")
    return None


def manual_recovery():
    """Se o agente escalou, o 'humano' (este script) limpa para o próximo cenário."""
    sh(f"kubectl -n {NS} scale deploy/orders-api --replicas=2")
    sh(f"kubectl -n {NS} set env deploy/orders-api FAULT_ERROR_RATE- FAULT_LATENCY_MS-", check=False)
    sh(f"kubectl -n {NS} set resources deploy/orders-api -c app --limits=memory=256Mi --requests=memory=96Mi", check=False)
    sh(f"kubectl -n {NS} rollout status deploy/orders-api --timeout=180s", check=False)


def run(name: str) -> dict:
    alert, description, inject = SCENARIOS[name]
    print(f"\n=== {name}: {description}\n    esperando alerta {alert}")
    if any_firing():
        print("    aguardando alertas anteriores resolverem", end="")
        wait(lambda: not any_firing(), 600, "alertas antigos")
    known = {i["id"] for i in get(f"{AGENT}/reports")}
    t0 = time.time()
    inject()
    print("    falha injetada; detectando", end="")
    mttd = wait(lambda: firing(alert), 420, "detecção")
    print(f"\n    MTTD: {mttd}s; agente investigando", end="")
    new = []

    def agent_done():
        new[:] = [i for i in get(f"{AGENT}/reports") if i["id"] not in known]
        return bool(new)
    wait(agent_done, 600, "agente")
    inc = new[0] if new else {}
    outcome = inc.get("outcome", "sem resposta")
    print(f"\n    agente: {outcome}; esperando alerta resolver", end="")
    if outcome != "resolved":
        manual_recovery()
    resolved = wait(lambda: not firing(alert), 600, "recuperação")
    mttr = round(time.time() - t0) if resolved is not None else None
    print(f"\n    MTTR (injeção -> alerta resolvido): {mttr}s")
    action = inc["attempts"][-1]["plan"]["action"] if inc.get("attempts") else {}
    md = ""
    if inc:
        with urllib.request.urlopen(f"{AGENT}/reports/{inc['id']}", timeout=10) as r:
            md = r.read().decode()
    time.sleep(60)  # respiro entre cenários
    return {"scenario": name, "description": description, "alert": alert, "mttd": mttd, "mttr": mttr,
            "outcome": outcome, "action": action, "attempts": len(inc.get("attempts", [])),
            "agent_seconds": inc.get("agent_seconds"), "usage": inc.get("usage", {}), "report_md": md}


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    pf = [subprocess.Popen(c.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in (
        "kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 19090:9090",
        "kubectl -n sre-agent port-forward svc/sre-agent 18080:8080")]
    try:
        time.sleep(4)
        results = [run(n) for n in names]
    finally:
        for p in pf:
            p.terminate()

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = pathlib.Path("reports") / f"test-run-{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    ok = sum(r["outcome"] == "resolved" for r in results)
    tokens = sum(r["usage"].get("inputTokens", 0) + r["usage"].get("outputTokens", 0) for r in results)
    cost = sum(r["usage"].get("cost_usd", 0) for r in results)
    lines = [
        f"# Rodada de testes do agente SRE autônomo ({datetime.now():%d/%m/%Y %H:%M})", "",
        f"**{ok} de {len(results)} incidentes resolvidos sem intervenção humana.** "
        f"Tokens: {tokens} · custo de LLM: US$ {cost:.5f}", "",
        "| Cenário | Falha injetada | Alerta | MTTD (s) | Ação do agente | Tentativas | Resultado | MTTR (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        act = r["action"].get("type", "-") + (f" {r['action'].get('name') or r['action'].get('replicas') or r['action'].get('memory_mi') or ''}".rstrip())
        lines.append(f"| {r['scenario']} | {r['description']} | `{r['alert']}` | {r['mttd']} | `{act}` | {r['attempts']} | {r['outcome']} | {r['mttr']} |")
    lines += ["", "MTTD: da injeção ao alerta disparar. MTTR: da injeção ao alerta resolver.", ""]
    for r in results:
        lines += ["---", "", r["report_md"].replace("# Incidente", f"## [{r['scenario']}] Incidente"), ""]
    out.write_text("\n".join(lines))
    print(f"\nRelatório: {out}")


if __name__ == "__main__":
    main()
