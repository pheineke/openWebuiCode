"""OpenAI-compatible local proxy: /v1/* -> Open WebUI /api/* with the captured session."""
import asyncio
import datetime
import fcntl
import json
import os
import re
import secrets
import signal
import struct
import subprocess
import termios
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from . import auth, tools

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
       "authorization", "cookie", "accept-encoding"}

@asynccontextmanager
async def lifespan(_app):
    _meta()   # live usage capture starts the first time the server runs, not the first time the page is opened
    yield


app = FastAPI(lifespan=lifespan)
PORT = 8787
client = httpx.AsyncClient(base_url=auth.BASE, timeout=httpx.Timeout(600, connect=15), follow_redirects=False)
_lock = asyncio.Lock()
_state = auth.load_state()


def _ok_hosts() -> set[str]:
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """The server holds a live session: refuse DNS-rebinding and cross-site requests."""
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


# ---- usage ledger -----------------------------------------------------------------------------
# Every chat completion that passes through the proxy leaves one line (tokens, cost). The upstream
# has no quota API for users, so this is the measurable consumption; it also covers opencode.
LEDGER = auth.STATE_DIR / "usage.jsonl"
META = auth.STATE_DIR / "usage-meta.json"


def _meta() -> dict:
    try:
        return json.loads(META.read_text())
    except (OSError, ValueError):
        meta = {"since": int(time.time()), "backfilled": False}   # live capture starts now
        _save_meta(meta)
        return meta


def _save_meta(meta: dict) -> None:
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta))


def _record(model: str | None, usage: dict, src: str = "proxy", ts: int | None = None) -> None:
    try:
        row = {"ts": int(ts or time.time()), "model": model or "?", "src": src,
               "in": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
               "out": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
               "cost": float(usage.get("cost") or 0)}
        auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except (OSError, ValueError, TypeError):
        pass   # accounting must never break a chat


class UsageTap:
    """Watches a response pass by and records the final `usage` block (SSE chunk or JSON body)."""

    def __init__(self, model: str | None, streaming: bool):
        self.model, self.streaming, self.buf, self.usage = model, streaming, b"", None

    def feed(self, chunk: bytes) -> None:
        if self.streaming:
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                if line.startswith(b"data:") and b'"usage"' in line:
                    try:
                        u = json.loads(line[5:]).get("usage")
                    except ValueError:
                        continue
                    if u:
                        self.usage = u
        elif len(self.buf) < 5_000_000:
            self.buf += chunk

    def finish(self) -> None:
        if not self.streaming:
            try:
                self.usage = json.loads(self.buf).get("usage")
            except (ValueError, AttributeError):
                pass
        if self.usage:
            _record(self.model, self.usage)


def _expired(resp: httpx.Response) -> bool:
    # front door answers 3xx to the login page when its session is gone; Open WebUI answers 401
    return resp.status_code in (401, 403) or (300 <= resp.status_code < 400)


async def _refresh(stale_token: str | None) -> None:
    global _state
    async with _lock:
        if _state and _state["token"] != stale_token:
            return  # another request already refreshed
        _state = await asyncio.to_thread(auth.silent_refresh)


def _build(request: Request, path: str, body: bytes) -> httpx.Request:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["Authorization"] = f"Bearer {_state['token']}"
    headers["Cookie"] = auth.cookie_header(_state)
    headers["Accept-Encoding"] = "identity"  # we forward raw bytes, so never negotiate compression
    return client.build_request(request.method, f"/api/{path}", params=request.query_params,
                                headers=headers, content=body)


# /v1/* is for OpenAI-compatible clients (opencode); /ow/* reaches any Open WebUI /api/* route (chats etc.)
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.api_route("/ow/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward(path: str, request: Request):
    if not _state:
        return JSONResponse({"error": {"message": "not logged in: run python -m edu_ki_proxy login"}}, 401)
    body = await request.body()
    resp = await client.send(_build(request, path, body), stream=True)
    if _expired(resp):
        await resp.aclose()
        try:
            await _refresh(_state["token"])
        except auth.LoginRequired as e:
            return JSONResponse({"error": {"message": str(e)}}, 401)
        resp = await client.send(_build(request, path, body), stream=True)

    tap = None
    if request.method == "POST" and path.strip("/") == "chat/completions":
        try:
            model = json.loads(body).get("model")
        except ValueError:
            model = None
        tap = UsageTap(model, "text/event-stream" in resp.headers.get("content-type", ""))

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                if tap:
                    tap.feed(chunk)
                yield chunk
        finally:
            await resp.aclose()
            if tap:
                tap.finish()

    out = {k: v for k, v in resp.headers.items()
           if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}}
    return StreamingResponse(body_iter(), status_code=resp.status_code, headers=out)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/app/status")
async def status():
    if not _state:
        return {"logged_in": False}
    try:
        r = await client.get("/api/v1/auths/", headers={"Authorization": f"Bearer {_state['token']}",
                                                        "Cookie": auth.cookie_header(_state)})
        if r.status_code == 200:
            u = r.json()
            return {"logged_in": True, "user": {"name": u.get("name"), "email": u.get("email")}}
    except httpx.HTTPError:
        pass
    return {"logged_in": False}


_login_task: asyncio.Task | None = None


@app.post("/app/login")
async def login():
    """Open the Shibboleth login in a browser window and wait for it to finish."""
    global _state, _login_task
    if _login_task is None or _login_task.done():
        _login_task = asyncio.create_task(asyncio.to_thread(auth.interactive_login))
    try:
        _state = await asyncio.shield(_login_task)
    except auth.LoginRequired as e:
        return JSONResponse({"error": {"message": str(e)}}, 408)
    return {"logged_in": True}


@app.post("/app/logout")
async def logout():
    global _state
    _state = None
    auth.clear_state()
    return {"logged_in": False}


class ToolReq(BaseModel):
    workdir: str
    name: str
    args: dict = {}


@app.post("/app/tool")
async def run_tool(req: ToolReq):
    try:
        output, meta = await tools.run(req.workdir, req.name, req.args)
        return {"output": output, "is_error": False, "meta": meta}
    except tools.ToolError as e:
        return {"output": f"Error: {e}", "is_error": True}


def _agg(rows: list[dict]) -> dict:
    models: dict[str, dict] = {}
    tot = {"requests": 0, "in": 0, "out": 0, "cost": 0.0}
    for r in rows:
        m = models.setdefault(r["model"], {"model": r["model"], "requests": 0, "in": 0, "out": 0, "cost": 0.0})
        for d in (m, tot):
            d["requests"] += 1; d["in"] += r["in"]; d["out"] += r["out"]; d["cost"] += r["cost"]
    return {**tot, "models": sorted(models.values(), key=lambda m: -(m["in"] + m["out"]))}


@app.get("/app/usage")
async def usage():
    rows = []
    try:
        rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    meta = _meta()
    now = datetime.datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    days: dict[str, int] = {}
    for r in rows:
        d = datetime.date.fromtimestamp(r["ts"]).isoformat()
        days[d] = days.get(d, 0) + r["in"] + r["out"]
    last30 = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    return {"since": meta["since"], "backfilled": meta["backfilled"],
            "month": {**_agg([r for r in rows if r["ts"] >= month_start]), "label": now.strftime("%Y-%m")},
            "total": _agg(rows), "days": [{"date": d, "tokens": days.get(d, 0)} for d in last30]}


async def _upstream_get(path: str):
    """GET against Open WebUI with the stored session; re-authenticates once on expiry."""
    for attempt in (0, 1):
        r = await client.get(f"/api/{path}", headers={"Authorization": f"Bearer {_state['token']}", "Cookie": auth.cookie_header(_state),
                                                      "Accept-Encoding": "identity"})
        if attempt == 0 and _expired(r):
            await _refresh(_state["token"])
            continue
        return r


async def _all_chat_ids(limit: int) -> list[str]:
    ids: list[str] = []
    for page in range(1, 11):
        r = await _upstream_get(f"v1/chats/?page={page}")
        batch = r.json() if r.status_code == 200 else []
        if not batch:
            break
        ids += [c["id"] for c in batch]
        if len(ids) >= limit:
            break
    return ids[:limit]


@app.post("/app/usage/backfill")
async def backfill():
    """One-off: take token/cost figures from earlier Open WebUI chats (older than live capture)."""
    if not _state:
        return JSONResponse({"error": {"message": "not logged in"}}, 401)
    meta = _meta()
    if meta["backfilled"]:
        return {"added": 0}
    sem = asyncio.Semaphore(5)
    added = 0

    async def one(cid: str) -> int:
        async with sem:
            r = await _upstream_get(f"v1/chats/{cid}")
        if r.status_code != 200:
            return 0
        n = 0
        for m in (r.json().get("chat", {}).get("history", {}).get("messages", {}) or {}).values():
            u, ts = m.get("usage"), m.get("timestamp") or 0
            if m.get("role") == "assistant" and isinstance(u, dict) and ts and ts < meta["since"]:
                _record(m.get("model"), u, src="chat", ts=ts)
                n += 1
        return n

    try:
        added = sum(await asyncio.gather(*[one(c) for c in await _all_chat_ids(300)]))
    except auth.LoginRequired as e:
        return JSONResponse({"error": {"message": str(e)}}, 401)
    meta["backfilled"] = True
    _save_meta(meta)
    return {"added": added}


@app.get("/app/budget")
async def budget():
    """Latest 'Budgetverbrauch diesen Monat' status the server attached to one of the user's replies."""
    if not _state:
        return JSONResponse({"error": {"message": "not logged in"}}, 401)
    try:
        for cid in await _all_chat_ids(15):          # newest chats first
            r = await _upstream_get(f"v1/chats/{cid}")
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
    except auth.LoginRequired as e:
        return JSONResponse({"error": {"message": str(e)}}, 401)
    return {"text": None, "at": None, "percent": None}


_previews: dict[str, Path] = {}  # unguessable id -> working folder


@app.post("/app/preview")
async def preview_session(req: dict):
    try:
        root = tools._root(req.get("workdir", ""))
    except tools.ToolError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)
    pid = next((k for k, v in _previews.items() if v == root), None) or secrets.token_urlsafe(16)
    _previews[pid] = root
    return {"id": pid}


@app.get("/app/preview/{pid}/{path:path}")
async def preview(pid: str, path: str):
    """Serve files of the working folder so a previewed HTML page can load its own css/js/images."""
    root = _previews.get(pid)
    if not root:
        return JSONResponse({"error": {"message": "unknown preview"}}, 404)
    try:
        f = tools._path(root, path or ".")
    except tools.ToolError:
        return JSONResponse({"error": {"message": "outside working folder"}}, 403)
    if f.is_dir():
        f = f / "index.html"
    if not f.is_file():
        return JSONResponse({"error": {"message": "not found"}}, 404)
    # CSP sandbox keeps the page in an opaque origin even if it is opened as a top-level document
    return FileResponse(f, headers={"Content-Security-Policy": "sandbox allow-scripts allow-forms", "Cache-Control": "no-store",
                                   "Access-Control-Allow-Origin": "*"})  # preview pages may fetch() their own files


def _winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


@app.websocket("/app/term")
async def terminal(ws: WebSocket, workdir: str = "", cols: int = 80, rows: int = 24):
    """Interactive shell on a PTY. This is arbitrary code execution as the user, so the
    WebSocket handshake must come from our own page: browsers send Origin on every WebSocket
    and the HTTP middleware does not cover them."""
    origin = ws.headers.get("origin", "")
    if ws.headers.get("host") not in _ok_hosts() or origin.split("//", 1)[-1] not in _ok_hosts():
        await ws.close(code=1008)
        return
    await ws.accept()
    cwd = Path(workdir).expanduser() if workdir else Path.home()
    if not cwd.is_dir():
        cwd = Path.home()
    master, slave = os.openpty()
    _winsize(master, rows, cols)
    proc = subprocess.Popen(
        [os.environ.get("SHELL") or "/bin/bash"], stdin=slave, stdout=slave, stderr=slave, cwd=cwd,
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
async def file(workdir: str, path: str):
    try:
        return {"content": await asyncio.to_thread(tools.read_raw, workdir, path)}
    except (tools.ToolError, OSError) as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.get("/app/ls")
async def ls(workdir: str, path: str = "."):
    try:
        return {"entries": await asyncio.to_thread(tools.ls, workdir, path)}
    except (tools.ToolError, OSError) as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)


@app.get("/app/fs")
async def fs(path: str = ""):
    """Directory listing for the folder picker."""
    p = Path(path or "~").expanduser().resolve()
    try:
        dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    except OSError as e:
        return JSONResponse({"error": {"message": str(e)}}, 400)
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
