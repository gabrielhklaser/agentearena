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
import sys
import json
import time
import argparse
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Load environment variables from .env
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
if not ENV_PATH.exists():
    ENV_PATH = SCRIPT_DIR.parent / ".env"

if ENV_PATH.exists():
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

ARENA_EMAIL = os.environ.get("ARENA_EMAIL", "")
ARENA_PASSWORD = os.environ.get("ARENA_PASSWORD", "")
DEFAULT_USER_DATA_DIR = os.environ.get(
    "ARENA_USER_DATA_DIR",
    str(SCRIPT_DIR / "arena_user_data")
)

BASE_URL = "https://arena.ai"
AGENT_URL = "https://arena.ai/agent"


class ArenaController:
    def __init__(self, user_data_dir=None, headless=True):
        self.user_data_dir = user_data_dir or DEFAULT_USER_DATA_DIR
        os.makedirs(self.user_data_dir, exist_ok=True)
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def ensure_logged_in(self):
        """Verifies authentication and performs login if necessary."""
        print(f"[*] Navigating to {BASE_URL}...")
        await self.page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        await self.page.wait_for_timeout(1000)

        # Check if already logged in
        if ARENA_EMAIL:
            user_btn = self.page.locator(f'button:has-text("{ARENA_EMAIL}")')
            if await user_btn.count() > 0 and await user_btn.first.is_visible():
                print(f"[+] Already logged in as {ARENA_EMAIL}")
                return True

        # Open sidebar if needed
        toggle_btn = self.page.locator('button[aria-label="Toggle Sidebar"], button[aria-label="Open sidebar"]')
        if await toggle_btn.count() > 0:
            await toggle_btn.first.click()
            await self.page.wait_for_timeout(500)

        login_btn = self.page.locator('button:has-text("Log In")')
        if await login_btn.count() > 0 and await login_btn.first.is_visible():
            if not ARENA_EMAIL or not ARENA_PASSWORD:
                raise ValueError("Missing ARENA_EMAIL or ARENA_PASSWORD in .env file.")

            print(f"[*] Performing login with {ARENA_EMAIL}...")
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
            print("[+] Login successfully completed.")
            return True

        return False

    async def open_agent_mode(self):
        """Navigates to the Agent Mode workspace."""
        await self.ensure_logged_in()
        if not self.page.url.startswith(AGENT_URL):
            print(f"[*] Navigating to Agent Mode: {AGENT_URL}...")
            await self.page.goto(AGENT_URL, wait_until="networkidle", timeout=60000)
            await self.page.wait_for_timeout(1500)

    async def ensure_github_enabled(self):
        """Ensures the GitHub connection toggle is activated in Agent Mode."""
        await self.open_agent_mode()
        repo_btn = self.page.locator('button:has-text("Select a repository"), div:has-text("Select a repository")')
        if await repo_btn.count() > 0 and await repo_btn.first.is_visible():
            return True

        add_btn = self.page.locator('button[aria-label="Add files and connections"]')
        if await add_btn.count() > 0:
            await add_btn.first.click()
            await self.page.wait_for_timeout(500)
            github_switch = self.page.locator('button[role="switch"]')
            if await github_switch.count() > 0:
                await github_switch.first.click()
                await self.page.wait_for_timeout(1000)
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(500)
            return True

        return False

    async def list_repositories(self):
        """Returns the list of available GitHub repositories connected to the account."""
        await self.ensure_github_enabled()
        print("[*] Opening repository selector...")
        await self.page.click('button:has-text("Select a repository")')
        await self.page.wait_for_timeout(1000)

        repos = await self.page.eval_on_selector_all(
            '[role="option"], div[data-radix-collection-item]',
            """elements => elements.map(el => el.innerText.trim()).filter(x => x && !x.startsWith('+'))"""
        )
        await self.page.keyboard.press("Escape")
        await self.page.wait_for_timeout(300)
        return repos

    async def select_repository(self, repo_name, branch_name=None):
        """Selects a specific GitHub repository and optionally a branch."""
        await self.ensure_github_enabled()
        print(f"[*] Selecting repository '{repo_name}'...")

        selected_pill = self.page.locator(f'button:has-text("{repo_name}")')
        if await selected_pill.count() > 0 and await selected_pill.first.is_visible():
            print(f"[+] Repository '{repo_name}' is already active.")
        else:
            repo_btn = self.page.locator('button:has-text("Select a repository")')
            if await repo_btn.count() > 0:
                await repo_btn.first.click()
                await self.page.wait_for_timeout(800)
                target_option = self.page.locator(f'[role="option"]:has-text("{repo_name}")')
                if await target_option.count() > 0:
                    await target_option.first.click()
                    await self.page.wait_for_timeout(1500)
                    print(f"[+] Repository '{repo_name}' selected.")
                else:
                    raise ValueError(f"Repository '{repo_name}' not found in available list.")

        if branch_name:
            print(f"[*] Selecting branch '{branch_name}'...")
            branch_btn = self.page.locator('button:has-text("Branch"), button[aria-label*="branch" i]').first
            if await branch_btn.count() > 0:
                await branch_btn.click()
                await self.page.wait_for_timeout(800)
                b_opt = self.page.locator(f'[role="option"]:has-text("{branch_name}")')
                if await b_opt.count() > 0:
                    await b_opt.first.click()
                    await self.page.wait_for_timeout(1000)
                    print(f"[+] Branch '{branch_name}' selected.")

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
            "elements": elements[:10]
        }

    async def send_prompt(self, prompt, repo=None, branch=None, timeout_seconds=120):
        """
        Sends a prompt to the Arena AI Agent.
        Monitors output until completion and returns the generated response.
        """
        await self.open_agent_mode()
        if repo:
            await self.select_repository(repo, branch)

        print(f"[*] Typing prompt: {prompt[:80]}...")
        editor = self.page.locator('div.tiptap.ProseMirror')
        await editor.click()
        await self.page.keyboard.type(prompt, delay=10)
        await self.page.wait_for_timeout(500)

        send_btn = self.page.locator('button[aria-label="Send message"]')
        await send_btn.click()
        print("[*] Prompt sent. Waiting for agent generation...")

        start_time = time.time()
        last_text_length = 0
        stable_count = 0

        while time.time() - start_time < timeout_seconds:
            await self.page.wait_for_timeout(3000)

            stop_btn = self.page.locator('button[aria-label*="Stop" i], button:has-text("Stop")')
            is_generating = await stop_btn.count() > 0 and await stop_btn.first.is_visible()

            messages = await self.page.eval_on_selector_all(
                '[data-message-author="assistant"], div.prose, [data-testid="agent-message"]',
                """els => els.map(e => e.innerText.trim()).filter(Boolean)"""
            )

            current_text = "\n\n".join(messages) if messages else ""
            if len(current_text) == last_text_length and len(current_text) > 0 and not is_generating:
                stable_count += 1
                if stable_count >= 2:
                    print("[+] Generation finished.")
                    break
            else:
                stable_count = 0
                last_text_length = len(current_text)

        conversation_url = self.page.url
        return {
            "conversation_url": conversation_url,
            "response": current_text or "Prompt dispatched successfully to Arena AI Agent.",
            "status": "success"
        }

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
        await self.page.goto(AGENT_URL, wait_until="networkidle", timeout=60000)
        await self.page.wait_for_timeout(2000)

        print(f"[*] Step 2: Re-establishing repository connection to '{repo}' and branch '{branch}'...")
        await self.select_repository(repo, branch)

        push_instruction = custom_message or (
            f"Por favor, recarregue todas as alterações e arquivos da branch '{branch}' "
            f"trabalhados na conversa anterior e execute o push de todas as modificações para o repositório '{repo}' no GitHub."
        )

        print(f"[*] Step 3: Sending reload and push command...")
        result = await self.send_prompt(push_instruction, repo=None, branch=None)
        print("[+] Recovery prompt executed. Changes reloaded and push triggered.")
        return result


def main():
    parser = argparse.ArgumentParser(description="Arena AI Platform Controller")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Ensure login session is active")
    subparsers.add_parser("list-repos", help="List connected GitHub repositories")
    subparsers.add_parser("status", help="Check current session and repository status")

    send_parser = subparsers.add_parser("send", help="Send a prompt to Arena AI agent")
    send_parser.add_argument("--prompt", required=True, help="Prompt text to send")
    send_parser.add_argument("--repo", default=None, help="Target GitHub repository")
    send_parser.add_argument("--branch", default=None, help="Target branch name")

    recover_parser = subparsers.add_parser("recover-push", help="Recover lost repo connection and push")
    recover_parser.add_argument("--repo", required=True, help="GitHub repository name")
    recover_parser.add_argument("--branch", required=True, help="Branch identifier/number")
    recover_parser.add_argument("--message", default=None, help="Custom push instruction")

    args = parser.parse_args()

    async def run():
        async with ArenaController(headless=True) as controller:
            if args.command == "login":
                logged_in = await controller.ensure_logged_in()
                print(json.dumps({"logged_in": logged_in, "email": ARENA_EMAIL}, indent=2))

            elif args.command == "list-repos":
                repos = await controller.list_repositories()
                print(json.dumps({"repositories": repos}, indent=2))

            elif args.command == "status":
                state = await controller.get_current_state()
                print(json.dumps(state, indent=2))

            elif args.command == "send":
                res = await controller.send_prompt(args.prompt, repo=args.repo, branch=args.branch)
                print(json.dumps(res, indent=2))

            elif args.command == "recover-push":
                res = await controller.recover_and_push(args.repo, args.branch, args.message)
                print(json.dumps(res, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    main()
