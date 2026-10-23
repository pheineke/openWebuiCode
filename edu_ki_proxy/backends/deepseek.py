"""chat.deepseek.com (unofficial).

The web app has no public API.  We reuse the session captured at login and speak
its internal endpoints:

  POST /api/v0/chat_session/create
  POST /api/v0/chat/create_pow_challenge   (DeepSeekHashV1 proof of work)
  POST /api/v0/chat/completion             (SSE)

Every OpenAI request is stateless: the full transcript is rendered into one
prompt and a fresh DeepSeek session is created (and removed afterwards).  Tool
calls are emulated (see emulate.py): the model is asked for a fenced ```tool JSON
block, which we translate into OpenAI tool_calls for the app.
"""
import asyncio
import base64
import json
import os
import struct
import threading
import time
import uuid

import httpx
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .. import auth, providers, trace, usage
from .base import Backend, ProxyError, token
from .emulate import ToolStream, messages as _turns, text as _text

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
# The module is versioned by DeepSeek's frontend build; override when they ship a new one.
WASM_URL = os.environ.get("EDU_KI_DEEPSEEK_WASM",
                          "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm")
MAX_PROMPT = 380_000
EXPIRED = "DeepSeek-Sitzung abgelaufen, bitte erneut anmelden."
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

MODELS = [
    {"id": "deepseek-chat", "name": "DeepSeek Chat", "model_type": "default", "thinking": False, "search": False},
    {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner (R1)", "model_type": "expert", "thinking": True, "search": False},
    {"id": "deepseek-search", "name": "DeepSeek Chat + Web Search", "model_type": "default", "thinking": False, "search": True},
    {"id": "deepseek-reasoner-search", "name": "DeepSeek Reasoner + Web Search", "model_type": "expert", "thinking": True, "search": True},
]
_BY_ID = {m["id"]: m for m in MODELS}


class DeepSeekError(Exception):
    pass


class DeepSeekLoginRequired(DeepSeekError):
    pass


# ---- proof of work -----------------------------------------------------------------------------
class _Pow:
    """DeepSeekHashV1 inside DeepSeek's own wasm module (via wasmtime).

    wasmtime.Store is not reentrant, so all solves are serialised.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._inst = None

    def _ensure(self):
        if self._inst:
            return
        try:
            import wasmtime
        except ImportError as e:
            raise DeepSeekError("Für DeepSeek fehlt das Paket 'wasmtime' (pip install wasmtime).") from e
        path = auth.STATE_DIR / WASM_URL.rsplit("/", 1)[-1]
        if not path.exists():
            auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                r = httpx.get(WASM_URL, timeout=30, follow_redirects=True)
                r.raise_for_status()
                path.write_bytes(r.content)
            except (httpx.HTTPError, OSError) as e:
                raise DeepSeekError(f"PoW-Modul konnte nicht geladen werden ({e}). Neue Adresse ggf. "
                                    "über EDU_KI_DEEPSEEK_WASM setzen.") from e
        store = wasmtime.Store()
        module = wasmtime.Module.from_file(store.engine, str(path))
        inst = wasmtime.Instance(store, module, [])
        exp = inst.exports(store)
        try:
            self._inst = (store, exp["memory"], exp["wasm_solve"],
                          exp["__wbindgen_export_0"], exp["__wbindgen_add_to_stack_pointer"])
        except KeyError as e:
            raise DeepSeekError(f"PoW-Modul hat eine unbekannte Schnittstelle (fehlt: {e}).") from e

    def _solve(self, challenge: str, prefix: str, difficulty: float) -> int | None:
        store, memory, solve, malloc, add_to_stack = self._inst

        def write(text: str) -> tuple[int, int]:
            data = text.encode()
            ptr = malloc(store, len(data), 1)
            mem = memory.data_ptr(store)       # after malloc: growing the memory may move it
            for i, b in enumerate(data):
                mem[ptr + i] = b
            return ptr, len(data)

        retptr = add_to_stack(store, -16)
        try:
            c_ptr, c_len = write(challenge)
            p_ptr, p_len = write(prefix)
            solve(store, retptr, c_ptr, c_len, p_ptr, p_len, float(difficulty))
            mem = memory.data_ptr(store)
            status = struct.unpack("<i", bytes(mem[retptr:retptr + 4]))[0]
            value = struct.unpack("<d", bytes(mem[retptr + 8:retptr + 16]))[0]
        finally:
            add_to_stack(store, 16)
        return int(value) if status else None

    def header(self, challenge: dict) -> str:
        with self._lock:
            self._ensure()
            prefix = f"{challenge['salt']}_{challenge['expire_at']}_"
            answer = self._solve(challenge["challenge"], prefix, challenge["difficulty"])
        if answer is None:
            raise DeepSeekError("DeepSeek-PoW konnte nicht gelöst werden.")
        payload = {"algorithm": challenge["algorithm"], "challenge": challenge["challenge"],
                   "salt": challenge["salt"], "answer": answer, "signature": challenge["signature"],
                   "target_path": challenge["target_path"]}
        return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


_POW = _Pow()


# ---- prompt building --------------------------------------------------------------------------
def build_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    """The transcript as one prompt, with earlier tool calls written as the instruction asks."""
    turns = _turns(messages, tools)
    parts, convo = [], []
    if turns and turns[0]["role"] == "system":
        parts.append("System:\n" + _text(turns.pop(0)["content"]))
    for m in turns:
        text = _text(m["content"])
        if text:
            convo.append(f"{m['role'].capitalize()}:\n{text}")
    if convo:
        parts.append("Conversation:\n\n" + "\n\n".join(convo))
    text = "\n\n".join(parts)
    if len(text) <= MAX_PROMPT:
        return text
    head = "\n\n".join(parts[:-1])
    if head and len(head) < MAX_PROMPT // 3:
        return head + "\n\n[... gekürzt ...]\n\n" + parts[-1][-(MAX_PROMPT - len(head) - 30):]
    return text[-MAX_PROMPT:]


# ---- DeepSeek HTTP client ----------------------------------------------------------------------
def _biz(data: dict) -> dict:
    if not isinstance(data, dict):
        raise DeepSeekError("Ungültige Antwort von DeepSeek.")
    if data.get("code") not in (None, 0, "0"):
        if str(data.get("code")) in ("40002", "40003"):
            raise DeepSeekLoginRequired(EXPIRED)
        raise DeepSeekError(str(data.get("msg") or data))
    d = data.get("data") or {}
    if d.get("biz_code") not in (None, 0, "0"):
        if str(d.get("biz_code")) in ("40002", "40003"):
            raise DeepSeekLoginRequired(EXPIRED)
        raise DeepSeekError(str(d.get("biz_msg") or data.get("msg") or d))
    return d.get("biz_data") or {}


def _check(r: httpx.Response) -> None:
    """401: session gone.  403 from the CDN (an HTML page) is bot protection, not a logout."""
    if r.status_code == 401:
        raise DeepSeekLoginRequired(EXPIRED)
    if r.status_code == 403:
        if "text/html" in r.headers.get("content-type", ""):
            raise DeepSeekError("DeepSeek blockiert die Anfrage (HTTP 403, Bot-Schutz). "
                                "Später erneut versuchen oder neu anmelden.")
        raise DeepSeekLoginRequired(EXPIRED)


def _headers(token_: str, ua: str | None) -> dict:
    return {
        "authorization": f"Bearer {token_}",
        "accept": "*/*",
        "content-type": "application/json",
        "user-agent": ua or UA,
        "origin": BASE,
        "referer": f"{BASE}/a/chat/",
        "x-client-platform": "web",
        "x-client-version": "2.4.0",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-timezone-offset": str(-int(time.timezone)),
    }


def _cookies(state: dict) -> dict:
    return {c.get("name"): c.get("value") for c in state.get("cookies") or [] if c.get("name")}


class _Client:
    def __init__(self, provider: dict):
        s = provider.get("session") or {}
        if not s.get("token"):
            raise DeepSeekLoginRequired("Nicht bei DeepSeek angemeldet.")
        self.http = httpx.AsyncClient(base_url=BASE, cookies=_cookies(s), timeout=httpx.Timeout(600, connect=20),
                                      headers=_headers(s["token"], s.get("user_agent")))

    async def _post(self, path: str, body: dict) -> dict:
        r = await self.http.post(path, json=body)
        _check(r)
        r.raise_for_status()
        return _biz(r.json())

    async def create_session(self) -> str:
        biz = await self._post("/api/v0/chat_session/create", {})
        sid = (biz.get("chat_session") or {}).get("id") or biz.get("id")
        if not sid:
            raise DeepSeekError("DeepSeek hat keine Chat-Sitzung geliefert.")
        return sid

    async def pow_header(self) -> str:
        challenge = (await self._post("/api/v0/chat/create_pow_challenge",
                                      {"target_path": COMPLETION_PATH})).get("challenge")
        if not challenge:
            raise DeepSeekError("DeepSeek hat keine PoW-Aufgabe geliefert.")
        return await asyncio.to_thread(_POW.header, challenge)

    async def delete_session(self, sid: str) -> None:
        try:
            await self.http.post("/api/v0/chat_session/delete", json={"chat_session_id": sid})
        except httpx.HTTPError:
            pass

    async def close(self) -> None:
        await self.http.aclose()


def _user_of(biz: dict) -> dict:
    u = biz.get("user") if isinstance(biz.get("user"), dict) else biz
    profile = u.get("id_profile") if isinstance(u.get("id_profile"), dict) else {}
    return {"name": profile.get("name") or u.get("name") or u.get("email") or u.get("mobile_number"),
            "email": u.get("email") or u.get("mobile_number")}


# ---- SSE parsing ------------------------------------------------------------------------------
async def _events(response: httpx.Response):
    event, data = None, []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def _frag(frag) -> tuple[str, str] | None:
    if not isinstance(frag, dict):
        return None
    kind, text = frag.get("type"), frag.get("content")
    if not isinstance(text, str):
        text = ""
    if kind == "THINK":
        return "reasoning", text
    if kind in ("RESPONSE", "ANSWER"):
        return "content", text
    if kind in ("SEARCH", "SEARCH_REF"):
        return "reasoning", (f"\n[search] {text}\n" if text else "\n[search]\n")
    return None


def _error_text(data: dict) -> str | None:
    if data.get("type") == "error" or data.get("o") == "ERROR" or isinstance(data.get("error"), dict):
        pass
    elif isinstance(data.get("code"), int) and data["code"] != 0 and (data.get("message") or data.get("msg")):
        pass
    else:
        return None
    for v in (data.get("message"), data.get("msg"), data.get("content"),
              (data.get("v") or {}).get("message") if isinstance(data.get("v"), dict) else None,
              (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None):
        if isinstance(v, str) and v.strip():
            return v
    return json.dumps(data, ensure_ascii=False)[:300]


async def _updates(response: httpx.Response):
    current = "content"
    async for event, raw in _events(response):
        try:
            data = json.loads(raw)
        except ValueError:
            trace.log("deepseek <", event=event, raw=raw)
            continue
        if not isinstance(data, dict):
            trace.log("deepseek <", event=event, raw=raw)
            continue
        if not (isinstance(data.get("v"), str) and set(data) == {"v"}):
            trace.log("deepseek <", event=event, data=data)     # all but the bare text appends
        if event == "ready" or event == "title":
            continue
        if event == "close":
            return
        err = _error_text(data)
        if err:
            raise DeepSeekError(err)
        resp = ((data.get("v") or {}).get("response") if isinstance(data.get("v"), dict) else None)
        if isinstance(resp, dict):
            for frag in resp.get("fragments") or []:
                u = _frag(frag)
                if u:
                    current = u[0]
                    if u[1]:
                        yield u
            usage_ = resp.get("accumulated_token_usage")
            if isinstance(usage_, int):
                yield "usage", usage_
            continue
        if data.get("p") == "response/fragments" and data.get("o") == "APPEND":
            for frag in data.get("v") or []:
                u = _frag(frag)
                if u:
                    current = u[0]
                    if u[1]:
                        yield u
            continue
        p = data.get("p")
        if p == "response/fragments/-1/content" and data.get("o") in (None, "APPEND"):   # the first text of a new fragment comes without "o"
            v = data.get("v")
            if isinstance(v, str) and v:
                yield current, v
            continue
        if p is None and (data.get("o") == "APPEND" or data.get("o") is None) and isinstance(data.get("v"), str):
            if data["v"]:
                yield current, data["v"]
            continue
        if p == "response" and data.get("o") == "BATCH":
            for patch in data.get("v") or []:
                if isinstance(patch, dict) and patch.get("p") == "accumulated_token_usage" and isinstance(patch.get("v"), int):
                    yield "usage", patch["v"]


# ---- OpenAI translation -----------------------------------------------------------------------
def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _chunk(cid: str, model: str, delta: dict, finish=None) -> bytes:
    return _sse({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def _usage(tokens: int | None) -> dict:
    return {"prompt_tokens": 0, "completion_tokens": tokens or 0, "total_tokens": tokens or 0}


class _Upstream:
    """One open completion: the DeepSeek chat session and its SSE response."""

    def __init__(self, client: _Client, sid: str, response: httpx.Response):
        self.client, self.sid, self.response, self.closed = client, sid, response, False

    def updates(self):
        return _updates(self.response)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.response.aclose()
        finally:
            await self.client.delete_session(self.sid)
            await self.client.close()

    async def sse(self, cid: str, model_name: str, tools: bool):
        ts = ToolStream(tools)
        tokens = None

        def emit(events):
            for kind, value in events:
                if kind == "content":
                    yield _chunk(cid, model_name, {"content": value})
                elif kind == "call_start":
                    yield _chunk(cid, model_name, {"tool_calls": [ts.start(value)]})
                else:
                    yield _chunk(cid, model_name, {"tool_calls": ts.delta(value)})

        try:
            async for kind, value in self.updates():
                if kind == "reasoning":
                    yield _chunk(cid, model_name, {"reasoning_content": value})
                elif kind == "content":
                    for b in emit(ts.feed(value)):
                        yield b
                elif kind == "usage" and isinstance(value, int):
                    tokens = value
            for b in emit(ts.finish()):
                yield b
            yield _chunk(cid, model_name, {}, "tool_calls" if ts.calls else "stop")
            yield _sse({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": model_name, "choices": [], "usage": _usage(tokens)})
        except asyncio.CancelledError:
            raise
        except Exception as e:          # headers are out already: report in-band, as OpenAI does
            kind = "login_required" if isinstance(e, DeepSeekLoginRequired) else "upstream_error"
            yield _sse({"error": {"message": str(e) or type(e).__name__, "type": kind}})
        finally:
            await self.close()
        yield b"data: [DONE]\n\n"


async def _open(provider: dict, model: dict, prompt: str) -> _Upstream:
    """Start a completion.  Everything that can fail before the first token fails here, so the
    caller can still answer with a proper HTTP status."""
    c = _Client(provider)
    sid = r = None
    try:
        sid = await c.create_session()
        pow_header = await c.pow_header()
        body = {"action": None, "chat_session_id": sid, "parent_message_id": None,
                "model_type": model["model_type"], "prompt": prompt, "ref_file_ids": [],
                "thinking_enabled": model["thinking"], "search_enabled": model["search"],
                "preempt": False}
        trace.log("deepseek >", cap=30000, session=sid, model_type=model["model_type"], thinking=model["thinking"],
                  search=model["search"], prompt_chars=len(prompt), prompt=prompt)
        req = c.http.build_request("POST", COMPLETION_PATH, json=body,
                                   headers={"x-ds-pow-response": pow_header, "accept": "text/event-stream"})
        r = await c.http.send(req, stream=True)
        trace.log("deepseek <", status=r.status_code, content_type=r.headers.get("content-type"))
        _check(r)
        if r.status_code >= 400:
            text = (await r.aread()).decode("utf-8", "replace")[:300]
            raise DeepSeekError(f"DeepSeek antwortet mit HTTP {r.status_code}: {text}")
        if "text/event-stream" not in r.headers.get("content-type", ""):
            text = (await r.aread()).decode("utf-8", "replace")[:500]
            try:
                _biz(json.loads(text))     # JSON error body, e.g. an expired token
            except ValueError:
                pass
            raise DeepSeekError(f"DeepSeek lieferte kein SSE: {text}")
        return _Upstream(c, sid, r)
    except BaseException:
        if r is not None:
            await r.aclose()
        if sid:
            await c.delete_session(sid)
        await c.close()
        raise


async def complete(provider: dict, body: dict, model_name: str):
    """An OpenAI JSON response (dict), or an open _Upstream for streaming."""
    model = _BY_ID.get(model_name) or MODELS[0]
    tools = bool(body.get("tools"))
    up = await _open(provider, model, build_prompt(body.get("messages") or [], body.get("tools")))
    if body.get("stream"):
        return up

    content, reasoning, calls, tokens = [], [], [], None
    ts = ToolStream(tools)

    def take(events):
        for kind, value in events:
            if kind == "content":
                content.append(value)
            elif kind == "tool_calls":       # call_start only matters when streaming
                calls.extend(value)

    try:
        async for kind, value in up.updates():
            if kind == "reasoning":
                reasoning.append(value)
            elif kind == "content":
                take(ts.feed(value))
            elif kind == "usage" and isinstance(value, int):
                tokens = value
        take(ts.finish())
    finally:
        await up.close()
    msg = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion", "created": int(time.time()),
            "model": model_name, "choices": [{"index": 0, "message": msg,
                                              "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": _usage(tokens)}


# ---- backend ----------------------------------------------------------------------------------
class DeepSeek(Backend):
    type = "deepseek"
    label = "DeepSeek (Web)"
    hint = "Anmeldung bei chat.deepseek.com per E-Mail/Telefon, Google oder Apple – in einem Chromium-Fenster."
    login = "browser"      # Google/Apple sign-in needs popups and refuses embedded web views
    fields = ("name",)
    default_base = BASE
    base_required = False
    native_tools = False   # tools are always emulated here

    def token_js(self) -> str:
        return "JSON.parse(localStorage.getItem('userToken') || '{}').value || ''"

    def check_session(self, p: dict, state: dict) -> bool:
        try:
            r = httpx.get(BASE + "/api/v0/users/current", cookies=_cookies(state), timeout=20,
                          headers=_headers(state["token"], state.get("user_agent")))
            _check(r)
            state["user"] = _user_of(_biz(r.json()))
        except DeepSeekLoginRequired:
            return False
        except (DeepSeekError, httpx.HTTPError, ValueError):
            pass        # can't tell (network, bot protection): don't block the login
        return True

    async def models(self, p: dict) -> list[tuple[str, str]]:
        return [(m["id"], m["name"]) for m in MODELS]

    async def chat(self, p: dict, data: dict):
        name = data.get("model") or MODELS[0]["id"]
        try:
            try:
                result = await complete(p, data, name)
            except DeepSeekLoginRequired:
                trace.log("deepseek", relogin="session expired, refreshing from the browser profile")
                p = await self.refresh(p, token(p))       # 401 if the browser profile can't log in either
                result = await complete(p, data, name)
        except DeepSeekLoginRequired as e:
            providers.mark_expired(p["id"])
            raise ProxyError(str(e), 401) from e
        except DeepSeekError as e:
            raise ProxyError(str(e), 502) from e
        if isinstance(result, dict):
            usage.record(p["id"], name, result["usage"])
            return JSONResponse(result)
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        tap = usage.Tap(p["id"], name, streaming=True)
        return StreamingResponse(usage.tapped(result.sse(cid, name, bool(data.get("tools"))), tap),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                 background=BackgroundTask(result.close))   # also runs if the client left early

    async def test(self, p: dict) -> dict:
        state = dict(p.get("session") or {})
        if not state.get("token"):
            return {"ok": False, "error": "nicht angemeldet"}
        ok = await asyncio.to_thread(self.check_session, p, state)
        return {"ok": True, "user": state.get("user")} if ok else {"ok": False, "error": EXPIRED}
