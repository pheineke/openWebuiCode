"""Local proxy and web app backend.

OpenAI-compatible clients talk to /v1/*; the app itself uses /app/* and /ow/*.
A request's model id is ``providerId/modelName``.  Unprefixed ids go to the
default provider, so old configurations keep working.  Everything that depends
on the kind of provider lives in backends/.
"""
import asyncio
import fcntl
import json
import mimetypes
import os
import re
import secrets
import signal
import struct
import subprocess
import termios
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from . import auth, backends, chats, extract, providers, remote, tools, trace, usage
from .backends import ProxyError, emulate


@asynccontextmanager
async def lifespan(_app):
    usage.meta()   # live usage capture starts the first time the server runs, not the first time the page is opened
    yield


app = FastAPI(lifespan=lifespan)
PORT = 8787
_login_tasks: dict[str, asyncio.Task] = {}


@app.exception_handler(ProxyError)
async def _proxy_error(request: Request, exc: ProxyError):
    trace.log("error", path=request.url.path, status=exc.status, message=exc.message)
    return JSONResponse({"error": {"message": exc.message}}, status_code=exc.status)


def _ok_hosts() -> set[str]:
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """The server holds live sessions: refuse DNS-rebinding and cross-site requests."""
    ok_hosts = _ok_hosts()
    origin = request.headers.get("origin")
    preview = request.url.path.startswith("/app/preview/") and request.method == "GET"
    if origin == "null" and preview:  # sandboxed preview iframe fetching its own assets
        origin = None
    if request.headers.get("host") not in ok_hosts or (origin and origin.split("//", 1)[-1] not in ok_hosts):
        return JSONResponse({"error": {"message": "forbidden"}}, 403)
    resp = await call_next(request)
    if not request.url.path.startswith(("/v1/", "/ow/")):
        resp.headers["Cache-Control"] = "no-cache"  # UI files: always revalidate, so updates show up immediately
    return resp


# ---- provider helpers -------------------------------------------------------------------------
def _provider(pid: str | None = None) -> dict:
    if pid:
        p = providers.get(pid)
        if not p:
            raise ProxyError("Anbieter nicht gefunden.", 404)
        return p
    p = providers.default()
    if not p:
        raise ProxyError("Kein Anbieter angemeldet.", 401)
    return p


def _logged_in(p: dict) -> dict:
    if not providers.logged_in(p):
        raise ProxyError(f"Bei {p.get('name') or p.get('id')} nicht angemeldet.", 401)
    return p


def _ow_provider(pid: str | None) -> dict:
    """The Open WebUI instance for raw pass-through calls."""
    p = _provider(pid) if pid else providers.default_openwebui()
    if not p or p.get("type") != "openwebui":
        raise ProxyError("Kein Open-WebUI-Anbieter.", 400)
    return _logged_in(p)


def _history_providers() -> list[dict]:
    return [p for p in providers.load() if providers.logged_in(p) and backends.of(p).history]


async def _each(items: list, fn) -> list:
    """fn over items concurrently; a failing provider contributes nothing instead of failing the request."""
    results = await asyncio.gather(*(fn(x) for x in items), return_exceptions=True)
    return [r for r in results if not isinstance(r, BaseException)]


# ---- models -----------------------------------------------------------------------------------
def _model_item(p: dict, mid: str, name: str) -> dict:
    return {"id": f"{p['id']}/{mid}", "name": name or mid, "model": mid, "provider": p["id"],
            "provider_name": p.get("name"), "type": p.get("type"), "object": "model", "created": 0,
            "owned_by": p.get("name") or p["id"]}


async def _models_for(p: dict) -> list[dict]:
    if not providers.logged_in(p):
        return []
    pairs = await asyncio.wait_for(backends.of(p).models(p), 45)
    return [_model_item(p, mid, name) for mid, name in pairs]


# ---- OpenAI-compatible API --------------------------------------------------------------------
@app.get("/v1/models")
async def v1_models():
    return {"object": "list", "data": [m for ms in await _each(providers.load(), _models_for) for m in ms]}


@app.post("/v1/chat/completions")
async def v1_chat(request: Request):
    try:
        data = json.loads(await request.body())
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise ProxyError("Ungültiger JSON-Body.", 400)
    trace.start()
    if trace.on():
        trace.log("request", model=data.get("model"), stream=data.get("stream"),
                  tools=[(t.get("function") or {}).get("name") for t in data.get("tools") or [] if isinstance(t, dict)],
                  messages=trace.messages(data.get("messages")))
    pid, model_name = providers.split_model(str(data.get("model") or ""))
    p = _logged_in(_provider(pid))
    data["model"] = model_name
    b = backends.of(p)
    try:
        if not b.native_tools:
            trace.log("route", how=f"{b.type} backend", provider=p["id"])
            return trace.response(await b.chat(p, data))
        in_prompt = emulate.wanted(p, model_name) or (p["id"], model_name.lower()) in _prompt_tools
        if in_prompt and data.get("tools"):
            trace.log("route", how="tools in the prompt (emulate.py)", provider=p["id"])
            return trace.response(await emulate.chat(b, p, data))
        if in_prompt and _tool_turns(data.get("messages")):    # no tools this time, but earlier calls in the chat
            trace.log("route", how="no tools; earlier tool calls as text", provider=p["id"])
            return trace.response(await b.chat(p, {**data, "messages": emulate.messages(data["messages"], [])}))
        trace.log("route", how="native tools", provider=p["id"])
        resp = await b.chat(p, data)
        if resp.status_code < 400:
            if data.get("tools") and data.get("stream") and "text/event-stream" in resp.headers.get("content-type", ""):
                async def retry():                 # the model wrote "Tool: name" + JSON: it never got the tools
                    _prompt_tools.add((p["id"], model_name.lower()))
                    return await emulate.chat(b, p, data)
                resp = StreamingResponse(emulate.watch_text_calls(resp.body_iterator, retry),
                                         media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
            return trace.response(resp)
        if not _tool_turns(data.get("messages")):
            return trace.response(resp)
        body = await _read_body(resp)
        if not TOOL_ROLE_REJECTED.search(body.decode("utf-8", "replace")):
            return Response(body, status_code=resp.status_code, media_type=resp.headers.get("content-type"))
        # The upstream takes no tool turns (e.g. Claude behind a gateway that passes OpenAI messages on as
        # they are): from now on this model gets its tools and their results as text.
        _prompt_tools.add((p["id"], model_name.lower()))
        trace.log("route", how="upstream rejects role 'tool': retrying with tools in the prompt", provider=p["id"])
        if data.get("tools"):
            return trace.response(await emulate.chat(b, p, data))
        return trace.response(await b.chat(p, {**data, "messages": emulate.messages(data["messages"], [])}))
    except ProxyError:
        raise
    except Exception:
        trace.log("crash", error=traceback.format_exc(), cap=8000)
        raise


# Models whose upstream refused tool turns during this run (provider id, lower-case model id).
_prompt_tools: set[tuple[str, str]] = set()
# 'Unexpected role "tool"' (Anthropic via LiteLLM), "invalid role: tool", "role 'tool' is not supported", ...
TOOL_ROLE_REJECTED = re.compile(r"(unexpected|invalid|unsupported|unknown)\W+role\W+tool\b"
                                r"|\brole\W+tool\W+(is\s+)?(not\s+allowed|not\s+supported|invalid|unsupported)", re.I)


def _tool_turns(msgs) -> bool:
    return any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls")) for m in msgs or [])


async def _read_body(resp) -> bytes:
    """The whole body of an (error) response; a streamed one is consumed, which closes its upstream."""
    if not hasattr(resp, "body_iterator"):
        return resp.body
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c if isinstance(c, bytes) else c.encode())
    return b"".join(chunks)


async def _ow_forward(path: str, request: Request):
    p = _ow_provider(request.query_params.get("provider"))
    tap = None
    if request.method == "POST" and path.strip("/") == "chat/completions":
        try:
            tap = json.loads(await request.body()).get("model")
        except (ValueError, AttributeError):
            tap = None
    return await backends.of(p).forward(p, path, request, tap_model=tap)


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_v1(path: str, request: Request):
    return await _ow_forward(path, request)


@app.api_route("/ow/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward_ow(path: str, request: Request):
    return await _ow_forward(path, request)


@app.get("/health")
async def health():
    return {"ok": True}


# ---- app: status, providers, models -----------------------------------------------------------
@app.get("/app/status")
async def status():
    logged = [p for p in providers.load() if providers.logged_in(p)]
    user = None
    for p in sorted(logged, key=lambda p: p.get("type") != "openwebui"):   # the university account first
        try:
            user = await backends.of(p).user(p)
        except Exception:
            user = None
        if user:
            break
    default = providers.default()
    return {"logged_in": bool(logged), "providers": providers.all_public(), "user": user,
            "default": default["id"] if default else None, "debug": trace.on()}


@app.get("/app/providers")
async def provider_list():
    return {"providers": providers.all_public(), "types": backends.types()}


@app.post("/app/providers")
async def provider_add(req: dict):
    try:
        p = providers.add(req)
    except ValueError as e:
        raise ProxyError(str(e), 400) from e
    return {"provider": providers.public(p), "providers": providers.all_public()}


@app.patch("/app/providers/{pid}")
async def provider_update(pid: str, req: dict):
    try:
        p = providers.edit(pid, req)
    except ValueError as e:
        raise ProxyError(str(e), 400) from e
    if not p:
        raise ProxyError("Anbieter nicht gefunden.", 404)
    return {"provider": providers.public(p)}


@app.delete("/app/providers/{pid}")
async def provider_remove(pid: str):
    providers.remove(pid)
    return {"ok": True, "providers": providers.all_public()}


@app.post("/app/providers/{pid}/login")
async def provider_login(pid: str):
    """Log in in a Chromium window (blocks until done).  The app window logs in by itself instead
    for providers whose login is "window", and hands the result to install_session()."""
    p = _provider(pid)
    b = backends.of(p)
    if not b.login:
        raise ProxyError("Dieser Anbieter braucht keine Anmeldung, nur einen API-Schlüssel.", 400)
    task = _login_tasks.get(pid)
    if not task or task.done():
        task = asyncio.create_task(asyncio.to_thread(auth.interactive_login, p, b))
        _login_tasks[pid] = task
    try:
        state = await asyncio.shield(task)
    except auth.LoginRequired as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=408)
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    providers.set_session(pid, state)
    return {"logged_in": True, "provider": providers.public(providers.get(pid) or p)}


@app.post("/app/providers/{pid}/logout")
async def provider_logout(pid: str):
    p = _provider(pid)
    providers.clear_session(pid)
    await asyncio.to_thread(auth.clear_profile, p)
    return {"logged_in": False, "providers": providers.all_public()}


@app.post("/app/providers/{pid}/test")
async def provider_test(pid: str):
    p = _logged_in(_provider(pid))
    try:
        return await backends.of(p).test(p)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/app/models")
async def app_models():
    async def one(p: dict) -> dict:
        try:
            models = await _models_for(p)
        except Exception:
            models = []
        return {"id": p.get("id"), "name": p.get("name"), "type": p.get("type"),
                "logged_in": providers.logged_in(p), "models": models}
    return {"providers": await _each(providers.load(), one)}


@app.post("/app/login")
async def login(provider: str | None = None):
    p = providers.get(provider) if provider else (providers.default_openwebui() or providers.default())
    if not p:
        raise ProxyError("Kein Anbieter konfiguriert.", 400)
    return await provider_login(p["id"])


@app.post("/app/logout")
async def logout():
    for p in providers.load():
        providers.clear_session(p["id"])
        await asyncio.to_thread(auth.clear_profile, p)
    auth.clear_state()
    return {"logged_in": False}


def install_session(pid: str, state: dict) -> None:
    """Adopt a session captured elsewhere (the native window's own login)."""
    if not providers.set_session(pid, state):
        raise ProxyError("Anbieter nicht gefunden.", 404)


# ---- app: chats (stored locally, or in the provider's own history) ----------------------------
@app.get("/app/chats")
async def chat_list():
    out = chats.list_all()
    for items in await _each(_history_providers(), lambda p: backends.of(p).chat_list(p)):
        out += items
    out.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return {"chats": out[:300]}


@app.get("/app/chats/search")
async def chat_search(q: str = ""):
    hits = chats.search(q)
    if q.strip():
        for items in await _each(_history_providers(), lambda p: backends.of(p).chat_search(p, q)):
            hits += items
    hits.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return hits


@app.post("/app/chats")
async def chat_save(req: dict):
    try:
        rec = chats.save(req)
    except ValueError as e:
        raise ProxyError(str(e), 400) from e
    return {"id": rec["id"], "updated_at": rec["updated_at"], "chat": rec}


def _chat_candidates(provider: str | None) -> list[dict]:
    if provider:
        p = providers.get(provider)
        return [p] if p and providers.logged_in(p) and backends.of(p).history else []
    return _history_providers()


@app.get("/app/chats/{cid}")
async def chat_get(cid: str, provider: str | None = None):
    if cid.startswith("local-"):
        c = chats.load(cid)
        if not c:
            raise ProxyError("Chat nicht gefunden.", 404)
        return {"id": cid, "title": c.get("title") or "Chat", "updated_at": c.get("updated_at") or 0,
                "model": c.get("model"), "provider": c.get("provider"), "source": "local", "chat": c}
    for p in _chat_candidates(provider):
        try:
            return await backends.of(p).chat_get(p, cid)
        except ProxyError:
            continue
    raise ProxyError("Chat nicht gefunden.", 404)


@app.delete("/app/chats/{cid}")
async def chat_delete(cid: str, provider: str | None = None):
    if cid.startswith("local-"):
        chats.delete(cid)
        return {"ok": True}
    for p in _chat_candidates(provider):
        try:
            await backends.of(p).chat_delete(p, cid)
            return {"ok": True}
        except ProxyError:
            continue
    raise ProxyError("Chat nicht gefunden.", 404)


# ---- app: usage -------------------------------------------------------------------------------
@app.get("/app/usage")
async def usage_report():
    return usage.report({p["id"]: p.get("name") for p in providers.load()})


@app.post("/app/usage/backfill")
async def backfill():
    """One-off: take token/cost figures from earlier Open WebUI chats (older than live capture)."""
    meta = usage.meta()
    if meta["backfilled"]:
        return {"added": 0}
    p = providers.default_openwebui()
    added = 0
    if p and providers.logged_in(p):
        added = await backends.of(p).backfill(p, meta["since"])
    meta["backfilled"] = True
    usage.save_meta(meta)
    return {"added": added}


@app.get("/app/budget")
async def budget():
    p = providers.default_openwebui()
    if not p or not providers.logged_in(p):
        return {"text": None, "at": None, "percent": None}
    return await backends.of(p).budget(p)


def _conn(conn_id: str | None) -> dict | None:
    """Saved SSH connection for an id (None = this machine)."""
    if not conn_id:
        return None
    c = remote.get(conn_id)
    if not c:
        raise tools.ToolError("unknown SSH connection (it may have been removed)")
    return c


class ToolReq(BaseModel):
    workdir: str
    name: str
    args: dict = {}
    conn: str | None = None


@app.post("/app/tool")
async def run_tool(req: ToolReq):
    t0 = time.monotonic()
    try:
        output, meta = await tools.run(req.workdir, req.name, req.args, _conn(req.conn))
        trace.log("tool", name=req.name, args=req.args, output=output, t=round(time.monotonic() - t0, 1), cap=600)
        return {"output": output, "is_error": False, "meta": meta}
    except tools.ToolError as e:
        trace.log("tool", name=req.name, args=req.args, error=str(e), t=round(time.monotonic() - t0, 1), cap=600)
        return {"output": f"Error: {e}", "is_error": True}


MAX_ATTACHMENT = 100 * 1024 * 1024


@app.post("/app/extract")
async def extract_text(request: Request, name: str = ""):
    """Text of an attached document (PDF, Word, PowerPoint, OpenDocument) for the chat."""
    if int(request.headers.get("content-length") or 0) > MAX_ATTACHMENT:
        return JSONResponse({"error": {"message": "Die Datei ist zu groß (höchstens 100 MB)."}}, 413)
    data = await request.body()
    try:
        return await asyncio.to_thread(extract.text_of, name, data)
    except extract.Unsupported as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.post("/app/log")
async def app_log(req: dict):
    """Debug lines from the app window: its errors and what it made of each answer."""
    trace.log("app " + str(req.get("what") or ""), data=req.get("data"))
    return {"ok": True}


_previews: dict[str, tuple[Path | str, str | None]] = {}  # unguessable id -> (working folder, SSH connection id)


@app.post("/app/preview")
async def preview_session(req: dict):
    conn_id = req.get("conn") or None
    try:
        c = _conn(conn_id)
        if c:
            await remote.fs_list(c, req.get("workdir", ""))        # fails if the folder does not exist
            root = str(req.get("workdir", ""))
        else:
            root = tools._root(req.get("workdir", ""))
    except tools.ToolError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)
    pid = next((k for k, v in _previews.items() if v == (root, conn_id)), None) or secrets.token_urlsafe(16)
    _previews[pid] = (root, conn_id)
    return {"id": pid}


@app.get("/app/preview/{pid}/{path:path}")
async def preview(pid: str, path: str):
    """Serve files of the working folder so a previewed HTML page can load its own css/js/images."""
    entry = _previews.get(pid)
    if not entry:
        return JSONResponse({"error": {"message": "unknown preview"}}, 404)
    root, conn_id = entry
    headers = {"Content-Security-Policy": "sandbox allow-scripts allow-forms", "Cache-Control": "no-store",
               "Access-Control-Allow-Origin": "*"}
    if conn_id:                                                        # file lives on the SSH host
        try:
            data = await remote.read_bytes(_conn(conn_id), root, path)
        except tools.ToolError as e:
            return JSONResponse({"error": {"message": str(e)}}, 403 if "outside" in str(e) else 404)
        ctype = mimetypes.guess_type(path)[0] or ("text/html" if data.lstrip()[:1] == b"<" else "application/octet-stream")
        return Response(data, media_type=ctype, headers=headers)
    try:
        f = tools._path(root, path or ".")
    except tools.ToolError:
        return JSONResponse({"error": {"message": "outside working folder"}}, 403)
    if f.is_dir():
        f = f / "index.html"
    if not f.is_file():
        return JSONResponse({"error": {"message": "not found"}}, 404)
    # CSP sandbox keeps the page in an opaque origin even if it is opened as a top-level document
    return FileResponse(f, headers=headers)  # preview pages may fetch() their own files


def _winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


@app.websocket("/app/term")
async def terminal(ws: WebSocket, workdir: str = "", cols: int = 80, rows: int = 24, conn: str = ""):
    """Interactive shell on a PTY. This is arbitrary code execution as the user, so the
    WebSocket handshake must come from our own page: browsers send Origin on every WebSocket
    and the HTTP middleware does not cover them."""
    origin = ws.headers.get("origin", "")
    if ws.headers.get("host") not in _ok_hosts() or origin.split("//", 1)[-1] not in _ok_hosts():
        await ws.close(code=1008)
        return
    try:
        c = _conn(conn)
    except tools.ToolError:
        await ws.close(code=1008)
        return
    await ws.accept()
    if c:                                  # shell on the SSH host (ssh -tt inside the PTY)
        shell, cwd = remote.terminal_argv(c, workdir), Path.home()
    else:
        shell = [os.environ.get("SHELL") or "/bin/bash"]
        cwd = Path(workdir).expanduser() if workdir else Path.home()
        if not cwd.is_dir():
            cwd = Path.home()
    master, slave = os.openpty()
    _winsize(master, rows, cols)
    proc = subprocess.Popen(
        shell, stdin=slave, stdout=slave, stderr=slave, cwd=cwd,
        env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}, close_fds=True,
        start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))  # make the PTY its controlling terminal
    os.close(slave)
    loop = asyncio.get_running_loop()
    out: asyncio.Queue[bytes] = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        out.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, readable)

    async def to_client():
        while data := await out.get():
            await ws.send_bytes(data)

    async def from_client():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                os.write(master, msg["bytes"])
            elif msg.get("text"):
                size = json.loads(msg["text"]).get("resize")
                if size:
                    _winsize(master, size[1], size[0])

    tasks = [asyncio.create_task(to_client()), asyncio.create_task(from_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        for sig in (signal.SIGHUP, signal.SIGKILL):   # polite first, then force
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), 2)
                break
            except asyncio.TimeoutError:
                continue
        os.close(master)
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/app/file")
async def file(workdir: str, path: str, conn: str = ""):
    try:
        c = _conn(conn)
        if c:
            return {"content": await remote.read_raw(c, workdir, path)}
        return {"content": await asyncio.to_thread(tools.read_raw, workdir, path)}
    except (tools.ToolError, OSError) as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.get("/app/ls")
async def ls(workdir: str, path: str = ".", conn: str = ""):
    try:
        c = _conn(conn)
        if c:
            return {"entries": await remote.ls_entries(c, workdir, path)}
        return {"entries": await asyncio.to_thread(tools.ls, workdir, path)}
    except (tools.ToolError, OSError) as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.get("/app/fs")
async def fs(path: str = "", conn: str = ""):
    """Directory listing for the folder picker."""
    try:
        c = _conn(conn)
        if c:
            return await remote.fs_list(c, path)
    except tools.ToolError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)
    p = Path(path or "~").expanduser().resolve()
    try:
        dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    except OSError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs}


@app.get("/app/ssh")
async def ssh_list():
    return {"connections": remote.load()}


@app.post("/app/ssh")
async def ssh_add(req: dict):
    try:
        return remote.add(req)
    except tools.ToolError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.delete("/app/ssh/{conn_id}")
async def ssh_remove(conn_id: str):
    remote.remove(conn_id)
    return {"ok": True}


@app.post("/app/ssh/test")
async def ssh_test(req: dict):
    """Try a connection (not saved): who/where/what, and which tools the host lacks."""
    try:
        return await remote.test(req)
    except tools.ToolError as e:
        return {"ok": False, "error": str(e)}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
