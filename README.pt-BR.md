# AgentBoard Collectors

[English](./README.md) | [简体中文](./README.zh-CN.md) | [繁體中文](./README.zh-TW.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md) | **Português (BR)**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

Coletores locais open-source para uso de programação com IA.

Parte do [AgentBoard](https://agentboard.cc), o ranking de uso de programação com IA — [veja ao vivo →](https://agentboard.cc/leaderboard)

Os coletores do AgentBoard escaneiam os logs que as ferramentas de programação com IA já mantêm na sua máquina, agregam tudo localmente em estatísticas de uso e sincronizam **apenas metadados** com o [AgentBoard](https://agentboard.cc). Eles nunca enviam prompts, respostas do modelo, código-fonte, diffs, conteúdo de arquivos, caminhos de arquivos ou saída do terminal.

Cada linha de código que toca seus logs locais está neste repositório, escrita em Python puro com **zero dependências externas** — você pode ler exatamente o que é escaneado e exatamente o que é enviado.

## Fontes Suportadas

| Fonte | Status | Dados locais escaneados |
| --- | --- | --- |
| Claude Code | ✅ Suportado | `~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ Suportado | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ Suportado | `~/.codex/sessions`, `~/.codex/archived_sessions`, `$CODEX_HOME`, `%APPDATA%/codex` |
| Gemini | ✅ Suportado | `~/.gemini/tmp`, `$GEMINI_CLI_HOME/tmp` |
| Kimi Code | ✅ Suportado | `~/.kimi-code/sessions`, `$KIMI_CODE_DIR`, `$KIMI_CODE_HOME` |
| OpenCode | ✅ Suportado | `~/.local/share/opencode`, `$OPENCODE_HOME`, `$OPENCODE_DB` |
| OpenClaw | ✅ Suportado | `~/.openclaw`, `$OPENCLAW_HOME`, `$OPENCLAW_DIR` |

**Como as fontes se agrupam.** Cada coletor lê o armazenamento local de sessões de uma ferramenta (os caminhos acima), não um cliente específico — então qualquer cliente que grave suas sessões nesses caminhos é coletado, não apenas a CLI. No ranking do AgentBoard, eles se agrupam em seis selos: **Claude** (Claude Code + Cowork), **Codex**, **Gemini**, **Kimi Code**, **OpenCode**, **OpenClaw**.

Detalhes por fonte: [docs/supported-sources.md](./docs/supported-sources.md)

## O Que É Enviado

O AgentBoard envia **apenas metadados agregados de uso**:

- data, id de sessão com prefixo (ex.: `claude:<id>`), nome da fonte
- nome do dispositivo (hostname sanitizado, substituível via `$AGENTBOARD_DEVICE_NAME`) e plataforma
- tempo ativo de programação, janelas de tempo ativo (apenas timestamps)
- contagens de tokens: input, output, leitura de cache, criação de cache, raciocínio, total do provedor
- contagens de mensagens, chamadas de ferramentas, **nomes** das ferramentas mais usadas com contagens
- linhas adicionadas / removidas (contagens das ferramentas de edição)
- **contagens** de projetos e arquivos tocados — nunca seus nomes ou caminhos
- uso de skills (Claude Code): nomes/chaves de skills vindos de metadados públicos do `SKILL.md` e contagens de uso
- versão do coletor

## O Que Nunca Sai da Sua Máquina

- ❌ Prompts e respostas do assistente
- ❌ Código-fonte, diffs, conteúdo de arquivos
- ❌ Caminhos de arquivos e de projetos (apenas contagens são enviadas)
- ❌ Saída do terminal e variáveis de ambiente
- ❌ Nomes de usuário, e-mails, identificadores brutos da máquina

Referência completa campo a campo: [docs/data-fields.md](./docs/data-fields.md) · Política de privacidade: [PRIVACY.md](./PRIVACY.md)

## Instalação

macOS / Linux:

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows (PowerShell):

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

O instalador copia estes coletores para `~/.agentboard/`, registra uma sincronização em segundo plano (launchd no macOS, tarefa agendada no Windows) e conecta um hook ao evento de fim de sessão do Claude Code. Nada é executado remotamente após a instalação.

## Experimente Localmente Primeiro

Todo coletor tem um modo `--summary` que escaneia seus logs e imprime o JSON agregado **sem enviar nada**. Execute antes de instalar e veja exatamente o que o AgentBoard receberia:

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_kimi.py --summary      # Kimi Code
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## Como Funciona

1. **Hook** — o evento `Stop` do Claude Code aciona o `hook.sh`, que executa o coletor para a sessão finalizada (limitado a uma vez a cada 5 minutos por sessão).
2. **Daemon** — um processo leve em segundo plano reescaneia a cada 180 segundos, mantendo sincronizado o uso do Codex, Gemini e outras ferramentas.
3. **Agregação local** — os coletores transformam logs de sessão JSONL/JSON/SQLite em estatísticas por dia e por sessão. O parsing acontece inteiramente na sua máquina.
4. **Sincronização de metadados** — os agregados são enviados via POST para a API do AgentBoard com um token de dispositivo armazenado em `~/.agentboard/config.json`. O servidor armazena apenas **hashes** dos tokens, nunca os tokens em si.

## Contabilidade de Tokens

Cada ferramenta reporta tokens de um jeito. O AgentBoard guarda tanto o total do próprio provedor quanto o detalhamento completo, para que os números do ranking batam com os painéis oficiais de uso:

- **Claude Code / Cowork / Kimi Code / OpenCode / OpenClaw** — tokens de cache são campos separados; `provider_total = input + output + cache_read + cache_creation`.
- **Codex** — o input reportado já inclui tokens de cache; `provider_total = input + output`. Os campos de cache existem só para detalhamento, nunca são contados duas vezes.
- **Gemini** — usa o `totalTokenCount` do provedor; o Gemini não reporta criação de cache.

`non_cache_total = provider_total − cache_read − cache_creation` está disponível em todas as fontes para comparações sem cache. Semântica completa: [docs/token-accounting.md](./docs/token-accounting.md)

## O Que o AgentBoard Não É

O AgentBoard não é um medidor de cobrança nem uma prova criptográfica de trabalho. Logs locais de ferramentas de IA podem ser editados pelo dono, e cada ferramenta expõe dados de tokens de forma diferente. O foco do AgentBoard é análise de uso transparente e consciente da fonte — estatísticas pessoais, visibilidade de equipe e tendências.

## Desenvolvimento

Os coletores são scripts independentes de Python 3 que usam apenas a biblioteca padrão — sem `pip install`. Cada script é intencionalmente autocontido para que possa ser auditado em uma única leitura.

```bash
python3 collectors/collect.py --summary            # dry-run com seus logs reais
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # análise detalhada da contabilidade de tokens
```

Issues e PRs são bem-vindos — veja [CONTRIBUTING.md](./CONTRIBUTING.md). Para remover tudo, veja [UNINSTALL.md](./UNINSTALL.md).

## Licença

[MIT](./LICENSE)
