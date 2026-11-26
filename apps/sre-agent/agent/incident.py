"""Orquestrador do time de agentes: investigar -> diagnosticar -> revisar -> decidir -> agir -> verificar -> documentar.

    alerta
      -> evidências + sinais (código)
      -> plano (LLM, ação tipada)
      -> política (código): execute | approval_required | escalate | dry_run
      -> ação (snapshot antes)
      -> verificação (SLIs + saúde do rollout)
            ok      -> resolvido
            piorou  -> restaura o snapshot
            não ok  -> nova tentativa com o histórico (até MAX_ATTEMPTS) -> escalonar
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from . import evidence as ev_mod
from .policy import Decision

log = logging.getLogger("sre-agent.incident")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_action(a: dict) -> str:
    extra = a.get("name") or a.get("replicas") or a.get("memory_mi") or a.get("reason") or ""
    return " ".join(str(x) for x in (a.get("type", "?"), a.get("deployment", ""), extra) if x)


def is_healthy(evidence: dict, deployment: str, s) -> tuple[bool, list[str]]:
    problems = []
    d = next((d for d in evidence["deployments"] if d["name"] == deployment), None)
    if not d or not d["replicas_desired"]:
        problems.append("nenhuma réplica desejada")
    else:
        if d["replicas_ready"] < d["replicas_desired"]:
            problems.append(f"{d['replicas_ready']}/{d['replicas_desired']} réplicas prontas")
        if d["replicas_updated"] < d["replicas_desired"]:
            problems.append("rollout ainda não concluído")
    waiting = [p["name"] for p in evidence["pods"] if p["app"] == deployment and p["waiting_reasons"]]
    if waiting:
        problems.append(f"pods aguardando: {waiting}")
    err = evidence["slis"].get("error_ratio_1m")
    if err is not None and err > s.healthy_error_ratio:
        problems.append(f"taxa de erro 1m {err:.1%}")
    p95 = evidence["slis"].get("p95_latency_1m")
    if p95 is not None and p95 > s.healthy_p95_seconds:
        problems.append(f"p95 1m {p95:.2f}s")
    return not problems, problems


def is_worse(before: dict, after: dict) -> bool:
    def ready(ev):
        return sum(d["replicas_ready"] for d in ev["deployments"])
    eb, ea = before["slis"].get("error_ratio_1m") or 0, after["slis"].get("error_ratio_1m") or 0
    return ea > eb + 0.10 or ready(after) < ready(before)


class IncidentRunner:
    def __init__(self, cluster, prom, planner, policy, settings, sleep=time.sleep, clock=time.time,
                 reviewer=None, scribe=None):
        self.cluster, self.prom, self.planner, self.policy, self.s = cluster, prom, planner, policy, settings
        self.reviewer, self.scribe = reviewer, scribe  # agentes opcionais do time (None = desligado)
        self.sleep, self.clock = sleep, clock
        self.remediated: set[tuple[str, int]] = set()  # (deployment, revisão) criadas pelo agente

    def _collect(self):
        evidence = ev_mod.collect(self.cluster, self.prom, self.s, frozenset(self.remediated))
        return evidence, ev_mod.detect_signals(evidence, self.s)

    def _execute(self, action: dict) -> str:
        name = action["deployment"]
        kind = action["type"]
        if kind == "rollback":
            return self.cluster.rollback(name)
        if kind == "restart":
            return self.cluster.restart(name)
        if kind == "scale":
            return self.cluster.scale(name, action["replicas"])
        if kind == "remove_env":
            return self.cluster.remove_env(name, action["name"])
        if kind == "set_memory_limit":
            return self.cluster.set_memory_limit(name, action["memory_mi"])
        raise ValueError(kind)

    def _verify(self, deployment: str) -> dict:
        start = self.clock()
        problems = []
        evidence = None
        while self.clock() - start <= self.s.verify_timeout_seconds:
            self.sleep(self.s.verify_interval_seconds)
            evidence, _ = self._collect()
            ok, problems = is_healthy(evidence, deployment, self.s)
            if ok:
                return {"ok": True, "seconds": round(self.clock() - start), "slis": evidence["slis"], "problems": []}
        return {"ok": False, "seconds": round(self.clock() - start), "slis": evidence["slis"] if evidence else {},
                "problems": problems, "evidence": evidence}

    def run(self, alert: dict) -> dict:
        inc = {
            "id": uuid.uuid4().hex[:8],
            "alertname": alert.get("labels", {}).get("alertname", "unknown"),
            "alert": alert,
            "severity": self.policy.severity(alert),
            "alert_started_at": alert.get("startsAt"),
            "received_at": _now(),
            "attempts": [],
            "timeline": [],
            "conversation": [],   # mensagens entre os agentes do time
            "outcome": None,
            "usage": {"inputTokens": 0, "outputTokens": 0, "llm_seconds": 0.0, "model": "", "by_agent": {}},
        }
        t0 = self.clock()

        def mark(event: str):
            inc["timeline"].append({"at": _now(), "event": event})
            log.info("[%s] %s", inc["id"], event)

        def say(frm: str, to: str, msg: str):
            inc["conversation"].append({"at": _now(), "from": frm, "to": to, "msg": msg})

        def account(agent: str, usage: dict, ok: bool):
            for k in ("inputTokens", "outputTokens"):
                inc["usage"][k] += usage.get(k, 0)
            inc["usage"]["llm_seconds"] += usage.get("latency_seconds", 0)
            inc["usage"]["model"] = usage.get("model", inc["usage"]["model"])
            a = inc["usage"]["by_agent"].setdefault(agent, {"calls": 0, "errors": 0, "inputTokens": 0, "outputTokens": 0,
                                                             "seconds": 0.0, "latencies": []})
            a["calls"] += 1
            a["errors"] += 0 if ok else 1
            a["inputTokens"] += usage.get("inputTokens", 0)
            a["outputTokens"] += usage.get("outputTokens", 0)
            a["seconds"] += usage.get("latency_seconds", 0)
            if usage.get("latency_seconds"):
                a["latencies"].append(usage["latency_seconds"])

        mark(f"alerta {inc['alertname']} recebido, iniciando investigação")
        say("alertmanager", "investigador", f"{inc['alertname']}: {alert.get('annotations', {}).get('summary', '')}")
        history = []
        for n in range(1, self.s.max_attempts + 1):
            # --- Investigador (código) ---
            evidence, signals = self._collect()
            if n == 1:
                inc["initial_signals"], inc["initial_slis"] = signals, evidence["slis"]
            mark(f"tentativa {n}: {len(signals)} sinais coletados")
            say("investigador", "diagnosticador",
                f"{len(signals)} sinais" + (f"; o principal: {signals[0][:220]}" if signals else ": nenhum sinal claro"))

            # --- Diagnosticador (LLM) ---
            try:
                plan, usage = self.planner.plan(alert, signals, evidence, history)
                account("diagnosticador", usage, True)
            except Exception as exc:
                log.exception("LLM indisponível")
                plan = {"summary": "LLM indisponível", "root_cause": str(exc)[:300], "confidence": "low", "evidence": [],
                        "action": {"type": "escalate", "reason": f"LLM indisponível: {exc}"}, "expected_outcome": "", "risk": ""}
                usage = {}
                account("diagnosticador", usage, False)
            mark(f"diagnóstico ({plan['confidence']}): {plan['summary']}")
            say("diagnosticador", "revisor" if self.reviewer else "politica",
                f"causa provável: {str(plan.get('root_cause', ''))[:220]} · proponho {fmt_action(plan['action'])} (confiança {plan['confidence']})")

            # --- Revisor de segurança (LLM): só pode deixar a decisão MAIS conservadora ---
            review = None
            if self.reviewer and plan["action"].get("type") != "escalate":
                try:
                    review, rusage = self.reviewer.review(plan, signals)
                    account("revisor", rusage, True)
                except Exception as exc:
                    log.exception("revisor indisponível")
                    review = {"verdict": "reject", "reason": f"revisor indisponível ({exc}): falha segura", "concerns": []}
                    account("revisor", {}, False)
                say("revisor", "politica", f"{ {'approve': 'aprovo', 'reject': 'rejeito', 'abstain': 'me abstenho'}[review['verdict']] }: {review['reason'][:220]}")
                mark(f"revisor: {review['verdict']} ({review['reason'][:160]})")

            # --- Política (código): decisão final ---
            if review and review["verdict"] == "reject":
                decision = Decision("escalate", [f"o agente Revisor rejeitou o plano: {review['reason']}"])
            else:
                decision = self.policy.evaluate(plan, evidence, inc["attempts"], alert=alert)
            attempt = {"n": n, "signals": signals, "slis_before": evidence["slis"], "plan": plan, "review": review,
                       "decision": {"verdict": decision.verdict, "reasons": decision.reasons},
                       "usage": {k: usage.get(k) for k in ("inputTokens", "outputTokens", "latency_seconds")},
                       "llm_ok": bool(usage)}
            inc["attempts"].append(attempt)
            mark(f"política: {decision.verdict} ({'; '.join(decision.reasons)})")
            say("politica", "executor" if decision.verdict == "execute" else "humanos",
                f"{decision.verdict}: {'; '.join(decision.reasons)[:220]}")

            if decision.verdict != "execute":
                inc["outcome"] = {"escalate": "escalated", "approval_required": "approval_required",
                                  "dry_run": "dry_run"}[decision.verdict]
                break

            # --- Executor (código) ---
            action = plan["action"]
            snapshot = self.cluster.snapshot(action["deployment"])
            try:
                attempt["result"] = self._execute(action)
                self.policy.record_execution()
                mark(f"ação executada: {attempt['result']}")
                say("executor", "verificador", f"feito: {attempt['result']} (snapshot guardado para reverter)")
            except Exception as exc:
                attempt["result"] = f"falhou: {exc}"
                mark(f"ação falhou: {exc}")
                say("executor", "diagnosticador", f"a ação falhou: {exc}")
                history.append({"action": action, "result": attempt["result"]})
                continue

            # --- Verificador (código) ---
            verify = self._verify(action["deployment"])
            attempt["verify"] = {k: v for k, v in verify.items() if k != "evidence"}
            if verify["ok"] and action["type"] in ("rollback", "remove_env", "set_memory_limit"):
                # lido depois do rollout: o controller atualiza o número da revisão de forma assíncrona
                rev = next((d["revision"] for d in self.cluster.deployments() if d["name"] == action["deployment"]), None)
                if rev:
                    self.remediated.add((action["deployment"], rev))
            if verify["ok"]:
                mark(f"verificação OK em {verify['seconds']}s: serviço saudável")
                say("verificador", "documentador" if self.scribe else "humanos",
                    f"SLO restabelecido em {verify['seconds']}s: rollout completo, erro e latência dentro do alvo")
                inc["outcome"] = "resolved"
                break
            mark(f"verificação falhou após {verify['seconds']}s: {verify['problems']}")
            say("verificador", "diagnosticador", f"não resolveu: {'; '.join(verify['problems'])[:200]}")
            if verify.get("evidence") and is_worse(evidence, verify["evidence"]):
                attempt["reverted"] = self.cluster.restore(action["deployment"], snapshot)
                mark("a ação piorou o serviço: snapshot restaurado")
                say("verificador", "executor", "piorou: snapshot restaurado")
            history.append({"action": action, "result": attempt["result"], "after": verify["problems"]})

        if inc["outcome"] is None:
            inc["outcome"] = "escalated"
            mark("tentativas esgotadas: escalando para humano")
        inc["finished_at"] = _now()
        if inc["outcome"] == "resolved" and inc["alert_started_at"]:
            try:
                started = datetime.fromisoformat(inc["alert_started_at"].replace("Z", "+00:00"))
                mttr = round((datetime.now(timezone.utc) - started).total_seconds())
                if mttr >= 0:
                    inc["mttr_seconds"] = mttr
            except ValueError:
                pass
        inc["agent_seconds"] = round(self.clock() - t0)  # MTTR e tempo do agente medidos ANTES do postmortem

        # --- Documentador (LLM): postmortem; nunca bloqueia nem altera o resultado ---
        if self.scribe:
            facts = {
                "alerta": inc["alertname"], "resumo_alerta": alert.get("annotations", {}).get("summary", ""),
                "resultado": inc["outcome"], "mttr_segundos": inc.get("mttr_seconds"),
                "sinais": inc.get("initial_signals", []), "slis_antes": inc.get("initial_slis", {}),
                "tentativas": [{"acao": a["plan"].get("action"), "causa": a["plan"].get("root_cause"),
                                "revisor": (a.get("review") or {}).get("verdict"), "politica": a["decision"],
                                "execucao": a.get("result"), "verificacao": a.get("verify")} for a in inc["attempts"]],
                "linha_do_tempo": inc["timeline"],
            }
            try:
                inc["postmortem"], susage = self.scribe.write(facts)
                account("documentador", susage, True)
                say("documentador", "humanos", "postmortem escrito: " + inc["postmortem"]["resumo"][:200])
            except Exception as exc:
                log.warning("documentador falhou: %s", exc)
                account("documentador", {}, False)
                say("documentador", "humanos", "não consegui escrever o postmortem; o relatório técnico abaixo está completo")

        u = inc["usage"]
        u["cost_usd"] = round(u["inputTokens"] / 1e6 * self.s.price_input_per_m
                              + u["outputTokens"] / 1e6 * self.s.price_output_per_m, 6)
        for a in u["by_agent"].values():
            a["cost_usd"] = round(a["inputTokens"] / 1e6 * self.s.price_input_per_m
                                  + a["outputTokens"] / 1e6 * self.s.price_output_per_m, 6)
        return inc
