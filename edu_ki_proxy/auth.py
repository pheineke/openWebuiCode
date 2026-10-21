"""Shibboleth login via a real browser, and session capture for Open WebUI.

The front door (edu-ki-rlp.de) authenticates via the university IdP and then
Open WebUI trusts it (auth_trusted_header). We never see credentials: the user
logs in inside Chromium, we only read the resulting cookies and the Open WebUI
JWT from localStorage.
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = os.environ.get("EDU_KI_BASE", "https://chat-test.edu-ki-rlp.de").rstrip("/")
STATE_DIR = Path(os.environ.get("EDU_KI_HOME", Path.home() / ".config" / "edu-ki-proxy"))
STATE_FILE = STATE_DIR / "state.json"
PROFILE_DIR = STATE_DIR / "browser-profile"


def system_browser() -> str | None:
    """A regular Chromium/Chrome is preferred: Playwright's own build shows a 'Chrome for Testing' banner."""
    import shutil
    env = os.environ.get("EDU_KI_BROWSER")
    return env or next((p for n in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome") if (p := shutil.which(n))), None)


class LoginRequired(Exception):
    pass


def load_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    tmp.replace(STATE_FILE)


def clear_state() -> None:
    """Forget the session, including the browser profile (so the next login is clean)."""
    import shutil
    STATE_FILE.unlink(missing_ok=True)
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)


def cookie_header(state: dict) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in state["cookies"])


def _capture(ctx, page) -> dict | None:
    """Return {token, cookies} once Open WebUI is loaded and logged in."""
    host = urlparse(BASE).hostname
    if urlparse(page.url).hostname != host:
        return None
    try:
        token = page.evaluate("localStorage.getItem('token')")
    except Exception:  # page mid-navigation
        return None
    if not token:
        return None
    cookies = [c for c in ctx.cookies(BASE) if c["domain"].lstrip(".") in host or host.endswith(c["domain"].lstrip("."))]
    return {"token": token, "cookies": cookies, "captured_at": int(time.time())}


def _run(headless: bool, timeout_s: int) -> dict | None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless, executable_path=system_browser())
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                state = _capture(ctx, page)
                if state:
                    save_state(state)
                    return state
                page.wait_for_timeout(500)
            return None
        finally:
            ctx.close()


def interactive_login(timeout_s: int = 600) -> dict:
    print("Browser opened: choose your Hochschule and log in. Waiting...")
    state = _run(headless=False, timeout_s=timeout_s)
    if not state:
        raise LoginRequired("login timed out")
    return state


def silent_refresh() -> dict:
    """Reuse the persistent browser profile without UI; works while the IdP/front-door session is alive."""
    if not PROFILE_DIR.exists():   # logged in through the native window: there is no Chromium profile to reuse
        raise LoginRequired("session expired, please log in again")
    state = _run(headless=True, timeout_s=25)
    if not state:
        raise LoginRequired("session expired, run: python -m edu_ki_proxy login")
    return state
