"""Gate do CI: toda expressão PromQL do projeto (alertas, dashboards, agente) precisa ser válida.

Um erro de sintaxe numa regra de alerta é um alerta que nunca dispara: falha silenciosa.
"""

import json
import pathlib
import re
import sys

import promql_parser
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
exprs = []
for doc in yaml.safe_load_all((ROOT / "k8s/observability/dashboards.yaml").read_text()):
    for board in map(json.loads, doc["data"].values()):
        for panel in board["panels"]:
            exprs += [(board["uid"], panel["title"], t["expr"]) for t in panel.get("targets", [])]
        exprs += [(board["uid"], a["name"], a["expr"]) for a in board["annotations"]["list"]]
for group in yaml.safe_load((ROOT / "k8s/orders/alerts.yaml").read_text())["spec"]["groups"]:
    exprs += [("alerts.yaml", r.get("alert") or r.get("record"), r["expr"]) for r in group["rules"]]
for line in (ROOT / "apps/sre-agent/agent/prom.py").read_text().splitlines():
    m = re.match(r"\s+\"(\w+)\": '(.*)',$", line)
    if m:
        exprs.append(("prom.py", m.group(1), m.group(2)))

bad = 0
for src, name, expr in exprs:
    try:
        promql_parser.parse(expr)
    except Exception as exc:  # noqa: BLE001
        bad += 1
        print(f"INVÁLIDA [{src}] {name}: {exc}")
print(f"{len(exprs)} expressões PromQL, {bad} inválidas")
sys.exit(1 if bad or not exprs else 0)
