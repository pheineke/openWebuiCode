"""Browser session capture for providers.

The user logs in inside a real browser.  We never see credentials: we only read
the resulting cookies and the token the web app keeps in localStorage.  This
works for Open WebUI/Shibboleth and for chat.deepseek.com (email/phone, Google
or Apple login).  Storing the captured session is up to the caller.
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

BASE = os.environ.get("EDU_KI_BASE", "https://chat-test.edu-ki-rlp.de").rstrip("/")
STATE_DIR = Path(os.environ.get("EDU_KI_HOME", Path.home() / ".config" / "edu-ki-proxy"))
STATE_FILE = STATE_DIR / "state.json"      # pre-provider session file, only read to seed providers.json
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


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


def cookie_header(state: dict) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in state.get("cookies", []))


# ---- provider-aware browser login -------------------------------------------------------------
def profile_dir(provider: dict) -> Path:
    pid = provider.get("id") or "default"
    p = STATE_DIR / f"browser-profile-{pid}"
    if pid == "eduki" and not p.exists() and PROFILE_DIR.exists():
        return PROFILE_DIR                       # keep the old profile for existing installations
    return p


def clear_profile(provider: dict) -> None:
    import shutil
    shutil.rmtree(profile_dir(provider), ignore_errors=True)


def _capture(ctx, provider: dict, backend) -> dict | None:
    """Return {token, cookies, user_agent} once a tab shows the provider's app logged in.

    All tabs are checked: Google and Apple sign-in run in popups and may finish in any of them.
    """
    base = provider["base_url"]
    host = urlparse(base).hostname
    for page in list(ctx.pages):
        if urlparse(page.url).hostname != host:
            continue
        try:
            token = page.evaluate(backend.token_js())
            user_agent = page.evaluate("navigator.userAgent")
        except Exception:  # page mid-navigation or closed
            continue
        if not token:
            continue
        cookies = [c for c in ctx.cookies(base)
                   if (d := c["domain"].lstrip(".")) and (host == d or host.endswith("." + d))]
        # headless runs say so in the UA; requests made with it would stand out
        user_agent = (user_agent or "").replace("HeadlessChrome", "Chrome") or None
        return {"token": token, "cookies": cookies, "user_agent": user_agent, "captured_at": int(time.time())}
    return None


def _run(provider: dict, backend, headless: bool, timeout_s: int) -> dict | None:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    d = profile_dir(provider)
    d.mkdir(parents=True, exist_ok=True)
    rejected: set[str] = set()
    with sync_playwright() as p:
        # Without the automation switches the window is an ordinary browser to the sites; Google's
        # sign-in in particular refuses browsers that announce they are remote controlled.
        ctx = p.chromium.launch_persistent_context(
            str(d), headless=headless, executable_path=system_browser(), no_viewport=not headless,
            ignore_default_args=["--enable-automation"], args=["--disable-blink-features=AutomationControlled"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(provider["base_url"])
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                state = _capture(ctx, provider, backend)
                if state and state["token"] not in rejected:
                    if backend.check_session(provider, state):
                        return state
                    rejected.add(state["token"])      # stale token left in the profile: wait for a fresh login
                if not ctx.pages:
                    raise LoginRequired("Browserfenster wurde geschlossen")
                ctx.pages[0].wait_for_timeout(500)
            return None
        except PlaywrightError as e:
            if not ctx.pages or "closed" in str(e).lower():
                raise LoginRequired("Browserfenster wurde geschlossen") from e
            raise
        finally:
            try:
                ctx.close()
            except PlaywrightError:
                pass


def interactive_login(provider: dict, backend, timeout_s: int = 600) -> dict:
    print(f"Browser geöffnet: bei {provider.get('name') or provider['base_url']} anmelden …")
    state = _run(provider, backend, headless=False, timeout_s=timeout_s)
    if not state:
        raise LoginRequired("Zeitüberschreitung bei der Anmeldung")
    return state


def silent_refresh(provider: dict, backend) -> dict:
    """Reuse the persistent browser profile without UI; works while the site's session is alive."""
    if not profile_dir(provider).exists():
        raise LoginRequired("Sitzung abgelaufen, bitte erneut anmelden")
    state = _run(provider, backend, headless=True, timeout_s=25)
    if not state:
        raise LoginRequired("Sitzung abgelaufen, bitte erneut anmelden")
    return state
