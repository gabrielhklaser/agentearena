"""
Arena AI Automated Agent Controller
Controls https://arena.ai/ via Playwright persistent session.
Supports:
- Session maintenance & automated login
- GitHub connection & repository/branch selection
- Sending prompts & streaming responses
- Failover & recovery on disconnected repository:
  Retrieves branch, opens new conversation, loads previous changes and pushes to GitHub.
"""

import os
import re
import sys
import json
import time
import argparse
import shutil
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Load secrets only from the ignored, per-skill .env file or process environment.
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
_cfg_file = SKILL_DIR / ".env"

if _cfg_file.exists():
    with open(_cfg_file, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

def _default_user_data_dir() -> Path:
    """Keep persistent browser cookies outside the repository by default."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base.expanduser() / "gabebrain" / "arena-ai-controller"


def _ensure_private_directory(path: str | Path) -> Path:
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    return directory


ARENA_EMAIL = os.environ.get("ARENA_EMAIL", "").strip()
ARENA_PASSWORD = os.environ.get("ARENA_PASSWORD", "")
ARENA_HEADLESS = os.environ.get("ARENA_HEADLESS", "false").lower() in ("true", "1", "yes")
ARENA_VISIBLE = os.environ.get("ARENA_VISIBLE", "false").lower() in ("true", "1", "yes")
DEFAULT_USER_DATA_DIR = os.environ.get("ARENA_USER_DATA_DIR") or str(_default_user_data_dir())

BASE_URL = "https://arena.ai"
AGENT_URL = "https://arena.ai/agent"

# Prompts curtos iguais ao texto de um botão viram clique (ex: "Approve", "Sim")
MAX_BUTTON_PROMPT_LEN = 35
MESSAGE_SELECTOR = '[data-message-author="assistant"], div.prose, [data-testid="agent-message"]'
EDITOR_SELECTOR = 'div.tiptap.ProseMirror, [contenteditable="true"], textarea'

# Instrui o Arena a aplicar as skills do GabeBrain na nuvem (só em conversas novas)
SKILL_PREFIX = os.environ.get("ARENA_SKILL_PREFIX") or (
    "[GabeBrain] Antes de executar, consulte no repositório gabrielhklaser/gabebrain-skills "
    "(skills/<nome>/SKILL.md) as skills pertinentes à tarefa — em especial superpowers-coding-agent "
    "para código e no-ai-slop / escrita-tecnica-humanizada para textos. Responda em português brasileiro.\n\n"
)

# DADOS DE SESSÃO (cookies/perfil Chrome) SÃO DESCARTÁVEIS.
# - O perfil vive em ARENA_USER_DATA_DIR, FORA de pastas sincronizadas (Google Drive / vault).
# - Pode ser apagado a qualquer momento (`purge-session`); o próximo uso refaz o login pelo .env.
# - ARENA_PURGE_AFTER_USE=1 apaga o perfil ao fim de cada execução (mais seguro, exige login a cada uso).
# - Pastas `.session_data` de versões antigas da skill são removidas automaticamente.
PURGE_AFTER_USE = os.environ.get("ARENA_PURGE_AFTER_USE", "").strip() in ("1", "true", "yes")
SYNCED_DIR_MARKERS = ("meu drive", "my drive", "google drive", "onedrive", "dropbox", "obsidian_gabebrain")
LEGACY_SESSION_DIR = ".session_data"


def purge_session_dir(path):
    """Apaga um perfil de navegador/sessão. Retorna True se algo foi removido."""
    p = Path(path)
    if not p.exists():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return not p.exists()


def purge_legacy_session_dirs():
    for base in (SCRIPT_DIR.parent, SCRIPT_DIR):
        legacy = base / LEGACY_SESSION_DIR
        if purge_session_dir(legacy):
            print(f"[*] Sessão legada removida: {legacy}")


def warn_if_synced(path):
    if any(m in str(path).lower() for m in SYNCED_DIR_MARKERS):
        print(f"[!] ARENA_USER_DATA_DIR está em pasta sincronizada ({path}). Cookies de login vão para a nuvem — mova para fora.")


def extract_conversation_id(url: str | None) -> str | None:
    """Extrai o identificador único da conversa da URL do Arena AI."""
    if not url:
        return None
    m = re.search(r"/agent/([a-zA-Z0-9_-]+)", url)
    if m:
        cid = m.group(1)
        if cid not in ("agent", ""):
            return cid
    return None


class ArenaController:
    def __init__(self, user_data_dir=None, headless=None, visible=None):
        self.user_data_dir = str(_ensure_private_directory(user_data_dir or DEFAULT_USER_DATA_DIR))
        warn_if_synced(self.user_data_dir)
        self.headless = ARENA_HEADLESS if headless is None else headless
        self.visible = ARENA_VISIBLE if visible is None else visible
        self.playwright = None
        self.context = None
        self.page = None
        self._session_ok = False

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        # Se visible for False (padrão), posiciona fora do monitor visível (-32000,-32000)
        # permitindo que o Cloudflare Turnstile passe em ~1s sem incomodar a tela do usuário.
        window_pos = "--window-position=0,0" if self.visible else "--window-position=-32000,-32000"
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            window_pos,
            "--window-size=1440,900"
        ]
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                channel="chrome",
                args=launch_args,
                viewport={"width": 1440, "height": 900}
            )
        except Exception:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=launch_args,
                viewport={"width": 1440, "height": 900}
            )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        if PURGE_AFTER_USE and purge_session_dir(self.user_data_dir):
            print("[*] Perfil de sessão apagado após uso (ARENA_PURGE_AFTER_USE).")

    async def wait_for_cloudflare(self, timeout_seconds=12):
        """Awaits Cloudflare Turnstile / security challenge to clear."""
        for _ in range(timeout_seconds):
            try:
                title = await self.page.title()
                body = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
                if "momento" not in title.lower() and "just a moment" not in title.lower() and "security verification" not in body.lower():
                    return True
            except Exception:
                pass
            await self.page.wait_for_timeout(1000)
        return False

    async def ensure_logged_in(self):
        """Verifies authentication and performs login if necessary.
        Cached per run: repeated calls must not navigate away (would drop repo/branch selection)."""
        if self._session_ok:
            return True

        print(f"[*] Navigating to {BASE_URL}...")
        await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await self.wait_for_cloudflare(timeout_seconds=8)
        await self.page.wait_for_timeout(2000)

        if ARENA_EMAIL:
            user_btn = self.page.locator(f'button:has-text("{ARENA_EMAIL}")')
            if await user_btn.count() > 0 and await user_btn.first.is_visible():
                print("[+] An authenticated Arena session is active.")
                self._session_ok = True
                return True

        toggle_btn = self.page.locator('button[aria-label="Toggle Sidebar"], button[aria-label="Open sidebar"]')
        if await toggle_btn.count() > 0:
            await toggle_btn.first.click()
            await self.page.wait_for_timeout(500)

        login_btn = self.page.locator('button:has-text("Log In")')
        if await login_btn.count() == 0 or not await login_btn.first.is_visible():
            self._session_ok = True
            return True

        if not ARENA_EMAIL or not ARENA_PASSWORD:
            raise ValueError("Configure ARENA_EMAIL e ARENA_PASSWORD no ambiente ou no .env local ignorado pelo Git.")

        print("[*] Performing Arena sign-in...")
        await login_btn.first.click()
        await self.page.wait_for_timeout(1000)

        email_input = self.page.locator('input[type="email"]')
        await email_input.fill(ARENA_EMAIL)
        await self.page.wait_for_timeout(500)
        await self.page.click('button:has-text("Continue with email")')

        await self.page.wait_for_selector('input[type="password"]', timeout=15000)
        await self.page.fill('input[type="password"]', ARENA_PASSWORD)
        await self.page.wait_for_timeout(500)

        modal_login = self.page.locator('div[role="dialog"] button:has-text("Log In"), button[type="submit"]:has-text("Log In")')
        if await modal_login.count() > 0:
            await modal_login.first.click()
        else:
            await self.page.click('button:has-text("Log In")')

        await self.page.wait_for_timeout(5000)
        if await self.page.locator('input[type="password"]:visible').count() > 0:
            raise RuntimeError("Login falhou (formulário de senha ainda visível). Verifique as credenciais no .env.")
        print("[+] Arena login flow completed.")
        self._session_ok = True
        return True

    async def open_agent_mode(self, force_fresh=False):
        """Navigates to the Agent Mode workspace."""
        await self.ensure_logged_in()
        is_sub_agent_url = "/agent/" in self.page.url and not self.page.url.rstrip("/").endswith("/agent")
        if force_fresh or not self.page.url.startswith(AGENT_URL) or is_sub_agent_url:
            print(f"[*] Navigating to Agent Mode: {AGENT_URL}...")
            await self.page.goto(AGENT_URL, wait_until="domcontentloaded", timeout=45000)
            await self.wait_for_cloudflare(timeout_seconds=10)
            await self.page.wait_for_timeout(2000)
        else:
            await self.wait_for_cloudflare(timeout_seconds=5)

    async def ensure_github_enabled(self):
        """Ensures the GitHub connection toggle is activated in Agent Mode."""
        await self.open_agent_mode()

        # 1. Se o botão de configurações do GitHub ou o seletor já está visível
        repo_btn = await self._get_repo_button(wait_timeout=4)
        if repo_btn:
            return True

        # 2. Abre 'Add files and connections' e verifica o switch de forma idempotente
        add_btn = self.page.locator('button[aria-label="Add files and connections"]')
        if await add_btn.count() > 0:
            print("[*] Opening 'Add files and connections'...")
            await add_btn.first.click()
            await self.page.wait_for_timeout(800)

            github_switch = self.page.locator('button[role="switch"]').first
            if await github_switch.count() > 0:
                state = await github_switch.get_attribute("data-state")
                aria_checked = await github_switch.get_attribute("aria-checked")
                if state == "unchecked" or aria_checked == "false":
                    print("[*] Enabling GitHub switch...")
                    await github_switch.click()
                    await self.page.wait_for_timeout(1000)
                else:
                    print("[+] GitHub switch is already checked.")

            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(1000)

            # Aguarda a toolbar do GitHub carregar
            try:
                await self.page.wait_for_selector('button[aria-label="GitHub settings"], button:has-text("Select a repository")', timeout=6000)
            except Exception:
                pass
            return True

        return False

    async def _get_repo_button(self, wait_timeout=10):
        """
        Finds the button that opens the repository dropdown.
        Anchored to the GitHub settings button container in the toolbar.
        Handles the 'Loading...' state until the repository is hydrated.
        """
        gh_settings = self.page.locator('button[aria-label="GitHub settings"]')
        if await gh_settings.count() > 0 and await gh_settings.first.is_visible():
            parent = gh_settings.first.locator('..')
            candidate = parent.locator('button').first
            if await candidate.count() > 0:
                start = time.time()
                while time.time() - start < wait_timeout:
                    try:
                        text = (await candidate.inner_text()).strip()
                        disabled = await candidate.is_disabled()
                        if text != "Loading..." and not disabled:
                            return candidate
                    except Exception:
                        pass
                    await self.page.wait_for_timeout(500)
                return candidate

        # Fallback para seletores diretos excluindo o sidebar
        for sel in [
            'main button:has-text("Select a repository")',
            'button:has-text("Select a repository")',
            'div:has(> button[aria-label="GitHub settings"]) button',
        ]:
            b = self.page.locator(sel)
            if await b.count() > 0 and await b.first.is_visible():
                return b.first

        return None

    async def list_repositories(self):
        """Returns the list of available GitHub repositories connected to the account."""
        await self.ensure_github_enabled()
        print("[*] Opening repository selector...")
        repo_btn = await self._get_repo_button(wait_timeout=10)
        if not repo_btn:
            raise RuntimeError("Repository selector button not found.")
        await repo_btn.click()
        await self.page.wait_for_timeout(1000)

        repos = await self.page.eval_on_selector_all(
            '[role="option"], div[data-radix-collection-item], [role="menuitem"]',
            """elements => elements.map(el => el.innerText.trim()).filter(x => x && !x.startsWith('+'))"""
        )
        await self.page.keyboard.press("Escape")
        await self.page.wait_for_timeout(300)
        return repos

    async def select_repository(self, repo_name, branch_name=None, required_branch=False):
        """Selects a specific GitHub repository and optionally a branch."""
        await self.ensure_github_enabled()
        print(f"[*] Selecting repository '{repo_name}'...")

        repo_btn = await self._get_repo_button(wait_timeout=10)
        if not repo_btn:
            raise RuntimeError("Repository selector button not found. Rode `status` para checar os seletores.")

        current_repo_text = (await repo_btn.inner_text()).strip()
        print(f"[*] Current active repository text: '{current_repo_text}'")
        if repo_name.lower() in current_repo_text.lower():
            print(f"[+] Repository '{repo_name}' is already active.")
        else:
            await repo_btn.click()
            await self.page.wait_for_timeout(1000)
            target_option = self.page.locator(f'[role="option"]:has-text("{repo_name}"), [role="menuitem"]:has-text("{repo_name}")')
            if await target_option.count() == 0:
                await self.page.keyboard.press("Escape")
                raise ValueError(f"Repository '{repo_name}' not found in available list.")
            await target_option.first.click()
            await self.page.wait_for_timeout(1500)
            print(f"[+] Repository '{repo_name}' selected.")

        if branch_name:
            print(f"[*] Selecting branch '{branch_name}'...")
            gh_settings = self.page.locator('button[aria-label="GitHub settings"]')
            branch_btn = None
            if await gh_settings.count() > 0:
                parent = gh_settings.first.locator('..')
                buttons = parent.locator('button')
                if await buttons.count() >= 2:
                    branch_btn = buttons.nth(1)
            if not branch_btn or await branch_btn.count() == 0:
                branch_btn = self.page.locator('button:has-text("Branch"), button[aria-label*="branch" i]').first

            if not branch_btn or await branch_btn.count() == 0:
                if required_branch:
                    raise RuntimeError("Branch selector not found. Rode `status` para checar os seletores.")
                print("[!] Branch selector not found in UI. Prosseguindo...")
                return

            current_b_text = (await branch_btn.inner_text()).strip()
            if current_b_text.lower() == branch_name.lower():
                print(f"[+] Branch '{branch_name}' is already active.")
            else:
                await branch_btn.click()
                await self.page.wait_for_timeout(800)
                b_opt = self.page.locator(f'[role="option"]:has-text("{branch_name}"), [role="menuitem"]:has-text("{branch_name}")')
                if await b_opt.count() == 0:
                    await self.page.keyboard.press("Escape")
                    if required_branch:
                        raise ValueError(f"Branch '{branch_name}' not found for repository '{repo_name}'.")
                    print(f"[*] Branch '{branch_name}' não encontrada na UI; checkout/push será instruído no prompt.")
                else:
                    await b_opt.first.click()
                    await self.page.wait_for_timeout(1000)
                    print(f"[+] Branch '{branch_name}' selected.")

    async def get_active_repo(self):
        """Reads active repository name from the UI."""
        try:
            repo_btn = await self._get_repo_button(wait_timeout=4)
            if repo_btn:
                text = (await repo_btn.inner_text()).strip()
                if text and text not in ("Select a repository", "Loading..."):
                    return text
        except Exception:
            pass
        return None

    async def get_active_branch(self):
        """Reads active branch name from the UI."""
        try:
            gh_settings = self.page.locator('button[aria-label="GitHub settings"]')
            if await gh_settings.count() > 0:
                parent = gh_settings.first.locator('..')
                buttons = parent.locator('button')
                if await buttons.count() >= 2:
                    branch_btn = buttons.nth(1)
                    text = (await branch_btn.inner_text()).strip()
                    if text and text not in ("Branch", "Loading..."):
                        return text
            branch_btn = self.page.locator('button:has-text("Branch"), button[aria-label*="branch" i]').first
            if await branch_btn.count() > 0:
                text = (await branch_btn.inner_text()).strip()
                if text and text not in ("Branch", "Loading..."):
                    return text
        except Exception:
            pass
        return None

    async def get_current_state(self):
        """Inspects current repository, branch, and connection status."""
        await self.open_agent_mode()
        url = self.page.url
        body_text = await self.page.evaluate("() => document.body.innerText")

        elements = await self.page.eval_on_selector_all(
            "button, div",
            """elements => elements.map(el => el.innerText ? el.innerText.trim() : '').filter(x => x.includes('/') || x.includes('main') || x.includes('master'))"""
        )

        return {
            "url": url,
            "connected": "Select a repository" not in body_text,
            "elements": elements[:10],
            "selectors": await self.check_selectors(),
        }

    async def check_selectors(self):
        """Health check dos seletores da UI do Arena: detecta quando o site mudou e o script ficou cego."""
        checks = {
            "editor": EDITOR_SELECTOR,
            "repo_selector": 'button[aria-label="GitHub settings"], button:has-text("Select a repository"), div:has(> button[aria-label="GitHub settings"])',
            "add_connections": 'button[aria-label="Add files and connections"]',
            "messages": MESSAGE_SELECTOR,
        }
        counts = {name: await self.page.locator(sel).count() for name, sel in checks.items()}
        # Obrigatórios: editor sempre; mensagens só dentro de uma conversa. Os demais variam com o estado do GitHub.
        in_conversation = "/agent/" in self.page.url and not self.page.url.rstrip("/").endswith("/agent")
        required = ["editor"] + (["messages"] if in_conversation else [])
        broken = [n for n in required if counts[n] == 0]
        if counts["repo_selector"] == 0 and counts["add_connections"] == 0:
            broken.append("github_controls")
        return {"ok": not broken, "counts": counts, "broken": broken}


    async def _send_and_wait(self, prompt, timeout_seconds=180, is_new_conv=False, clicked_button=False):
        """
        Internal worker that inserts/sends prompt, handles Agree modals, captures URL,
        and polls generation until completion or timeout.
        """
        baseline_count = len(await self._read_messages())

        if not clicked_button:
            print(f"[*] Inserting prompt ({len(prompt)} characters)...")
            editor = self.page.locator(EDITOR_SELECTOR)
            await editor.first.wait_for(state="visible", timeout=20000)
            await editor.first.click()
            await self.page.keyboard.insert_text(prompt)
            await self.page.wait_for_timeout(800)

            send_btn = self.page.locator('button[aria-label="Send message"], button[aria-label="Send"], button[type="submit"]')
            if await send_btn.count() > 0 and await send_btn.first.is_visible():
                await send_btn.first.click()
            else:
                await self.page.keyboard.press("Enter")

        # Verifica se apareceu modal Agree automático
        await self.page.wait_for_timeout(1500)
        agree_btn = self.page.locator('button:has-text("Agree")')
        if await agree_btn.count() > 0 and await agree_btn.first.is_visible():
            print("[*] Agree modal appeared! Clicking Agree...")
            await agree_btn.first.click()
            await self.page.wait_for_timeout(2000)

        # Captura e emite a URL da conversa imediatamente para que o usuário possa abrir e acompanhar na Web
        await self.page.wait_for_timeout(1000)
        detected_url = self.page.url
        for _ in range(6):
            if "/agent/" in detected_url and not detected_url.endswith("/agent") and not detected_url.endswith("/agent/"):
                break
            await self.page.wait_for_timeout(1000)
            detected_url = self.page.url
        print(f"[CONVERSATION_URL] {detected_url}", flush=True)

        print("[*] Waiting for agent generation...")

        start_time = time.time()
        last_text_length = 0
        stable_count = 0
        new_messages = []
        current_text = ""
        finished = False

        while time.time() - start_time < timeout_seconds:
            await self.page.wait_for_timeout(3000)

            stop_btn = self.page.locator('button[aria-label*="Stop" i], button:has-text("Stop")')
            is_generating = await stop_btn.count() > 0 and await stop_btn.first.is_visible()

            messages = await self._read_messages()
            if len(messages) > baseline_count:
                new_messages = messages[baseline_count:]
            elif clicked_button:
                new_messages = messages[-1:]
            else:
                new_messages = []

            current_text = "\n\n".join(new_messages)
            if len(current_text) == last_text_length and len(current_text) > 0 and not is_generating:
                stable_count += 1
                if stable_count >= 2:
                    print("[+] Generation finished.")
                    finished = True
                    break
            else:
                stable_count = 0
                last_text_length = len(current_text)

        last_msg = new_messages[-1] if new_messages else ""
        
        # Procura por botões de ação ou escolha que ficaram disponíveis na tela
        detected_options = []
        try:
            detected_options = await self.page.eval_on_selector_all(
                'button:visible, [role="button"]:visible',
                r"""els => els
                    .map(e => e.innerText.trim())
                    .filter(t => t && t.length < 35 && /^(approve|run|allow|confirm|agree|reject|deny|cancel|yes|no|sim|n[ãa]o|aprovar|executar|continuar|op[çc][ãa]o \d+)/i.test(t))
                """
            )
            detected_options = list(dict.fromkeys(detected_options))
        except Exception:
            pass

        # Verifica se o texto pede confirmação / termina em pergunta
        is_question = bool(re.search(r"\?\s*$", last_msg.strip()))
        needs_confirmation_text = bool(re.search(r"(voc[eê] gostaria|devo prosseguir|deseja continuar|confirma|qual op[çc][ãa]o|proceder com|do you want|should i|please confirm|choose an option|waiting for your confirmation)", last_msg, re.IGNORECASE))
        
        requires_confirmation = (len(detected_options) > 0) or is_question or needs_confirmation_text
        if requires_confirmation:
            status = "waiting_user_input"
        elif finished:
            status = "success"
        else:
            status = "timeout"
            print(f"[!] Timeout de {timeout_seconds}s: agente ainda em execução ou interrompido no Arena AI.")

        warnings = []
        if not await self._read_messages():
            warnings.append(
                "Nenhuma mensagem encontrada com MESSAGE_SELECTOR — a UI do Arena pode ter mudado. Rode `status` para diagnosticar."
            )
            print(f"[!] {warnings[-1]}")

        conversation_url = self.page.url
        return {
            "conversation_url": conversation_url,
            "warnings": warnings,
            "response": current_text or "Prompt dispatched successfully to Arena AI Agent.",
            "status": status,
            "requires_confirmation": requires_confirmation,
            "last_message": last_msg,
            "options": detected_options,
            "finished": finished,
        }

    async def send_prompt(self, prompt, repo=None, branch=None, timeout_seconds=180, conversation_url=None,
                          skill_prefix=True, auto_failover=True, max_failovers=2):
        """
        Sends a prompt to the Arena AI Agent.
        Supports:
        - Navigating to an existing conversation_url to continue discussion or answer questions.
        - Clicking directly on confirmation buttons if prompt matches a button label.
        - Detecting when the agent asks a question or requires confirmation/choices.
        - Automatic failover: if prompt halts/times out, opens a new conversation, copies previous ID,
          pushes context, re-applies GabeBrain skills and continues task until completion.
        """
        is_existing_conv = bool(conversation_url and "/agent/" in conversation_url)
        if is_existing_conv:
            print(f"[*] Navigating to existing conversation: {conversation_url}...")
            await self.ensure_logged_in()
            await self.page.goto(conversation_url, wait_until="domcontentloaded", timeout=45000)
            await self.wait_for_cloudflare(timeout_seconds=8)
            await self.page.wait_for_timeout(2000)
            print(f"[CONVERSATION_URL] {conversation_url}", flush=True)
        else:
            await self.open_agent_mode()
            if repo:
                await self.select_repository(repo, branch)

        # 1. Resposta a confirmação: só em conversa existente, prompt curto e match exato com o botão
        clean_prompt = prompt.strip().lower()
        clicked_button = False

        if is_existing_conv and 0 < len(clean_prompt) <= MAX_BUTTON_PROMPT_LEN:
            btn_candidates = await self.page.locator('button:visible, [role="button"]:visible').all()
            for b in btn_candidates:
                try:
                    b_text = (await b.inner_text()).strip()
                    if b_text and b_text.lower() == clean_prompt:
                        print(f"[*] Found matching action button '{b_text}'. Clicking button...")
                        await b.click()
                        clicked_button = True
                        break
                except Exception:
                    continue

        full_prompt = prompt
        if skill_prefix and not is_existing_conv and not clicked_button:
            full_prompt = SKILL_PREFIX + prompt

        res = await self._send_and_wait(
            full_prompt,
            timeout_seconds=timeout_seconds,
            is_new_conv=not is_existing_conv,
            clicked_button=clicked_button
        )

        # 2. AUTO-FAILOVER & RETOMADA:
        # Se a execução parou por timeout e NÃO é um pedido de confirmação do usuário:
        # Abre nova conversa automaticamente, copia o ID anterior, recarrega contexto, faz push e continua.
        recovered_chain = []
        failover_attempt = 0

        while (res["status"] == "timeout") and auto_failover and (failover_attempt < max_failovers):
            failover_attempt += 1
            prev_conv_id = extract_conversation_id(res["conversation_url"] or self.page.url) or "desconhecido"
            recovered_chain.append(prev_conv_id)
            
            target_repo = repo or await self.get_active_repo()
            target_branch = branch or await self.get_active_branch()
            if not target_branch and prev_conv_id != "desconhecido":
                target_branch = f"arena/{prev_conv_id}"

            print(f"\n[FAILOVER] Geração interrompida na conversa {prev_conv_id}.", flush=True)
            print(f"[FAILOVER] Tentativa {failover_attempt}/{max_failovers}: Abrindo nova conversa no Arena AI com push de contexto...", flush=True)

            # Abre nova conversa limpa
            await self.open_agent_mode(force_fresh=True)

            # Reestabelece repositório e branch
            if target_repo:
                try:
                    await self.select_repository(target_repo, target_branch, required_branch=False)
                except Exception as e:
                    print(f"[!] Erro ao selecionar repo/branch no failover: {e}")

            # Monta o prompt de continuação com as skills do GabeBrain
            continuation_prompt = (
                f"{SKILL_PREFIX}"
                f"[GabeBrain - CONTINUAÇÃO AUTOMÁTICA DE TAREFA APÓS INTERRUPÇÃO]\n"
                f"A conversa anterior na plataforma Arena AI (ID: `{prev_conv_id}`) no repositório `{target_repo}` (branch: `{target_branch}`) "
                f"parou antes da finalização completa da tarefa solicitada.\n\n"
                f"DIRETRIZES OBRIGATÓRIAS:\n"
                f"1. Verifique o estado atual do repositório (`git status`, `git log -n 5`) na branch `{target_branch}`.\n"
                f"2. Recarregue os arquivos modificados e execute o push (`git commit` e `git push`) de todas as alterações pendentes da conversa `{prev_conv_id}` para o GitHub.\n"
                f"3. Retome e continue a execução da seguinte tarefa original até a sua conclusão definitiva, executando os testes, refatorações e validações necessárias:\n\n"
                f"--- TAREFA ORIGINAL SOLICITADA ---\n"
                f"{prompt}\n"
                f"--- FIM DA TAREFA ORIGINAL ---\n\n"
                f"Prossiga agora até finalizar 100% da tarefa e confirme a entrega ao final."
            )

            res = await self._send_and_wait(continuation_prompt, timeout_seconds=timeout_seconds, is_new_conv=True)

            if res["status"] == "success" or res["requires_confirmation"]:
                print(f"[+] Continuação finalizada com sucesso na nova conversa ({extract_conversation_id(self.page.url)})!", flush=True)
                break

        if recovered_chain:
            res["recovered"] = True
            res["previous_conv_ids"] = recovered_chain

        return res

    async def _read_messages(self):
        return await self.page.eval_on_selector_all(
            MESSAGE_SELECTOR,
            """els => els.map(e => e.innerText.trim()).filter(Boolean)"""
        )

    async def recover_and_push(self, repo, branch, custom_message=None):
        """
        FAILOVER & RECOVERY ROUTINE:
        If Arena AI loses connection with the GitHub repository:
        1. Identifies the branch from the target conversation.
        2. Opens a fresh conversation on Arena AI (/agent).
        3. Reconnects GitHub and selects the exact repository & branch.
        4. Issues the push command to reload previous conversation changes and push files to GitHub.
        """
        print(f"\n[!] Initiating Failover & Recovery Routine...")
        print(f"    Target Repo: {repo}")
        print(f"    Target Branch: {branch}")

        print("[*] Step 1: Opening a fresh conversation on https://arena.ai/agent ...")
        await self.open_agent_mode(force_fresh=True)

        print(f"[*] Step 2: Re-establishing repository connection to '{repo}' and branch '{branch}'...")
        await self.select_repository(repo, branch, required_branch=False)

        push_instruction = custom_message or (
            f"Por favor, recarregue todas as alterações e arquivos da branch '{branch}' "
            f"trabalhados na conversa anterior e execute o push de todas as modificações para o repositório '{repo}' no GitHub."
        )

        print(f"[*] Step 3: Sending reload and push command...")
        result = await self.send_prompt(push_instruction, repo=None, branch=None, skill_prefix=False, auto_failover=False)
        print("[+] Recovery prompt executed. Changes reloaded and push triggered.")
        return result


def emit_result(res):
    """Uma linha com prefixo fixo: o GabeBrain Hub faz parse sem regex frágil sobre o stdout inteiro."""
    print("[RESULT_JSON] " + json.dumps(res, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Arena AI Platform Controller")
    parser.add_argument("--visible", action="store_true",
                        help="Abre o navegador na tela visível (por padrão roda off-screen sem interromper)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Ensure login session is active")
    subparsers.add_parser("list-repos", help="List connected GitHub repositories")
    subparsers.add_parser("status", help="Check current session and repository status")

    connect_parser = subparsers.add_parser("connect", help="Select repository/branch in Agent Mode and report state")
    connect_parser.add_argument("--repo", required=True, help="Target GitHub repository")
    connect_parser.add_argument("--branch", default=None, help="Target branch name")

    send_parser = subparsers.add_parser("send", help="Send a prompt to Arena AI agent")
    send_parser.add_argument("--prompt", default=None, help="Prompt text to send")
    send_parser.add_argument("--prompt-file", default=None, help="Path to text file containing prompt")
    send_parser.add_argument("--conversation-url", default=None, help="Existing Arena AI conversation URL to continue")
    send_parser.add_argument("--repo", default=None, help="Target GitHub repository")
    send_parser.add_argument("--branch", default=None, help="Target branch name")
    send_parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds")
    send_parser.add_argument("--no-skill-prefix", action="store_true",
                             help="Não prefixar a instrução de uso das skills GabeBrain (conversas novas)")
    send_parser.add_argument("--no-failover", action="store_true",
                             help="Desativa o failover automático para nova conversa em caso de parada")
    send_parser.add_argument("--max-failovers", type=int, default=2,
                             help="Número máximo de tentativas de failover automático (padrão: 2)")
    send_parser.add_argument("--visible", action="store_true",
                             help="Abre o navegador na tela visível")

    subparsers.add_parser("purge-session", help="Apaga o perfil de sessão (cookies); o próximo uso refaz login pelo .env")

    recover_parser = subparsers.add_parser("recover-push", help="Recover lost repo connection and push")
    recover_parser.add_argument("--repo", required=True, help="GitHub repository name")
    recover_parser.add_argument("--branch", required=True, help="Branch identifier/number")
    recover_parser.add_argument("--message", default=None, help="Custom push instruction")

    args = parser.parse_args()
    purge_legacy_session_dirs()

    if args.command == "purge-session":
        removed = purge_session_dir(DEFAULT_USER_DATA_DIR)
        print(json.dumps({"purged": removed, "path": DEFAULT_USER_DATA_DIR}, indent=2))
        return

    async def run():
        is_visible = getattr(args, "visible", False)
        async with ArenaController(visible=is_visible) as controller:
            if args.command == "login":
                logged_in = await controller.ensure_logged_in()
                print(json.dumps({"logged_in": logged_in, "email_configured": bool(ARENA_EMAIL)}, indent=2))

            elif args.command == "list-repos":
                repos = await controller.list_repositories()
                print(json.dumps({"repositories": repos}, indent=2))

            elif args.command == "status":
                state = await controller.get_current_state()
                print(json.dumps(state, indent=2))

            elif args.command == "connect":
                await controller.select_repository(args.repo, args.branch)
                state = await controller.get_current_state()
                print(json.dumps(state, indent=2))

            elif args.command == "send":
                prompt_text = args.prompt or ""
                if args.prompt_file and not os.path.exists(args.prompt_file):
                    raise FileNotFoundError(f"--prompt-file não encontrado: {args.prompt_file}")
                if args.prompt_file:
                    with open(args.prompt_file, "r", encoding="utf-8") as f:
                        prompt_text = f.read()
                if not prompt_text.strip():
                    raise ValueError("Prompt cannot be empty (provide --prompt or --prompt-file).")
                res = await controller.send_prompt(
                    prompt_text,
                    repo=args.repo,
                    branch=args.branch,
                    timeout_seconds=args.timeout,
                    conversation_url=args.conversation_url,
                    skill_prefix=not args.no_skill_prefix,
                    auto_failover=not args.no_failover,
                    max_failovers=args.max_failovers
                )
                emit_result(res)

            elif args.command == "recover-push":
                res = await controller.recover_and_push(args.repo, args.branch, args.message)
                emit_result(res)

    asyncio.run(run())


if __name__ == "__main__":
    main()
