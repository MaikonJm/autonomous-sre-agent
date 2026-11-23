"""Gera as imagens do projeto (capa, arquitetura, time de agentes) em SVG; o PNG sai com rsvg-convert."""
import pathlib

OUT = pathlib.Path(__file__).parent  # PNG: rsvg-convert -f png -o x.png x.svg
OUT.mkdir(exist_ok=True)

BG, PANEL, PANEL2, STROKE = "#0B1020", "#131A2E", "#18213A", "#2A3557"
TXT, MUTED = "#E6EAF2", "#8B95B0"
ORANGE, TEAL, PURPLE, BLUE, GREEN, RED = "#FF9900", "#2DD4BF", "#A78BFA", "#60A5FA", "#34D399", "#F87171"
FONT = "Inter, DejaVu Sans, sans-serif"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="{FONT}">\n<defs>\n'
            + "".join(f'<marker id="a{n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                      f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>\n'
                      for n, c in (("m", MUTED), ("o", ORANGE), ("t", TEAL), ("b", BLUE), ("g", GREEN), ("p", PURPLE)))
            + f'</defs>\n<rect width="{w}" height="{h}" fill="{BG}"/>\n{body}\n</svg>\n')


def text(x, y, s, size=14, fill=TXT, weight=400, anchor="start", italic=False):
    st = ' font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
            f'text-anchor="{anchor}"{st}>{esc(s)}</text>\n')


def box(x, y, w, h, fill=PANEL, stroke=STROKE, r=12, dash=False, sw=1.5):
    d = ' stroke-dasharray="6 5"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>\n'


def arrow(x1, y1, x2, y2, color=MUTED, m="m", dash=False, w=2):
    d = ' stroke-dasharray="6 5"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}"{d} '
            f'marker-end="url(#a{m})"/>\n')


def path(d, color=MUTED, m="m", dash=False, w=2):
    ds = ' stroke-dasharray="6 5"' if dash else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w}"{ds} marker-end="url(#a{m})"/>\n'


def pill(x, y, s, color, size=12):
    w = int(len(s) * size * 0.5) + 24
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{size + 10}" rx="{(size + 10) / 2}" fill="{color}" fill-opacity="0.16" '
            f'stroke="{color}" stroke-opacity="0.6"/>\n' + text(x + w / 2, y + size + 3, s, size, color, 600, "middle"))


def component(x, y, w, h, title, sub, accent):
    out = box(x, y, w, h, PANEL2, STROKE, 10)
    out += f'<rect x="{x}" y="{y + 10}" width="4" height="{h - 20}" rx="2" fill="{accent}"/>\n'
    out += text(x + 16, y + 26, title, 15, TXT, 600)
    for i, line in enumerate(sub):
        out += text(x + 16, y + 46 + i * 18, line, 12.5, MUTED)
    return out


def num(cx, cy, n, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="11" fill="{BG}" stroke="{color}" stroke-width="2"/>\n'
            + text(cx, cy + 5, n, 13, color, 700, "middle"))


# ------------------------------------------------------------------ arquitetura
def architecture():
    W, H = 1600, 1040
    b = text(40, 58, "Arquitetura", 30, TXT, 700)
    b += text(40, 86, "Tudo criado com Terraform · nenhuma porta de entrada aberta · custo de laboratório", 15, MUTED)

    # fora da AWS
    b += component(30, 190, 260, 92, "Você (operador)", ["navegador em localhost", "terminal no nó via SSM"], ORANGE)
    b += component(30, 360, 260, 200, "GitHub Actions", ["CI (gate): 32 testes, terraform", "validate, kubeconform, PromQL,",
                                                          "Semgrep, Trivy", "", "Release: build → ECR →", "assina com AWS Signer → verify"], GREEN)

    # AWS
    b += box(320, 120, 1250, 850, "none", ORANGE, 18, sw=1.5)
    b += pill(340, 132, "AWS · us-east-1", ORANGE, 13)
    svc = [("SSM Session Manager", ["terminal e túneis", "sem SSH, sem IP aberto"], ORANGE),
           ("Amazon ECR", ["tags imutáveis", "scan on push"], ORANGE),
           ("AWS Signer", ["assinatura das imagens", "(Notation)"], ORANGE),
           ("Amazon Bedrock", ["Nova Micro", "só InvokeModel em 1 modelo"], PURPLE),
           ("IAM", ["role do nó (IMDSv2)", "role do GitHub via OIDC"], ORANGE),
           ("AWS Budgets", ["alertas de custo", "50 / 80 / 100%"], ORANGE)]
    for i, (t, s, c) in enumerate(svc):
        b += component(350, 190 + i * 125, 270, 92, t, s, c)

    # VPC / EC2
    b += box(650, 170, 900, 780, "none", STROKE, 16, dash=True)
    b += text(670, 196, "VPC 10.20.0.0/16 · subnet pública sem NAT · Security Group sem regra de entrada", 13, MUTED)
    b += box(675, 212, 850, 720, PANEL, STROKE, 14)
    b += text(695, 240, "EC2 c7i-flex.large · k3s (Kubernetes de 1 nó) · disco criptografado", 15, TXT, 600)

    def ns(x, y, w, h, name, color):
        return box(x, y, w, h, "none", color, 12, dash=True) + text(x + 14, y + 22, f"namespace {name}", 12.5, color, 600)

    # loadtest + orders (coluna esquerda), monitoring (direita)
    b += ns(700, 262, 390, 90, "loadtest", MUTED)
    b += text(714, 312, "loadgen", 15, TXT, 600) + text(714, 332, "~10 req/s contra a API (sem tráfego, sem SLI)", 12.5, MUTED)
    b += ns(700, 372, 390, 200, "orders", BLUE)
    b += component(716, 404, 358, 74, "orders-api (2 réplicas)", ["FastAPI · métricas RED · falhas injetáveis"], BLUE)
    b += component(716, 488, 358, 70, "NetworkPolicy default-deny", ["só loadtest, Prometheus e probes entram"], BLUE)
    b += arrow(895, 352, 895, 402, MUTED, "m")

    b += ns(1120, 262, 385, 310, "monitoring", BLUE)
    b += component(1136, 294, 353, 82, "Prometheus", ["SLO 99,5% · burn rate · regras", "de alerta com runbook"], BLUE)
    b += component(1136, 386, 353, 74, "Alertmanager", ["alertas agent=sre → webhook"], BLUE)
    b += component(1136, 470, 353, 88, "Grafana", ["dashboards como código: SLO,", "golden signals, custo do LLM"], BLUE)
    b += arrow(1076, 440, 1134, 335, BLUE, "b")
    b += num(1105, 372, "1", BLUE)

    # agente
    b += ns(700, 600, 805, 315, "sre-agent · time de agentes", TEAL)
    agents = [("Investigador", "código", "junta evidências"), ("Diagnosticador", "LLM", "causa + 1 ação"),
              ("Revisor", "LLM", "pode vetar"), ("Política", "código", "decide"),
              ("Executor", "código", "age com snapshot"), ("Verificador", "código", "SLO voltou?"),
              ("Documentador", "LLM", "postmortem")]
    bw, gap, x0 = 170, 28, 716
    for i, (n, kind, role) in enumerate(agents):
        row, col = divmod(i, 4)
        x = x0 + col * (bw + gap)
        y = 640 + row * 130
        c = PURPLE if kind == "LLM" else TEAL
        b += box(x, y, bw, 92, PANEL2, c, 10)
        b += text(x + bw / 2, y + 30, n, 15, TXT, 600, "middle")
        b += text(x + bw / 2, y + 52, kind, 12, c, 700, "middle")
        b += text(x + bw / 2, y + 74, role, 12.5, MUTED, 400, "middle")
        if col < 3 and i < 6:
            b += arrow(x + bw + 2, y + 46, x + bw + gap - 2, y + 46, TEAL, "t")
    # quebra de linha: Política -> Executor
    b += path(f"M {x0 + 3 * (bw + gap) + bw / 2} 734 L {x0 + 3 * (bw + gap) + bw / 2} 752 "
              f"L {x0 + bw / 2} 752 L {x0 + bw / 2} 768", TEAL, "t")
    b += text(1488, 898, "IA propõe · IA revisa · código decide", 13, TEAL, 600, "end")

    # 2: alertmanager -> agente ; 3: executor -> orders-api
    b += arrow(1312, 574, 1312, 598, BLUE, "b")
    b += num(1336, 586, "2", BLUE)
    b += path("M 706 820 L 690 820 L 690 445 L 712 445", TEAL, "t")
    b += num(690, 520, "3", TEAL)

    # serviços <-> cluster
    b += arrow(292, 236, 348, 236, ORANGE, "o", dash=True)
    b += arrow(622, 236, 673, 236, ORANGE, "o", dash=True)
    b += arrow(292, 410, 348, 361, GREEN, "g")
    b += arrow(292, 470, 348, 486, GREEN, "g")
    b += arrow(622, 361, 673, 361, ORANGE, "o")
    b += arrow(706, 700, 624, 610, PURPLE, "p")  # agente -> bedrock

    # legenda
    ly = 1010
    b += num(50, ly - 5, "1", BLUE) + text(68, ly, "métricas viram alerta de SLO", 13, MUTED)
    b += num(300, ly - 5, "2", BLUE) + text(318, ly, "webhook aciona o time de agentes", 13, MUTED)
    b += num(580, ly - 5, "3", TEAL) + text(598, ly, "ação tipada no Deployment, verificada contra o SLO", 13, MUTED)
    b += text(1000, ly, "·   imagens: o nó só faz deploy com assinatura válida", 13, MUTED)
    return svg(W, H, b)


# ------------------------------------------------------------------ time de agentes
def agents():
    W, H = 1600, 640
    b = text(40, 62, "O time de agentes", 30, TXT, 700)
    b += text(40, 92, "Cada agente tem um papel estreito. Os de IA nunca ampliam o que pode ser feito: quem libera a execução é a política em código.", 15, MUTED)
    steps = [("Investigador", "código", ["logs, métricas e", "\"o que mudou?\""], "só lê"),
             ("Diagnosticador", "LLM", ["causa raiz e UMA", "ação do catálogo"], "só propõe"),
             ("Revisor", "LLM", ["segunda opinião", "sobre o plano"], "só veta"),
             ("Política", "código", ["confiança, limites,", "allowlist, tentativas"], "libera ou barra"),
             ("Executor", "código", ["aplica a ação e", "guarda snapshot"], "só Deployments"),
             ("Verificador", "código", ["rollout e SLIs de", "1 min; piorou? desfaz"], "só lê"),
             ("Documentador", "LLM", ["postmortem em", "português"], "só escreve")]
    bw, gap, x0, y0 = 196, 21, 40, 170
    for i, (n, kind, desc, can) in enumerate(steps):
        x = x0 + i * (bw + gap)
        c = PURPLE if kind == "LLM" else TEAL
        b += box(x, y0, bw, 250, PANEL, c, 14, sw=2)
        b += text(x + 18, y0 + 34, f"{i + 1:02d}", 13, c, 700)
        b += text(x + 18, y0 + 66, n, 19, TXT, 700)
        b += pill(x + 18, y0 + 82, kind, c, 12)
        for j, line in enumerate(desc):
            b += text(x + 18, y0 + 142 + j * 21, line, 14, MUTED)
        b += f'<line x1="{x + 18}" y1="{y0 + 196}" x2="{x + bw - 18}" y2="{y0 + 196}" stroke="{STROKE}"/>\n'
        b += text(x + 18, y0 + 226, can, 14, c, 600)
        if i < 6:
            b += arrow(x + bw + 2, y0 + 125, x + bw + gap - 1, y0 + 125, MUTED, "m")
    # faixa de guardrails
    gy = 470
    b += box(40, gy, 1520, 120, PANEL2, STROKE, 14)
    b += text(64, gy + 38, "Guardrails em código, não em prompt", 17, TXT, 700)
    items = ["catálogo fechado de 6 ações", "máx. 2 tentativas · 10 ações/hora", "réplicas 1..5 · memória 64..1024Mi",
             "RBAC mínimo no cluster", "snapshot e reversão automática", "LLM fora do ar → escala para humano"]
    x = 64
    for it in items:
        w = int(len(it) * 13 * 0.5) + 24
        if x + w > 1540:
            break
        b += pill(x, gy + 62, it, TEAL, 13)
        x += w + 12
    return svg(W, H, b)


# ------------------------------------------------------------------ capa
def cover():
    W, H = 1600, 900
    b = ""
    # grade sutil
    for gx in range(0, W, 40):
        b += f'<line x1="{gx}" y1="0" x2="{gx}" y2="{H}" stroke="#121A30" stroke-width="1"/>\n'
    for gy in range(0, H, 40):
        b += f'<line x1="0" y1="{gy}" x2="{W}" y2="{gy}" stroke="#121A30" stroke-width="1"/>\n'
    b += pill(90, 120, "PROJETO · SRE · DEVOPS · IA", TEAL, 15)
    b += text(90, 250, "Autonomous", 96, TXT, 800)
    b += text(90, 352, "SRE Agent", 96, TEAL, 800)
    b += text(90, 420, "Um time de agentes de IA que detecta, corrige e documenta", 28, TXT, 500)
    b += text(90, 458, "incidentes em produção sozinho, com guardrails em código.", 28, TXT, 500)

    stats = [("1 a 3 min", "MTTR: do alerta ao SLO verificado"), ("US$ 0,0002", "de LLM por incidente"),
             ("7 agentes", "IA propõe, IA revisa, código decide")]
    for i, (big, small) in enumerate(stats):
        x = 90 + i * 380
        b += box(x, 530, 350, 130, PANEL, STROKE, 16)
        b += text(x + 26, 590, big, 40, TXT, 800)
        b += text(x + 26, 630, small, 16, MUTED)

    tags = ["AWS", "Terraform", "k3s", "Amazon Bedrock", "Prometheus", "Grafana", "GitHub Actions", "AWS Signer"]
    x = 90
    for t in tags:
        b += pill(x, 720, t, BLUE, 15)
        x += int(len(t) * 15 * 0.56) + 24 + 12
    b += text(90, 820, "github.com/MaikonJm/autonomous-sre-agent", 18, MUTED)

    # ilustração: anel de agentes à direita
    import math
    cx, cy, r = 1290, 290, 155
    names = ["Investigador", "Diagnosticador", "Revisor", "Política", "Executor", "Verificador", "Documentador"]
    b += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{STROKE}" stroke-width="2" stroke-dasharray="4 8"/>\n'
    b += f'<circle cx="{cx}" cy="{cy}" r="64" fill="{PANEL}" stroke="{TEAL}" stroke-width="2"/>\n'
    b += text(cx, cy - 4, "incidente", 16, TXT, 700, "middle") + text(cx, cy + 18, "→ resolvido", 14, GREEN, 600, "middle")
    for i, n in enumerate(names):
        a = -math.pi / 2 + i * 2 * math.pi / len(names)
        px, py = cx + r * math.cos(a), cy + r * math.sin(a)
        c = PURPLE if n in ("Diagnosticador", "Revisor", "Documentador") else TEAL
        b += f'<circle cx="{px:.1f}" cy="{py:.1f}" r="13" fill="{BG}" stroke="{c}" stroke-width="3"/>\n'
        lx, ly = cx + (r + 30) * math.cos(a), cy + (r + 30) * math.sin(a)
        anc = "middle" if abs(math.cos(a)) < 0.3 else ("start" if math.cos(a) > 0 else "end")
        b += text(f"{lx:.1f}", f"{ly + 5:.1f}", n, 15, TXT, 600, anc)
    return svg(W, H, b)


for name, fn in (("arquitetura", architecture), ("time-de-agentes", agents), ("capa", cover)):
    (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
    print("ok", name)
