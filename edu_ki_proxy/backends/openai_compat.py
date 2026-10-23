"""Any OpenAI-compatible HTTP API: OpenAI, OpenRouter, Ollama, LM Studio, Gemini, ..."""
from urllib.parse import urlparse

import httpx

from .. import usage
from .base import Backend, ProxyError, relay

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class OpenAICompat(Backend):
    type = "openai"
    label = "OpenAI-kompatibel"
    hint = "Basis-URL inkl. /v1, z. B. https://api.openai.com/v1, https://openrouter.ai/api/v1 oder http://127.0.0.1:11434/v1."
    fields = ("name", "base_url", "api_key", "models")

    def _url(self, p: dict, path: str) -> str:
        return (p.get("base_url") or "").rstrip("/") + path

    def _auth(self, p: dict) -> dict:
        return {"Authorization": f"Bearer {p['api_key']}"} if p.get("api_key") else {}

    def logged_in(self, p: dict) -> bool:
        if p.get("api_key"):
            return True
        return (urlparse(p.get("base_url") or "").hostname or "").lower() in LOCAL_HOSTS   # Ollama / LM Studio need no key

    def public(self, p: dict) -> dict:
        return {"models": p.get("models") or [], "has_key": bool(p.get("api_key"))}

    async def _remote_models(self, p: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20) as c:
            return await c.get(self._url(p, "/models"), headers=self._auth(p))

    async def models(self, p: dict) -> list[tuple[str, str]]:
        mids = [str(m) for m in (p.get("models") or []) if str(m).strip()]
        if not mids:
            try:
                r = await self._remote_models(p)
                if r.status_code == 200:
                    mids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            except (httpx.HTTPError, ValueError):
                pass
        return [(m, m) for m in mids]

    async def chat(self, p: dict, data: dict):
        cli = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), follow_redirects=False)
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "Accept": "text/event-stream" if data.get("stream") else "application/json", **self._auth(p)}
        try:
            resp = await cli.send(cli.build_request("POST", self._url(p, "/chat/completions"), headers=headers, json=data),
                                  stream=True)
        except BaseException:
            await cli.aclose()
            raise
        if resp.status_code in (401, 403):
            await resp.aclose()
            await cli.aclose()
            raise ProxyError(f"Anbieter {p.get('name') or p.get('id')} lehnt den API-Schlüssel ab.", 401)
        tap = usage.Tap(p["id"], data.get("model"), "text/event-stream" in resp.headers.get("content-type", ""))
        return relay(resp, cli, tap)

    async def test(self, p: dict) -> dict:
        try:
            r = await self._remote_models(p)
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)}
        if r.status_code >= 400:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        return {"ok": True, "models": len(r.json().get("data") or [])}
