# Política de autonomia N1–N5

O diagnóstico e a coleta continuam disponíveis para incidentes críticos; **não há alteração automática N4/N5**, mesmo se o modelo recomendar. Severidade é uma decisão da política de execução e não do LLM.

- N1: observar.
- N2–N3: candidato a ação do catálogo existente, somente laboratório, teste ou sandbox, com configuração e token explícitos.
- N4–N5, origem desconhecida ou severidade não mapeada: investigar e escalonar.
- Produção: sem ação automática nesta etapa.

A configuração padrão passa a ser `DRY_RUN=true`, `SRE_AUTONOMY=observe`. O comportamento da demo original muda: cenários com alertas critical serão escalados. Não reclassifique incidentes reais para forçar uma ação.

Para testar auto-remediação **somente em laboratório**, configure `SRE_AUTONOMY=lab`, `SRE_ENVIRONMENT=lab`, `DRY_RUN=false`, `SRE_ALERT_LEVELS` (JSON com nomes dos alertas e níveis internos 1–5), `SRE_AUTO_ACTIONS` (lista de tipos do catálogo autorizados), `SRE_WEBHOOK_TOKEN` (Secret, nunca em Git) e mantenha o revisor ativo. O Alertmanager deve enviar `X-SRE-Webhook-Token` por um proxy/adaptador privado autorizado. Labels recebidos apenas elevam o nível interno: critical vira N5, warning N3; valor desconhecido N5. Não há permissão só porque o webhook diz que o incidente é baixo.

O executor original continua aplicando allowlist de deployment, limites de recursos, tentativas e snapshot; esta etapa não adiciona autorização produtiva, rollback durável, integração com Dynatrace, ou aprovação executável pelo Squad HQ. Os relatórios incluem o nível interno. Valide a política com o cluster simulado e depois um laboratório isolado.
