# Agente Arena (`/agentearena`)

Controlador autônomo e integração para a plataforma [Arena AI](https://arena.ai/) (Agent Mode) com suporte nativo a repositórios GitHub, gerenciamento de sessões persistentes e rotina de recuperação de falhas de conexão (`recover-and-push`).

---

## 🚀 Funcionalidades

- **Autenticação Automática e Persistente**: Gerenciamento de login via Playwright com perfil de navegador persistente (`arena_user_data`), mantendo a sessão ativa sem precisar autenticar a cada execução.
- **Relay de Prompts**: Envio programático de prompts para o agente do Arena AI, com monitoramento do ciclo de geração em tempo real e captura da resposta.
- **Integração com GitHub**:
  - Seleção dinâmica de repositórios vinculados à conta.
  - Seleção e rastreamento de branches específicas da conversa.
- **Rotina de Recuperação de Falhas (`recover-and-push`)**:
  - Caso o Arena AI perca a conexão com o repositório ou o workspace seja reiniciado:
    1. Identifica o branch ou identificador da conversa trabalhada.
    2. Abre uma nova conversa em `https://arena.ai/agent`.
    3. Reconecta o repositório no GitHub e seleciona a branch correspondente.
    4. Envia o comando de recarregamento para restaurar todo o histórico e alterações do projeto.
    5. Executa o push direto das modificações para o repositório de destino no GitHub.

---

## 🎯 Regra de Ouro: Priorização da Execução na Nuvem (Economia de Tokens)

1. **Prioridade 1 (Padrão):** O agente deve SEMPRE priorizar a execução via Arena AI (utilizando as skills do repositório `gabrielhklaser/gabebrain-skills` diretamente na plataforma Arena). Qualquer implementação, escrita de código, criação de testes e refatoração roda em nuvem sem consumir tokens locais do Antigravity.
2. **Prioridade 2 (Exceção):** O motor do Antigravity só deve ser acionado quando houver necessidade explícita de acessar arquivos físicos locais do GabeBrain (Biblioteca Geológica no Google Drive, Docling local ou ferramentas desktop). Nesses casos, o Antigravity atua como orquestrador cirúrgico e envia o contexto mastigado para a Arena AI executar.


---

## 📦 Estrutura do Projeto

```text
agentearena/
├── arena_agent.py      # Motor de automação e interface CLI
├── requirements.txt    # Dependências (Playwright)
├── .env.example        # Modelo de variáveis de ambiente
├── .gitignore          # Proteção para dados de sessão e credenciais
└── README.md           # Documentação do projeto
```

---

## 🛠️ Instalação e Configuração

1. **Instale as dependências:**
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. **Configure o arquivo de credenciais:**
   Copie `.env.example` para `.env` e preencha suas informações:
   ```env
   ARENA_EMAIL=seu_email@dominio.com
   ARENA_PASSWORD=sua_senha
   ARENA_USER_DATA_DIR=./arena_user_data
   ```

---

## 💻 Uso via Linha de Comando (CLI)

### 1. Testar Status da Sessão / Login
```bash
python arena_agent.py login
```

### 2. Listar Repositórios do GitHub Conectados
```bash
python arena_agent.py list-repos
```

### 3. Enviar um Prompt para o Arena AI Agent
```bash
python arena_agent.py send --prompt "Crie um componente de login responsivo em React" --repo "gabrielhklaser/agentearena" --branch "main"
```

### 4. Recuperação Automática e Push de Alterações
Se a conexão for perdida durante o desenvolvimento:
```bash
python arena_agent.py recover-push --repo "gabrielhklaser/agentearena" --branch "main"
```

---

## 🛡️ Segurança

- As credenciais e tokens de sessão ficam protegidos em arquivos locais `.env` e na pasta de sessão do navegador `arena_user_data/`, ambos ignorados no controle de versão (`.gitignore`).
