"""Relatório do incidente em Markdown (pt-BR) + publicação opcional como issue no GitHub."""

import html
import logging
import re

import httpx

log = logging.getLogger("sre-agent.report")

OUTCOME = {
    "resolved": "RESOLVIDO automaticamente",
    "escalated": "ESCALADO para humano",
    "approval_required": "AGUARDANDO APROVAÇÃO humana",
    "dry_run": "DRY RUN (nada alterado)",
}


def _redact(text: str) -> str:
    text = re.sub(r"\b\d{12}\b", "<aws-account-id>", text)
    return re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<guid>", text)


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}"
    return "sem dados" if v is None else str(v)


def render_markdown(inc: dict) -> str:
    a = inc["alert"]
    last = inc["attempts"][-1] if inc["attempts"] else {}
    plan = last.get("plan", {})
    lines = [
        f"# Incidente {inc['id']}: {inc['alertname']}",
        "",
        f"**Resultado:** {OUTCOME.get(inc['outcome'], inc['outcome'])}  ",
        f"**Tempo do agente:** {inc.get('agent_seconds', '?')} s"
        + (f" · **MTTR (início do alerta até verificado):** {inc['mttr_seconds']} s" if inc.get("mttr_seconds") else ""),
        "",
        "## Resumo",
        f"- **Alerta:** `{inc['alertname']}`: {a.get('annotations', {}).get('summary', '')}",
        *([f"- **Runbook:** {a['annotations']['runbook_url']}"] if a.get("annotations", {}).get("runbook_url") else []),
        f"- **Diagnóstico:** {plan.get('summary', '')}",
        f"- **Causa raiz provável:** {plan.get('root_cause', '')}",
        f"- **Confiança:** {plan.get('confidence', '')}",
        "",
        "## Sinais determinísticos (calculados por código)",
        *[f"- {s}" for s in inc.get("initial_signals", [])],
        "",
        "## Tentativas",
    ]
    for at in inc["attempts"]:
        p = at["plan"]
        lines += [
            f"### Tentativa {at['n']}",
            f"- **Ação proposta pelo LLM:** `{p.get('action')}`",
            f"- **Raciocínio:** {p.get('root_cause', '')}",
            f"- **Resultado esperado:** {p.get('expected_outcome', '')} · **Risco:** {p.get('risk', '')}",
            f"- **Política:** `{at['decision']['verdict']}`: {'; '.join(at['decision']['reasons'])}",
        ]
        if at.get("review"):
            r = at["review"]
            lines.append(f"- **Revisor de segurança:** `{r['verdict']}`: {r['reason']}")
        if "result" in at:
            lines.append(f"- **Execução:** {at['result']}")
        if "verify" in at:
            v = at["verify"]
            lines.append(f"- **Verificação:** {'OK' if v['ok'] else 'FALHOU'} em {v['seconds']} s"
                         + (f" ({'; '.join(v['problems'])})" if v["problems"] else ""))
        if at.get("reverted"):
            lines.append(f"- **Reversão:** {at['reverted']}")
        lines.append("")
    if inc.get("conversation"):
        lines += ["## Conversa entre os agentes", "| Hora | De → Para | Mensagem |", "| --- | --- | --- |"]
        lines += [f"| {m['at'][11:19]} | {m['from']} → {m['to']} | {m['msg'].replace('|', '/')} |" for m in inc["conversation"]]
        lines.append("")
    pm = inc.get("postmortem")
    if pm:
        lines += ["## Postmortem (escrito pelo agente Documentador)", f"**Resumo:** {pm['resumo']}", "",
                  f"**Impacto:** {pm['impacto']}", "", f"**Causa raiz:** {pm['causa_raiz']}", "", "**5 porquês:**"]
        lines += [f"{i}. {w}" for i, w in enumerate(pm["cinco_porques"], 1)]
        if pm["o_que_funcionou"]:
            lines += ["", "**O que funcionou:**", *[f"- {w}" for w in pm["o_que_funcionou"]]]
        if pm["acoes"]:
            lines += ["", "| Ação de prevenção | Tipo |", "| --- | --- |", *[f"| {a['acao']} | {a['tipo']} |" for a in pm["acoes"]]]
        lines.append("")
    lines += ["## SLIs", "| SLI | Antes | Depois |", "| --- | --- | --- |"]
    before = inc.get("initial_slis", {})
    after = (last.get("verify") or {}).get("slis", {})
    for k in before:
        lines.append(f"| `{k}` | {_fmt(before.get(k))} | {_fmt(after.get(k))} |")
    lines += ["", "## Linha do tempo (UTC)", "| Hora | Evento |", "| --- | --- |"]
    lines += [f"| {t['at'][11:19]} | {t['event']} |" for t in inc["timeline"]]
    u = inc["usage"]
    if u.get("by_agent"):
        lines += ["", "| Agente | Chamadas | Tokens (entrada/saída) | Tempo de LLM | Custo |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {name} | {a['calls']} | {a['inputTokens']}/{a['outputTokens']} | {a['seconds']:.2f} s | US$ {a.get('cost_usd', 0):.6f} |"
                  for name, a in u["by_agent"].items()]
    lines += ["", f"<sub>Modelo: {u['model']} · tokens: {u['inputTokens']} entrada / {u['outputTokens']} saída · "
                  f"tempo de LLM: {u['llm_seconds']:.2f} s · custo estimado: US$ {u['cost_usd']:.6f}</sub>"]
    if inc["outcome"] != "resolved":
        lines += ["", "> **Ação humana necessária.** Revise o diagnóstico acima; o agente não alterou mais nada."]
    return _redact("\n".join(lines))


def render_index(incidents: list[dict]) -> str:
    rows = "".join(
        f"<tr><td><a href='/reports/{i['id']}'>{i['id']}</a></td><td>{html.escape(i['alertname'])}</td>"
        f"<td class='{i['outcome']}'>{OUTCOME.get(i['outcome'], i['outcome'])}</td>"
        f"<td>{html.escape(str((i['attempts'][-1]['plan'].get('action') if i['attempts'] else '')))}</td>"
        f"<td>{i.get('agent_seconds', '')}</td><td>{i.get('mttr_seconds', '')}</td>"
        f"<td>{i['usage']['inputTokens'] + i['usage']['outputTokens']}</td><td>{i['received_at'][11:19]}</td></tr>"
        for i in incidents
    )
    return f"""<!doctype html><html lang="pt-br"><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>Agente SRE: incidentes</title>
<style>body{{font-family:system-ui;margin:2rem;color:#1f2328}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #d0d7de;padding:.5rem;text-align:left;font-size:14px}}
.resolved{{color:#1a7f37;font-weight:600}}.escalated,.approval_required{{color:#cf222e;font-weight:600}}</style>
<h1>Agente SRE autônomo: incidentes</h1><p>Atualiza a cada 10 s. Clique no ID para o relatório completo.</p>
<table><tr><th>ID</th><th>Alerta</th><th>Resultado</th><th>Ação</th><th>Agente (s)</th><th>MTTR (s)</th><th>Tokens</th><th>Recebido (UTC)</th></tr>{rows}</table></html>"""


def publish_issue(inc: dict, body: str, settings) -> str | None:
    if not (settings.github_token and settings.github_repository):
        return None
    labels = ["incident", "ai-sre-agent", inc["outcome"]]
    title = _redact(f"[{OUTCOME.get(inc['outcome'], inc['outcome'])}] {inc['alertname']}: "
                    f"{(inc['attempts'][-1]['plan'].get('summary') if inc['attempts'] else '')}")[:200]
    try:
        r = httpx.post(f"https://api.github.com/repos/{settings.github_repository}/issues",
                       headers={"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"},
                       json={"title": title, "body": body, "labels": labels}, timeout=10)
        r.raise_for_status()
        return r.json()["html_url"]
    except Exception as exc:  # relatório nunca derruba o agente
        log.warning("falha ao abrir issue: %s", exc)
        return None
