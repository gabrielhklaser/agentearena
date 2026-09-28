# Agente Arena (`/agentearena`)

Controlador da plataforma [Arena AI](https://arena.ai/) (Agent Mode) para o ecossistema **GabeBrain**: relay de prompts, seleção de repositório/branch no GitHub, sessão de navegador descartável e rotina de recuperação (`recover-push`) quando o Arena perde a conexão com o repositório.

Este repositório é a versão standalone da skill `arena-ai-controller`. O conteúdo é idêntico à fonte canônica (`~/.gemini/config/skills/arena-ai-controller/`) e à cópia em [`gabebrain-skills`](https://github.com/gabrielhklaser/gabebrain-skills) (`skills/arena-ai-controller/`). Instruções completas para agentes em [`SKILL.md`](SKILL.md).

---

## 🚀 Funcionalidades

- **Sessão persistente e descartável**: perfil Playwright fora de pastas sincronizadas (padrão `%LOCALAPPDATA%\gabebrain\arena-ai-controller`). Pode ser apagado a qualquer momento com `purge-session`; o próximo uso refaz o login pelo `.env`. `ARENA_PURGE_AFTER_USE=1` apaga ao fim de cada execução.
- **Relay de prompts**: envia prompts ao Agent Mode, acompanha a geração e devolve só as mensagens novas.
- **Skills GabeBrain na nuvem**: em conversas novas, prefixa o prompt pedindo ao Arena que consulte as skills do `gabebrain-skills` (`superpowers-coding-agent`, `no-ai-slop`, `escrita-tecnica-humanizada`). Desative com `--no-skill-prefix`.
- **Confirmações e follow-ups**: detecta quando o agente pede confirmação/escolha (`waiting_user_input` + `options`) e permite responder na mesma conversa com `--conversation-url`.
- **Health check de seletores**: `status` informa se a UI do Arena mudou e o script ficou "cego".
- **GitHub**: habilita a conexão, lista repositórios e seleciona repo/branch. A seleção é estrita: se o repo ou a branch não forem encontrados, o script aborta.
- **Recuperação (`recover-push`)**: abre conversa nova, reconecta repo e branch e manda recarregar as alterações da conversa anterior e fazer push.

---

## 🎯 Regra de Ouro: priorizar a execução na nuvem (economia de tokens)

1. **Prioridade 1 (padrão):** delegar implementação, testes, refatoração e análises extensas ao Arena AI, que consome as skills direto do `gabrielhklaser/gabebrain-skills`.
2. **Prioridade 2 (exceção):** usar o motor local (Antigravity/Claude Code) só quando for preciso acessar arquivos físicos do GabeBrain (Biblioteca Geológica no Drive, Docling local, QGIS). Nesse caso o agente local lê o trecho necessário e despacha o contexto para o Arena executar.

Antes de despachar, passe o prompt pelo `prompt-router-coordinator`: se ele indicar `antigravity` ou `claude`, não envie ao Arena.

---

## 📦 Estrutura

```text
agentearena/
├── SKILL.md              # Instruções da skill para os agentes
├── scripts/
│   └── arena_agent.py    # Motor de automação e CLI
├── requirements.txt      # Dependências (Playwright)
├── .env.example          # Modelo de variáveis de ambiente
└── .gitignore            # Protege .env e dados de sessão
```

---

## 🛠️ Instalação

Requer **Python ≥ 3.10**.

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Copie `.env.example` para `.env` **na raiz do repositório** (ao lado de `SKILL.md`) e preencha `ARENA_EMAIL` e `ARENA_PASSWORD`. Variáveis de ambiente do processo têm precedência sobre o `.env`.

| Variável | Padrão | Uso |
|---|---|---|
| `ARENA_EMAIL` / `ARENA_PASSWORD` | — | Login no Arena |
| `ARENA_HEADLESS` | `true` | `false` para ver a janela do navegador |
| `ARENA_USER_DATA_DIR` | `%LOCALAPPDATA%\gabebrain\arena-ai-controller` | Perfil do navegador (nunca em Drive/vault) |
| `ARENA_PURGE_AFTER_USE` | desligado | `1` apaga o perfil após cada execução |
| `ARENA_SKILL_PREFIX` | instrução padrão GabeBrain | Texto prefixado em conversas novas |

---

## 💻 CLI

```bash
python scripts/arena_agent.py login
python scripts/arena_agent.py status
python scripts/arena_agent.py list-repos
python scripts/arena_agent.py connect --repo "gabrielhklaser/REPO" --branch "main"
python scripts/arena_agent.py send --prompt "..." --repo "gabrielhklaser/REPO" --branch "main" [--timeout 180] [--prompt-file arquivo.txt] [--no-skill-prefix]
python scripts/arena_agent.py send --conversation-url "https://arena.ai/agent/<id>" --prompt "Approve"
python scripts/arena_agent.py recover-push --repo "gabrielhklaser/REPO" --branch "BRANCH"
python scripts/arena_agent.py purge-session
```

### Contrato de saída (`send` / `recover-push`)

Durante a execução, `[CONVERSATION_URL] <url>` é emitido assim que a conversa existe. A última linha do stdout é `[RESULT_JSON] {…}` (JSON em uma linha) com `status`, `response`, `conversation_url`, `requires_confirmation`, `last_message`, `options` e `warnings`.

| `status` | Significado | Ação |
|---|---|---|
| `success` | Geração terminou | Registrar resposta |
| `waiting_user_input` | Agente pediu confirmação/escolha (`options`) | `send --conversation-url <url> --prompt "<opção>"` |
| `timeout` | Tempo local esgotou, agente segue rodando na nuvem | Não reenviar; retomar pela mesma `conversation_url` |

Um prompt curto (≤ 35 caracteres) igual ao texto de um botão visível vira clique, mas só com `--conversation-url`.

---

## 🛡️ Segurança

- Credenciais só no `.env` (ignorado pelo Git) ou em variáveis de ambiente; nada embutido no código.
- Logs não expõem o e-mail (`email_configured: true`) nem o conteúdo do prompt (só a contagem de caracteres).
- O script avisa se o perfil de sessão estiver em pasta sincronizada (Drive, OneDrive, Dropbox, vault) e remove pastas `.session_data` de versões antigas.
