# Instruções Base de Engenharia — Host `dev`

Estas regras valem para todo trabalho neste host, independentemente do
diretório ou harness. A fonte canônica é `/home/dev/.agents/BASELINE.md`.

- Codex recebe esta baseline por `~/.codex/AGENTS.md`; Claude Code por
  `~/.claude/CLAUDE.md` (symlinks para a canônica).
- Projetos gerenciados carregam no `AGENTS.md` apenas um bloco-ponteiro curto.
  A cópia integral portátil fica em `docs/agents/BASELINE.md` dentro do repo,
  para harnesses sem canal global — lida sob demanda, não em todo prompt.
- Conteúdo específico do projeto fica fora do bloco gerenciado.

## Fronteira: projeto de software ≠ workspace de agente

Este arquivo, e todo `AGENTS.md` de projeto, é **instrução de engenharia**.
Personalidade, memória, identidade, heartbeat e comportamento de chat
pertencem ao OpenClaw e vivem em `~/.openclaw/workspace-*/` (`SOUL.md`,
`IDENTITY.md`, `USER.md`, `HEARTBEAT.md`). **Não traga esse conteúdo para
cá**: um `AGENTS.md` é lido por todos os harnesses, e a personalidade de um
agente faria os outros herdarem comportamento que não é deles.

## Disciplina de Engenharia

- Declarar premissas. Havendo mais de uma leitura válida, expor as opções em
  vez de escolher em silêncio; perguntar quando a incerteza muda a solução;
  discordar quando houver caminho mais simples ou mais seguro.
- Mínimo de código que resolve o problema real. Sem abstração, flexibilidade
  ou configuração especulativa.
- Mudança cirúrgica: tocar só no necessário, seguir o estilo local, não
  refatorar vizinhança por oportunismo, não apagar código sem justificativa.
  Problema não relacionado notado: mencionar à parte, não embutir no patch.
- Entender por que algo falha antes de reescrever. Reescrita que
  "provavelmente resolve" troca um bug conhecido por vários desconhecidos.
- Verificar o resultado real, não o código de saída: o endpoint servido, o
  dado gravado, a tela renderizada. Relatar o que de fato ocorreu — dizer que
  um teste passou quando falhou custa mais caro do que nunca tê-lo rodado.
- Semântica de negócio ambígua (termo, unidade, regra, fluxo): **perguntar
  antes**. Palpite plausível passa no review e corrompe dado em silêncio.
- Centralizar regra de negócio: constante ou conversão duplicada se unifica,
  não se replica.
- Alterou modelo de dados, contrato ou regra: percorrer todos os pontos
  afetados e informar o que a mudança atinge. Estrutura trocada entre camadas
  se define uma vez e se reutiliza.
- Antes de construir sistema, integração ou automação custom: checar
  rapidamente se já existe solução open-source, biblioteca mantida ou recurso
  nativo adequado. Não recomendar serviço pago sem aprovação de gasto.

## Escreva, Não Memorize

Memória de sessão não sobrevive a restart; arquivo sobrevive. Decisão
relevante → changelog, ADR ou doc do projeto. Lição aprendida → onde o
próximo agente vai ler. "Anotação mental" não existe.

## Segurança

- Nunca ler nem imprimir `.env`, `.env.local`, `.env.production` ou
  equivalentes (`.env.example` pode, para entender a estrutura). Valor de
  variável: perguntar; adicionar: instruir o usuário a fazê-lo manualmente.
- Nunca commitar segredo, token ou chave; credencial sempre por variável de
  ambiente. Segredo exposto por acidente: avisar imediatamente e tratar como
  comprometido.
- Não escrever segredo em arquivo de instrução, memória ou log — esses
  arquivos entram no prompt de toda sessão.
- Não exfiltrar dado privado.

## Ações Destrutivas e Estado Existente

- Comando destrutivo: perguntar antes; `trash` é preferível a `rm`. Antes de
  deletar ou sobrescrever, olhar o alvo.
- Crontab, unidades systemd, configs do nginx, arquivos rc do shell, PM2:
  **inspecionar o estado atual primeiro** e preservar/mesclar por padrão.
- Produção não roda necessariamente de onde você está editando. Confirmar o
  caminho real antes de concluir que um deploy aconteceu.

## Ação Local vs Ação Externa

**Livre:** ler, explorar, buscar, testar, implementar localmente.
**Perguntar antes:** o que sai da máquina ou é difícil de reverter —
publicar, enviar mensagem, deploy em produção, apagar branch remota, DNS,
certificado. Aprovação em um contexto não se estende ao próximo.

## Portas, Domínios e Roteamento

Antes de mexer em domínio, porta, PM2, Docker, nginx, proxy reverso ou
roteamento neste servidor: **ler `/home/dev/PORT_REGISTRY.md` primeiro** —
fonte canônica de portas, domínios, bancos, serviços e localização de deploy.
Não inferir roteamento de um único repo quando o registry responde. Criou,
moveu ou reatribuiu porta: atualizar o registry **na mesma tarefa**; incidente
relevante de infraestrutura também é registrado lá.

## Ferramentas

Prefixar com `rtk` comandos de shell verbosos quando a forma RTK preservar a
semântica necessária: git, gerenciadores de pacote, build, teste, busca e
listagem de diretório. Usar o comando original quando RTK não suportar uma
opção ou quando a saída exata for necessária.

### Arquivos-chave

| O quê | Onde |
|---|---|
| Esta baseline | `/home/dev/.agents/BASELINE.md` |
| Bloco-ponteiro de projeto | `/home/dev/.agents/POINTER.md` |
| Template de projeto | `/home/dev/.agents/project-AGENTS.md` |
| Registro de infra | `/home/dev/PORT_REGISTRY.md` |
