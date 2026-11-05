"""Common provider backend interface.

A backend knows how to log in to one kind of service, list its models and run a chat completion
in OpenAI format.  Services that keep their own chat history (Open WebUI) also expose it; for the
others the app stores chats locally (chats.py).
"""
import asyncio

import httpx
from fastapi.responses import StreamingResponse

from .. import auth, providers, usage

_refresh_locks: dict[str, asyncio.Lock] = {}


class ProxyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def token(p: dict) -> str | None:
    return (p.get("session") or {}).get("token") or None


def relay(resp: httpx.Response, client: httpx.AsyncClient, tap: usage.Tap | None = None) -> StreamingResponse:
    """Pass an upstream response through unchanged (minus hop-by-hop headers), recording usage."""
    out = {k: v for k, v in resp.headers.items()
           if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}}

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                if tap:
                    tap.feed(chunk)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            if tap:
                tap.finish()

    return StreamingResponse(body_iter(), status_code=resp.status_code, headers=out)


class Backend:
    type = ""
    label = ""
    hint = ""
    login: str | None = None      # "window": inside the app window, "browser": system Chromium, None: API key only
    fields: tuple = ("name", "base_url")   # what the "add provider" form asks for
    default_base = ""
    base_required = True
    history = False               # keeps its own chat history
    native_tools = True           # passes OpenAI tools on; models listed in emulate_tools get them via the prompt

    def info(self) -> dict:
        return {"label": self.label, "hint": self.hint, "login": self.login, "fields": list(self.fields),
                "default_base": self.default_base, "base_required": self.base_required, "history": self.history}

    # ---- session ------------------------------------------------------------------------------
    def token_js(self) -> str:
        """JS expression that yields the web app's session token (empty while logged out)."""
        return "localStorage.getItem('token') || ''"

    def logged_in(self, p: dict) -> bool:
        s = p.get("session") or {}
        return bool(s.get("token")) and not s.get("expired")

    def public(self, p: dict) -> dict:
        """Extra, non-secret fields for the UI."""
        return {}

    def check_session(self, p: dict, state: dict) -> bool:
        """Is a freshly captured session really logged in?  Runs in login threads (sync).

        May add ``state["user"]``.  Answers False only when the service clearly rejects the
        session, so a network hiccup never blocks a login.
        """
        return True

    async def refresh(self, p: dict, stale: str | None) -> dict:
        """New session from the persistent browser profile, without UI.  Marks the provider
        expired and raises 401 when that is not possible."""
        pid = p["id"]
        async with _refresh_locks.setdefault(pid, asyncio.Lock()):
            cur = providers.get(pid) or p
            if token(cur) != stale and self.logged_in(cur):
                return cur            # another request already refreshed
            try:
                state = await self.silent_login(cur)
            except Exception as e:
                providers.mark_expired(pid)
                raise ProxyError(f"{cur.get('name') or pid}: Sitzung abgelaufen, bitte erneut anmelden.", 401) from e
            providers.set_session(pid, state)
            return providers.get(pid) or cur

    async def silent_login(self, p: dict) -> dict:
        """A fresh session without asking the person; raises when that is not possible."""
        return await asyncio.to_thread(auth.silent_refresh, p, self)

    # ---- API ----------------------------------------------------------------------------------
    async def models(self, p: dict) -> list[tuple[str, str]]:
        """(model id, display name) pairs."""
        return []

    async def chat(self, p: dict, data: dict):
        """OpenAI chat completion; ``data["model"]`` is already the provider's own model id."""
        raise ProxyError("Dieser Anbieter unterstützt keine Chats.", 400)

    async def user(self, p: dict) -> dict | None:
        return (p.get("session") or {}).get("user")

    async def skills(self, p: dict) -> list[dict]:
        """Skills (reusable instructions) the service offers: [{"id", "name", "description"}]."""
        return []

    async def skill(self, p: dict, sid: str) -> str:
        """A skill's instructions."""
        raise ProxyError("Skill nicht gefunden.", 404)

    async def web_search_info(self, p: dict) -> dict | None:
        """{"notice": text the user should confirm, or ""} if the service can search the web, else None."""
        return None

    async def web_search(self, p: dict, messages: list[dict], model: str | None) -> dict:
        """{"queries": [...], "results": [{"title", "url", "content"}]} for the conversation's last question."""
        raise ProxyError("Dieser Anbieter kann nicht im Web suchen.", 400)

    async def model_icon(self, p: dict, model: str) -> tuple[bytes, str] | None:
        """(image bytes, content type) of a model's picture, if the service has one."""
        return None

    async def test(self, p: dict) -> dict:
        models = await self.models(p)
        return {"ok": bool(models), "models": len(models)} if models else {"ok": False, "error": "keine Modelle"}

    # ---- own chat history (history = True) ----------------------------------------------------
    async def chat_list(self, p: dict, limit: int = 100) -> list[dict]:
        return []

    async def chat_search(self, p: dict, q: str) -> list[dict]:
        return []

    async def chat_get(self, p: dict, cid: str) -> dict:
        raise ProxyError("Chat nicht gefunden.", 404)

    async def chat_delete(self, p: dict, cid: str) -> None:
        raise ProxyError("Chat nicht gefunden.", 404)

    async def chat_rename(self, p: dict, cid: str, title: str) -> None:
        raise ProxyError("Chat nicht gefunden.", 404)

    async def chat_pin(self, p: dict, cid: str, pinned: bool) -> None:
        raise ProxyError("Chat nicht gefunden.", 404)
