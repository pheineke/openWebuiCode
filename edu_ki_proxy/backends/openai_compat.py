"""Any OpenAI-compatible HTTP API: OpenAI, OpenRouter, Ollama, LM Studio, Gemini, ..."""
import re

import httpx

from .. import providers, usage
from .base import Backend, ProxyError, relay

VERSION = re.compile(r"/v\d+\w*$")          # .../v1, .../v1beta
NOT_CHAT = re.compile(r"embed", re.I)        # embedding models in the list (LM Studio, OpenAI) cannot chat
MODELS_TIMEOUT = httpx.Timeout(20, connect=5)


def _model_ids(r: httpx.Response) -> list[str] | None:
    """The ids in a model list; None when the answer is no model list (an error page, or LM Studio's
    {"error": "Unexpected endpoint"} with status 200)."""
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    items = body.get("data", body.get("models")) if isinstance(body, dict) else body
    if not isinstance(items, list):
        return None
    ids = [m if isinstance(m, str) else m.get("id") or m.get("name") for m in items if isinstance(m, (str, dict))]
    return [str(i) for i in ids if i and not NOT_CHAT.search(str(i))]


class OpenAICompat(Backend):
    type = "openai"
    label = "OpenAI-kompatibel"
    hint = ("Basis-URL inkl. /v1, z. B. https://api.openai.com/v1, https://openrouter.ai/api/v1 oder "
            "http://192.168.0.10:1234/v1 (LM Studio). Die Modelle kommen von <Basis-URL>/models.")
    fields = ("name", "base_url", "api_key", "models")

    def _url(self, p: dict, path: str) -> str:
        return (p.get("base_url") or "").rstrip("/") + path

    def _auth(self, p: dict) -> dict:
        return {"Authorization": f"Bearer {p['api_key']}"} if p.get("api_key") else {}

    def logged_in(self, p: dict) -> bool:
        # Nothing to log in to. A server on the network (LM Studio, Ollama) needs no key, and one that
        # does says so itself with a 401.
        return bool(p.get("base_url"))

    def public(self, p: dict) -> dict:
        return {"models": p.get("models") or [], "has_key": bool(p.get("api_key"))}

    def _model_urls(self, p: dict) -> list[str]:
        """Where the model list may be: below the base URL, or below /v1 when the base URL has no version."""
        base = (p.get("base_url") or "").rstrip("/")
        return [base + "/models"] + ([] if VERSION.search(base) else [base + "/v1/models"])

    async def _remote_models(self, p: dict) -> list[str]:
        """The server's model ids; raises ProxyError with the reason when it lists none."""
        name, tried = p.get("name") or p.get("id"), []
        async with httpx.AsyncClient(timeout=MODELS_TIMEOUT) as c:
            for url in self._model_urls(p):
                try:
                    r = await c.get(url, headers=self._auth(p))
                except httpx.HTTPError as e:      # the server is unreachable: under /v1 it is too
                    raise ProxyError(f"{url} nicht erreichbar: {str(e) or type(e).__name__}", 502) from e
                if r.status_code in (401, 403):
                    raise ProxyError(f"{name} lehnt den API-Schlüssel ab." if p.get("api_key")
                                     else f"{name} verlangt einen API-Schlüssel.", 401)
                ids = _model_ids(r)
                if ids is not None:
                    base = (p.get("base_url") or "").rstrip("/")
                    if url == base + "/v1/models" and p.get("id"):   # base URL without /v1: chats need it too
                        providers.update(p["id"], base_url=base + "/v1")
                    return ids
                tried.append(f"{url}: HTTP {r.status_code}" if r.status_code != 200 else f"{url}: keine Modellliste")
        raise ProxyError(f"{name} liefert keine Modellliste ({'; '.join(tried)}).", 502)

    async def models(self, p: dict) -> list[tuple[str, str]]:
        mids = [str(m) for m in (p.get("models") or []) if str(m).strip()] or await self._remote_models(p)
        return [(m, m) for m in mids]

    async def chat(self, p: dict, data: dict):
        cli = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), follow_redirects=False)
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "Accept": "text/event-stream" if data.get("stream") else "application/json", **self._auth(p)}
        try:
            resp = await cli.send(cli.build_request("POST", self._url(p, "/chat/completions"), headers=headers, json=data),
                                  stream=True)
        except httpx.HTTPError as e:
            await cli.aclose()
            raise ProxyError(f"{p.get('name') or p.get('id')} nicht erreichbar: {str(e) or type(e).__name__}", 502) from e
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
            return {"ok": True, "models": len(await self._remote_models(p))}
        except ProxyError as e:
            return {"ok": False, "error": e.message}
