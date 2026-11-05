"""Open WebUI behind any login (EDU-KI-RLP's Shibboleth front door is the preconfigured one).

Requests carry the captured session (cookies + bearer token).  When the front door's session
runs out it answers 3xx to its login page; Open WebUI itself answers 401.  Either way one silent
refresh from the persistent browser profile is tried before giving up.
"""
import asyncio
import json
import re
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from .. import auth, shibboleth, usage
from .base import Backend, ProxyError, relay, token

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
       "authorization", "cookie", "accept-encoding"}


def _headers(state: dict) -> dict:
    return {"Authorization": f"Bearer {state.get('token', '')}", "Cookie": auth.cookie_header(state),
            "Accept-Encoding": "identity"}


def _expired(resp: httpx.Response) -> bool:
    """Session gone?  A JSON 403 is Open WebUI refusing one thing (a model, an admin page), not a logout."""
    if resp.status_code == 403:
        return "application/json" not in resp.headers.get("content-type", "")
    return resp.status_code == 401 or 300 <= resp.status_code < 400


class OpenWebUI(Backend):
    type = "openwebui"
    label = "Open WebUI"
    hint = "Basis-URL der Instanz, z. B. https://chat.example.de. Die Anmeldung läuft im App-Fenster."
    login = "window"
    history = True

    def _client(self, p: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=(p.get("base_url") or "").rstrip("/"),
                                 timeout=httpx.Timeout(600, connect=20), follow_redirects=False)

    async def silent_login(self, p: dict) -> dict:
        """Logged in with the app's own form: the university's session still knows the person, so a
        new Shibboleth round needs no password.  Otherwise the browser profile, as before."""
        shib = (p.get("session") or {}).get("shib")
        if shib and shib.get("entity_id"):
            return await shibboleth.refresh(p["base_url"], shib)
        return await super().silent_login(p)

    def check_session(self, p: dict, state: dict) -> bool:
        try:
            r = httpx.get((p.get("base_url") or "").rstrip("/") + "/api/v1/auths/", headers=_headers(state),
                          timeout=20, follow_redirects=False)
        except httpx.HTTPError:
            return True
        if _expired(r):
            return False
        try:
            u = r.json()
            state["user"] = {"name": u.get("name"), "email": u.get("email")}
        except (ValueError, AttributeError):
            pass
        return True

    async def send(self, p: dict, method: str, path: str, *, params=None, headers=None, content=None,
                   stream: bool = False) -> tuple[httpx.Response, httpx.AsyncClient]:
        """One request to /api/<path>, retried once after a silent refresh.  With stream=True the
        caller owns (and closes) the response and the client."""
        cli = self._client(p)
        try:
            for attempt in (0, 1):
                req = cli.build_request(method, f"/api/{path.lstrip('/')}", params=params, content=content,
                                        headers={**(headers or {}), **_headers(p.get("session") or {})})
                resp = await cli.send(req, stream=stream)
                if not _expired(resp) or attempt:
                    if not stream:
                        await cli.aclose()
                    return resp, cli
                await resp.aclose()
                p = await self.refresh(p, token(p))
        except BaseException:
            await cli.aclose()
            raise
        raise AssertionError("unreachable")

    async def get(self, p: dict, path: str, **params) -> httpx.Response:
        resp, _ = await self.send(p, "GET", path, params=params or None)
        return resp

    async def forward(self, p: dict, path: str, request: Request, tap_model: str | None = None) -> StreamingResponse:
        """Raw pass-through for the app's own Open WebUI calls (/ow/*)."""
        params = {k: v for k, v in request.query_params.items() if k != "provider"}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        resp, cli = await self.send(p, request.method, path, params=params, headers=headers,
                                    content=await request.body(), stream=True)
        tap = usage.Tap(p["id"], tap_model, "text/event-stream" in resp.headers.get("content-type", "")) if tap_model else None
        return relay(resp, cli, tap)

    # ---- Backend API --------------------------------------------------------------------------
    async def models(self, p: dict) -> list[tuple[str, str]]:
        try:
            r = await self.get(p, "models")
            if r.status_code != 200:
                return []
            return [(m["id"], m.get("name") or m["id"]) for m in r.json().get("data") or [] if m.get("id")]
        except (httpx.HTTPError, ValueError):
            return []

    async def chat(self, p: dict, data: dict):
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if data.get("stream") else "application/json"}
        resp, cli = await self.send(p, "POST", "chat/completions", headers=headers,
                                    content=json.dumps(data).encode(), stream=True)
        return relay(resp, cli, usage.Tap(p["id"], data.get("model"),
                                          "text/event-stream" in resp.headers.get("content-type", "")))

    async def skills(self, p: dict) -> list[dict]:
        """Skills from the workspace (Arbeitsbereich -> Skills); versions without them answer with their web
        page or an error, which counts as none."""
        try:
            r = await self.get(p, "v1/skills/")
            items = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError, ProxyError):
            return []
        if isinstance(items, dict):
            items = items.get("items") or []
        return [{"id": s["id"], "name": s.get("name") or s["id"], "description": s.get("description") or ""}
                for s in items if isinstance(s, dict) and s.get("id") and s.get("is_active", True)]

    async def skill(self, p: dict, sid: str) -> str:
        r = await self.get(p, f"v1/skills/id/{quote(sid, safe='')}")
        try:
            content = r.json().get("content") if r.status_code == 200 else None
        except (ValueError, AttributeError):
            content = None
        if not isinstance(content, str):
            raise ProxyError("Skill nicht gefunden.", 404)
        return content

    async def web_search_info(self, p: dict) -> dict | None:
        try:
            r = await self.get(p, "config")
            features = (r.json() or {}).get("features") or {} if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError, ProxyError):
            return None
        if not features.get("enable_web_search"):
            return None
        notice = features.get("web_search_confirmation_content") if features.get("enable_web_search_confirmation") else ""
        return {"notice": str(notice or "")}

    async def _post_json(self, p: dict, path: str, body: dict) -> httpx.Response:
        resp, _ = await self.send(p, "POST", path, headers={"Content-Type": "application/json"},
                                  content=json.dumps(body).encode())
        return resp

    async def web_search(self, p: dict, messages: list[dict], model: str | None) -> dict:
        """Open WebUI's web search, the way its own web UI runs it for a message: queries from its task model,
        the search (its configured engine), then the pages, either returned directly or from the vector store."""
        question = next((m["content"] for m in reversed(messages) if m.get("role") == "user" and m.get("content")), "")
        if not question.strip():
            raise ProxyError("Keine Frage zum Suchen.", 400)
        queries = [question.strip()[:300]]
        if model:
            try:
                r = await self._post_json(p, "v1/tasks/queries/completions",
                                          {"model": model, "messages": messages, "prompt": question, "type": "web_search"})
                text = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else ""
                found = json.loads(text[text.rfind("{"):text.rfind("}") + 1]).get("queries") if "{" in text else None
                if isinstance(found, list) and any(isinstance(q, str) and q.strip() for q in found):
                    queries = [q.strip() for q in found if isinstance(q, str) and q.strip()][:3]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError, ProxyError):
                pass                       # query generation is a nicety: the question itself works too
        r = await self._post_json(p, "v1/retrieval/process/web/search", {"queries": queries})
        if r.status_code == 404:
            raise ProxyError("Die Websuche hat nichts gefunden.", 404)
        if r.status_code != 200:
            try:
                detail = (r.json() or {}).get("detail")
            except ValueError:
                detail = None
            raise ProxyError(f"Websuche fehlgeschlagen: {detail or 'HTTP ' + str(r.status_code)}", 502 if r.status_code >= 500 else 400)
        d = r.json() or {}
        titles = {i.get("link"): i.get("title") for i in d.get("items") or [] if isinstance(i, dict)}
        docs = d.get("docs")
        if docs is None and d.get("collection_names"):
            r = await self._post_json(p, "v1/retrieval/query/collection",
                                      {"collection_names": d["collection_names"], "query": question, "k": 8})
            q = r.json() if r.status_code == 200 else {}
            docs = [{"content": c, "metadata": m or {}} for c, m in zip((q.get("documents") or [[]])[0], (q.get("metadatas") or [[]])[0])]
        results: dict[str, dict] = {}               # one entry per page, its pieces joined
        for doc in docs or []:
            meta = doc.get("metadata") or {}
            url = meta.get("source") or meta.get("link")
            if not url or not str(doc.get("content") or "").strip():
                continue
            entry = results.setdefault(url, {"title": meta.get("title") or titles.get(url) or url, "url": url, "content": ""})
            entry["content"] = (entry["content"] + "\n…\n" + doc["content"].strip()).strip("\n…") [:4000]
        return {"queries": queries, "results": list(results.values())[:8]}

    async def model_icon(self, p: dict, model: str) -> tuple[bytes, str] | None:
        """The picture Open WebUI shows for a model (newer versions serve it separately, not in /models)."""
        try:
            r = await self.get(p, "v1/models/model/profile/image", id=model)
        except (httpx.HTTPError, ProxyError):
            return None
        ctype = r.headers.get("content-type", "").split(";")[0]
        return (r.content, ctype) if r.status_code == 200 and ctype.startswith("image/") else None

    async def user(self, p: dict) -> dict | None:
        try:
            r = await self.get(p, "v1/auths/")
            if r.status_code == 200:
                u = r.json()
                return {"name": u.get("name"), "email": u.get("email")}
        except (httpx.HTTPError, ValueError, ProxyError):
            pass
        return None

    async def test(self, p: dict) -> dict:
        u = await self.user(p)
        return {"ok": True, "user": u} if u else {"ok": False, "error": "Sitzung ungültig"}

    # ---- chat history -------------------------------------------------------------------------
    def _item(self, p: dict, c: dict, **extra) -> dict:
        return {"id": c.get("id"), "title": c.get("title") or "Chat", "updated_at": c.get("updated_at") or 0,
                "provider": p["id"], "provider_name": p.get("name"), "source": "openwebui", **extra}

    async def chat_list(self, p: dict, limit: int = 100) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        try:
            for page in range(1, 21):
                r = await self.get(p, "v1/chats/", page=page)
                batch = r.json() if r.status_code == 200 else []
                if not batch:
                    break
                for c in batch:
                    if c.get("id") and c["id"] not in seen:
                        seen.add(c["id"])
                        out.append(self._item(p, c, model=(c.get("models") or [None])[0]))
                if len(out) >= limit:
                    break
        except (httpx.HTTPError, ValueError):
            pass
        out = out[:limit]
        try:                       # pinned ones are marked; any the plain list leaves out are added
            r = await self.get(p, "v1/chats/pinned")
            pinned = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            pinned = []
        pinned = [c for c in pinned if isinstance(c, dict) and c.get("id")] if isinstance(pinned, list) else []
        ids = {c["id"] for c in pinned}
        for c in out:
            c["pinned"] = c["id"] in ids
        out += [self._item(p, c, pinned=True) for c in pinned if c["id"] not in seen]
        return out

    async def chat_search(self, p: dict, q: str) -> list[dict]:
        try:
            r = await self.get(p, "v1/chats/search", text=q)
            if r.status_code == 200:
                return [self._item(p, h, snippet=h.get("snippet")) for h in r.json() or []]
        except (httpx.HTTPError, ValueError):
            pass
        return []

    async def chat_get(self, p: dict, cid: str) -> dict:
        r = await self.get(p, f"v1/chats/{cid}")
        if r.status_code != 200:
            raise ProxyError("Chat nicht gefunden.", 404)
        raw = r.json()
        chat = raw.get("chat") or {}
        return self._item(p, raw, model=(chat.get("models") or [None])[0], chat=chat)

    async def chat_pin(self, p: dict, cid: str, pinned: bool) -> None:
        """Open WebUI's own pin (its web UI shows it too). Its endpoint toggles, so the state is checked first."""
        r = await self.get(p, f"v1/chats/{cid}/pinned")
        if r.status_code != 200:
            raise ProxyError("Chat nicht gefunden.", 404)
        if bool((r.json() or {}).get("pinned")) == pinned:
            return
        resp, _ = await self.send(p, "POST", f"v1/chats/{cid}/pin")
        if resp.status_code >= 400:
            raise ProxyError(f"Anpinnen fehlgeschlagen (HTTP {resp.status_code}).", 502)

    async def chat_rename(self, p: dict, cid: str, title: str) -> None:
        """The whole chat is sent back with the new title, which works whether or not the server merges updates."""
        r = await self.get(p, f"v1/chats/{cid}")
        if r.status_code != 200:
            raise ProxyError("Chat nicht gefunden.", 404)
        chat = (r.json() or {}).get("chat") or {}
        chat["title"] = title
        resp, _ = await self.send(p, "POST", f"v1/chats/{cid}", headers={"Content-Type": "application/json"},
                                  content=json.dumps({"chat": chat}).encode())
        if resp.status_code >= 400:
            raise ProxyError(f"Umbenennen fehlgeschlagen (HTTP {resp.status_code}).", 502)

    async def chat_delete(self, p: dict, cid: str) -> None:
        resp, _ = await self.send(p, "DELETE", f"v1/chats/{cid}")
        if resp.status_code >= 400:
            raise ProxyError("Chat nicht gefunden.", 404)

    # ---- EDU-KI extras: usage figures stored in chats -----------------------------------------
    async def _chat_ids(self, p: dict, limit: int) -> list[str]:
        ids: list[str] = []
        for page in range(1, 11):
            r = await self.get(p, "v1/chats/", page=page)
            batch = r.json() if r.status_code == 200 else []
            if not batch:
                break
            ids += [c["id"] for c in batch]
            if len(ids) >= limit:
                break
        return ids[:limit]

    async def backfill(self, p: dict, since: int) -> int:
        """Token/cost figures from chats older than live capture; returns the number of rows added."""
        sem = asyncio.Semaphore(5)

        async def one(cid: str) -> int:
            async with sem:
                r = await self.get(p, f"v1/chats/{cid}")
            if r.status_code != 200:
                return 0
            n = 0
            for m in (r.json().get("chat", {}).get("history", {}).get("messages", {}) or {}).values():
                u, ts = m.get("usage"), m.get("timestamp") or 0
                if m.get("role") == "assistant" and isinstance(u, dict) and ts and ts < since:
                    usage.record(p["id"], m.get("model"), u, src="chat", ts=ts)
                    n += 1
            return n

        return sum(await asyncio.gather(*[one(c) for c in await self._chat_ids(p, 300)]))

    async def budget(self, p: dict) -> dict:
        """Latest 'Budgetverbrauch diesen Monat' status the server attached to one of the user's replies."""
        for cid in await self._chat_ids(p, 15):          # newest chats first
            r = await self.get(p, f"v1/chats/{cid}")
            if r.status_code != 200:
                continue
            hits = []
            for m in (r.json().get("chat", {}).get("history", {}).get("messages", {}) or {}).values():
                for st in m.get("statusHistory") or []:
                    text = st.get("description") or ""
                    if "Budgetverbrauch" in text:
                        hits.append((m.get("timestamp") or 0, text))
            if hits:
                ts, text = max(hits)
                pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
                return {"text": text, "at": ts, "percent": float(pct.group(1).replace(",", ".")) if pct else None}
        return {"text": None, "at": None, "percent": None}
