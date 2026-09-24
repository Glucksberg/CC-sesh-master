<!-- BEGIN MANAGED BASELINE: /home/dev/.agents/BASELINE.md -->
# Baseline de Engenharia — Host `dev`

A baseline de engenharia deste host aplica-se integralmente a este
repositório. Leia-a sob demanda: `docs/agents/BASELINE.md` (cópia portátil;
canônica em `/home/dev/.agents/BASELINE.md`).

Inegociáveis mesmo sem ler a baseline:

- Portas, domínios, nginx, PM2, Docker, deploy: ler
  `/home/dev/PORT_REGISTRY.md` antes; porta criada ou movida atualiza o
  registry na mesma tarefa.
- Nunca ler nem imprimir `.env*` (exceto `.env.example`); nunca commitar
  segredo.
- Ação destrutiva ou externa (deploy, publicar, apagar, DNS): perguntar antes.
- Personalidade/memória de agente (OpenClaw) não pertencem a este arquivo.
<!-- END MANAGED BASELINE -->

# CC-sesh-master

Dashboard de monitoramento em tempo real de sessões e processos do Claude
Code: lê os JSONL nativos de `~/.claude/projects/` e exibe processos (RAM,
CPU, anomalias) e conversas em uma UI web.

## Stack e Comandos

| | |
|---|---|
| Runtime | Python 3.8+ (`serve-dashboard.py`) + shell scripts |
| Processo | PM2 via `ecosystem.config.js` |

## Documentação

| Tarefa toca em… | Ler |
|---|---|
| Uso, arquitetura das views | `README.md` |
| Código legado | `legada/` |
