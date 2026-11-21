"""MCP servers (Model Context Protocol): more tools for the model, switched on per chat in the + menu.

A server is a program the app starts (stdio) or an address on the web (streamable HTTP).  Programs run in the
sandbox (sandbox.py), each in a folder of its own, with network through pasta: the internet, but none of the
person's files and nothing on this computer itself, so also not this app, which a browser could otherwise open
and use (its terminal is not sandboxed).  Where that is not possible, or the person switched it off for a server,
a program runs with the person's rights, and the page asks before each of its calls, also in Auto mode.  A server
on the web runs elsewhere: its calls follow the chat's mode like the other tools.

Built in: Playwright, a browser (headless, a fresh profile each start), run with npx; it can be switched off, not
removed.  The settings, with the servers' keys, live in mcp.json in the app's state folder.
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
from pathlib import Path

import httpx

from . import auth, sandbox

PROTOCOL = "2025-06-18"
PLAYWRIGHT = "@playwright/mcp@0.0.84"     # pinned: a new version is a change to the app, not a surprise
START_TIMEOUT = 180                       # the first start of an npx server downloads it
CALL_TIMEOUT = 300
MAX_OUT = 30_000
ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?\Z")   # tool names are "<id>__<tool>": no "__" in an id
BUILTIN = {"playwright": {"name": "Playwright", "transport": "stdio",
                          "description": "Ein Browser: Seiten öffnen, lesen, klicken, Formulare ausfüllen"}}


class McpError(Exception):
    pass


# ---- settings ----------------------------------------------------------------------------------
def _path() -> Path:
    return auth.STATE_DIR / "mcp.json"


def _saved() -> list[dict]:
    try:
        servers = json.loads(_path().read_text()).get("servers")
    except (OSError, ValueError, AttributeError):
        return []
    return [s for s in servers or [] if isinstance(s, dict) and ID_RE.fullmatch(str(s.get("id") or ""))]


def _store(servers: list[dict]) -> None:
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # keys and tokens: only for the person
    with os.fdopen(fd, "w") as f:
        json.dump({"servers": servers}, f, ensure_ascii=False, indent=1)
    tmp.replace(_path())


def load() -> list[dict]:
    """All servers: the built-in ones first (with what the person changed of them), then their own."""
    saved = {s["id"]: s for s in _saved()}
    out = []
    for sid, b in BUILTIN.items():
        s = saved.get(sid, {})
        out.append({"id": sid, **b, "builtin": True, "enabled": s.get("enabled", True) is not False,
                    "sandbox": s.get("sandbox", True) is not False})
    for s in saved.values():
        if s["id"] not in BUILTIN:
            out.append({"id": s["id"], "name": str(s.get("name") or s["id"]), "transport": s.get("transport", "stdio"),
                        "command": str(s.get("command") or ""), "env": dict(s.get("env") or {}),
                        "url": str(s.get("url") or ""), "headers": dict(s.get("headers") or {}),
                        "enabled": s.get("enabled", True) is not False, "sandbox": s.get("sandbox", True) is not False})
    return out


def get(sid: str) -> dict | None:
    return next((s for s in load() if s["id"] == sid), None)


def _pairs(v, what: str) -> dict[str, str]:
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        raise ValueError(f"{what}: name and value as text")
    return {k.strip(): x for k, x in v.items() if k.strip()}


async def put(sid: str | None, data: dict) -> dict:
    """Add a server (sid None) or change one; a running one stops, the next use starts it as it is now."""
    saved = _saved()
    old = next((s for s in saved if s["id"] == sid), None) if sid else None
    if sid and not old and sid not in BUILTIN:
        raise ValueError("unknown server")
    if sid in BUILTIN:                     # of a built-in server only the two switches
        rec = {"id": sid, **{k: bool(data[k]) for k in ("enabled", "sandbox") if k in data}}
        rec = {**(old or {}), **rec}
    else:
        rec = {**(old or {}), **{k: data[k] for k in ("name", "transport", "command", "env", "url", "headers", "enabled", "sandbox") if k in data}}
        rec["name"] = str(rec.get("name") or "").strip()[:60]
        if not rec["name"]:
            raise ValueError("Name fehlt")
        if rec.get("transport") not in ("stdio", "http"):
            raise ValueError("Art: Programm oder Adresse")
        if rec["transport"] == "stdio":
            try:
                if not shlex.split(str(rec.get("command") or "")):
                    raise ValueError
            except ValueError:
                raise ValueError("Befehl fehlt oder ist unvollständig (Anführungszeichen?)") from None
        elif not re.match(r"https?://[^\s/]+", str(rec.get("url") or "")):
            raise ValueError("Adresse: http://… oder https://…")
        rec["env"] = _pairs(rec.get("env") or {}, "Umgebungsvariablen")
        rec["headers"] = _pairs(rec.get("headers") or {}, "Header")
        for k in ("enabled", "sandbox"):
            rec[k] = rec.get(k, True) is not False
        if not sid:
            base = re.sub(r"[^a-z0-9]+", "-", rec["name"].lower()).strip("-")[:20].strip("-") or "server"
            taken = {s["id"] for s in saved} | set(BUILTIN)
            rec["id"] = next(i for i in (base, *(f"{base}-{n}" for n in range(2, 1000))) if i not in taken)
    _store([*(s for s in saved if s["id"] != rec["id"]), rec])
    await stop(rec["id"])
    return next(s for s in await asyncio.to_thread(public) if s["id"] == rec["id"])


async def remove(sid: str) -> None:
    if sid in BUILTIN:
        raise ValueError("Eingebaute Server lassen sich nur ausschalten")
    _store([s for s in _saved() if s["id"] != sid])
    await stop(sid)


def _unboxed(cfg: dict) -> bool:
    """Would a program of this server run with the person's rights (so each call is asked for)?"""
    return cfg["transport"] == "stdio" and not (cfg["sandbox"] and sandbox.network_available())


def public() -> list[dict]:
    """For the page: the settings and what each server is doing."""
    out = []
    for s in load():
        c = _conns.get(s["id"])
        live = bool(c and c.alive)
        out.append({**s, "unboxed": c.unboxed if live else _unboxed(s),
                    "state": "ready" if live else "error" if s["id"] in _errors else "off",
                    "error": None if live else _errors.get(s["id"]),
                    "tools": [t["name"] for t in _tools.get(s["id"], [])] if live else None})
    return out


# ---- talking to a server -----------------------------------------------------------------------
def _message(method: str, params, i: int | None = None) -> dict:
    return {"jsonrpc": "2.0", **({"id": i} if i is not None else {}), "method": method,
            **({"params": params} if params is not None else {})}


def _result(msg: dict) -> dict:
    if "error" in msg:
        e = msg["error"] or {}
        raise McpError(str(e.get("message") or e) if isinstance(e, dict) else str(e))
    return msg.get("result") or {}


class Stdio:
    """A program that speaks JSON-RPC, one message per line, on its stdin and stdout."""

    def __init__(self, argv: list[str], cwd: Path, env: dict | None, unboxed: bool):
        self.argv, self.cwd, self.env, self.unboxed = argv, cwd, env, unboxed
        self.alive, self.changed, self.n, self.pending, self.err = False, False, 0, {}, b""

    async def start(self) -> None:
        # pipes, not the sockets uvloop would hand over: pasta drops output that goes to a socket (see tools._run)
        r_out, w_out = os.pipe()
        r_in, w_in = os.pipe()
        r_err, w_err = os.pipe()
        try:
            self.proc = await asyncio.create_subprocess_exec(*self.argv, cwd=self.cwd, env=self.env, stdin=r_in,
                                                             stdout=w_out, stderr=w_err, start_new_session=True)
        except OSError as e:
            for fd in (r_out, w_in, r_err):
                os.close(fd)
            raise McpError(f"{self.argv[0]}: {e.strerror or e}") from None
        finally:
            for fd in (w_out, r_in, w_err):
                os.close(fd)
        loop = asyncio.get_running_loop()
        self.out = asyncio.StreamReader(limit=64 << 20)              # a page's snapshot can be large
        err = asyncio.StreamReader()
        self._rt, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self.out), os.fdopen(r_out, "rb", 0))
        self._et, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(err), os.fdopen(r_err, "rb", 0))
        self._wt, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(w_in, "wb", 0))
        self.alive = True
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr(err))]

    def tail(self) -> str:
        """The end of what the program wrote to stderr: why it did not start, usually."""
        return self.err.decode("utf-8", "replace").strip()[-800:]

    async def _stderr(self, err: asyncio.StreamReader) -> None:
        while chunk := await err.read(4096):
            self.err = (self.err + chunk)[-8000:]

    async def _read(self) -> None:
        try:
            while line := await self.out.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue                                           # a server that logs to stdout
                if isinstance(msg, dict):
                    self._handle(msg)
        except ValueError:
            pass                                                       # a line over the limit: the stream is lost
        finally:
            self.alive = False
            try:
                await asyncio.sleep(0.2)                               # its last words on stderr
            except asyncio.CancelledError:
                pass
            self._fail()

    def _fail(self) -> None:
        why = McpError("the server ended" + (f": {self.tail()}" if self.tail() else ""))
        for f in self.pending.values():
            if not f.done():
                f.set_exception(why)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        if method and "id" in msg:                                     # the server asks something
            if method == "ping":
                answer = {"result": {}}
            elif method == "roots/list":                               # Playwright: where files may go
                answer = {"result": {"roots": [{"uri": self.cwd.as_uri(), "name": "workspace"}]}}
            else:
                answer = {"error": {"code": -32601, "message": f"{method} is not supported"}}
            self._send({"jsonrpc": "2.0", "id": msg["id"], **answer})
        elif method == "notifications/tools/list_changed":
            self.changed = True
        elif not method:
            f = self.pending.pop(msg.get("id"), None)
            if f and not f.done():
                f.set_result(msg)

    def _send(self, msg: dict) -> None:
        if self.alive:
            self._wt.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())

    async def request(self, method: str, params=None, timeout: float = CALL_TIMEOUT) -> dict:
        if not self.alive:
            raise McpError("the server ended" + (f": {self.tail()}" if self.tail() else ""))
        self.n += 1
        i, f = self.n, asyncio.get_running_loop().create_future()
        self.pending[i] = f
        self._send(_message(method, params, i))
        try:
            return _result(await asyncio.wait_for(f, timeout))
        except asyncio.TimeoutError:
            self._send(_message("notifications/cancelled", {"requestId": i, "reason": "timeout"}))
            raise McpError(f"no answer after {int(timeout)} s") from None
        finally:
            self.pending.pop(i, None)

    async def notify(self, method: str, params=None) -> None:
        self._send(_message(method, params))

    async def close(self) -> None:
        self.alive = False
        for t in (self._wt, self._rt, self._et):
            t.close()
        if self.proc.returncode is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):               # the whole group: npx, node, the browser
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(self.proc.wait(), 3)
                    break
                except asyncio.TimeoutError:
                    continue
        for t in self._tasks:
            t.cancel()
        self._fail()


class Http:
    """A server on the web (streamable HTTP): each message a POST, the answer JSON or an event stream."""
    unboxed = False

    def __init__(self, url: str, headers: dict):
        self.url, self.headers = url, headers
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=20))
        self.alive, self.changed, self.n, self.session, self.version = True, False, 0, None, None

    async def request(self, method: str, params=None, timeout: float = CALL_TIMEOUT) -> dict:
        self.n += 1
        return await self._post(_message(method, params, self.n), timeout)

    async def notify(self, method: str, params=None) -> None:
        await self._post(_message(method, params), 30)

    async def _post(self, msg: dict, timeout: float) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        if self.version:
            h["MCP-Protocol-Version"] = self.version
        try:
            async with self.client.stream("POST", self.url, content=json.dumps(msg), headers=h, timeout=timeout) as r:
                self.session = r.headers.get("mcp-session-id") or self.session
                if "id" not in msg:
                    return {}
                if r.status_code == 404 and self.session:              # the session is gone: start anew next time
                    self.alive = False
                    raise McpError("the server ended the session; try again")
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace").strip()
                    raise McpError(f"HTTP {r.status_code}" + (f": {body[:300]}" if body else ""))
                if "text/event-stream" in r.headers.get("content-type", ""):
                    data = []
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line and data:
                            try:
                                o = json.loads("\n".join(data))
                            except ValueError:
                                o = None
                            data = []
                            if isinstance(o, dict) and o.get("id") == msg["id"] and "method" not in o:
                                return _result(o)
                    raise McpError("the server ended its answer without a result")
                o = json.loads(await r.aread())
                if not isinstance(o, dict):
                    raise McpError("the server's answer is not a JSON-RPC message")
                return _result(o)
        except httpx.HTTPError as e:
            raise McpError(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from None
        except ValueError:
            raise McpError("the server's answer is not JSON") from None

    async def close(self) -> None:
        self.alive = False
        if self.session:
            try:
                await self.client.delete(self.url, headers={**self.headers, "Mcp-Session-Id": self.session}, timeout=5)
            except httpx.HTTPError:
                pass
        await self.client.aclose()


# ---- running servers ---------------------------------------------------------------------------
_conns: dict[str, Stdio | Http] = {}
_tools: dict[str, list[dict]] = {}
_errors: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}


def _browser() -> str | None:
    """Chromium for Playwright: the computer's own, else the one the app's Playwright downloaded.  As a real path:
    the sandbox has /nix/store, not NixOS's /run/current-system/sw/bin."""
    b = auth.system_browser()
    if not b:
        found = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
        b = str(found[-1]) if found else None
    return os.path.realpath(b) if b and os.path.exists(b) else None


def _command(cfg: dict, root: Path) -> tuple[list[str], dict[str, str]]:
    """The server's command line and environment variables."""
    if cfg["id"] == "playwright":
        a = ["npx", "-y", PLAYWRIGHT, "--headless", "--isolated", "--output-dir", str(root), "--output-max-size", "20000000"]
        if b := _browser():
            a += ["--executable-path", b]
        # Playwright keeps a registry in $XDG_CACHE_HOME/ms-playwright, which the sandbox has read-only
        return a, {"XDG_CACHE_HOME": str(Path.home() / ".cache" / "playwright-mcp")}
    return shlex.split(cfg["command"]), cfg.get("env") or {}


async def _start(cfg: dict) -> Stdio | Http:
    if cfg["transport"] == "http":
        c = Http(cfg["url"], cfg["headers"])
    else:
        root = auth.STATE_DIR / "mcp" / cfg["id"]
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        command, env = _command(cfg, root)
        if not shutil.which(command[0]):
            raise McpError(f"{command[0]} not found" + (" (Node.js is needed)" if command[0] in ("npx", "node") else ""))
        if await asyncio.to_thread(_unboxed, cfg):                  # the first check of the sandbox takes a moment
            c = Stdio(command, root, {**os.environ, **env}, True)
        else:
            c = Stdio(sandbox.argv(root, command, True, env), root, None, False)
        await c.start()
    try:
        r = await c.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"roots": {"listChanged": False}},
                                           "clientInfo": {"name": "edu-ki-proxy", "version": "1"}}, START_TIMEOUT)
        if isinstance(c, Http):
            c.version = r.get("protocolVersion") or PROTOCOL
        await c.notify("notifications/initialized")
        _tools[cfg["id"]] = await _list(c)
    except BaseException:
        await c.close()
        raise
    return c


async def _list(c) -> list[dict]:
    out, cursor = [], None
    while True:
        r = await c.request("tools/list", {"cursor": cursor} if cursor else {}, 60)
        out += [t for t in r.get("tools") or [] if isinstance(t, dict) and t.get("name")]
        cursor = r.get("nextCursor")
        if not cursor or len(out) > 500:
            c.changed = False
            return out


async def connect(sid: str) -> Stdio | Http:
    """The running server, started (again) if it is not."""
    cfg = get(sid)
    if not cfg:
        raise McpError(f"unknown MCP server: {sid}")
    if not cfg["enabled"]:
        raise McpError(f"the MCP server {cfg['name']} is switched off in the settings")
    async with _locks.setdefault(sid, asyncio.Lock()):
        c = _conns.get(sid)
        if c and c.alive:
            if c.changed:
                _tools[sid] = await _list(c)
            return c
        if c:
            await c.close()
            _conns.pop(sid, None)
        _errors.pop(sid, None)
        try:
            _conns[sid] = await _start(cfg)
        except McpError as e:
            _errors[sid] = str(e)
            raise
        except Exception as e:
            _errors[sid] = f"{type(e).__name__}: {e}"
            raise McpError(_errors[sid]) from None
        return _conns[sid]


async def tools(sid: str) -> dict:
    """What the page offers the model: the server's tools as they are, and whether each call is asked for."""
    c = await connect(sid)
    return {"tools": [{"name": t["name"], "description": t.get("description") or "",
                       "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}} for t in _tools.get(sid, [])],
            "unboxed": c.unboxed}


def _text(r: dict) -> str:
    parts = []
    for p in r.get("content") or []:
        t = p.get("type") if isinstance(p, dict) else None
        if t == "text":
            parts.append(str(p.get("text") or ""))
        elif t == "resource":
            res = p.get("resource") or {}
            parts.append(str(res.get("text") or f"[resource {res.get('uri', '')}]"))
        elif t == "resource_link":
            parts.append(f"[{p.get('name') or 'resource'}: {p.get('uri') or ''}]")
        elif t in ("image", "audio"):
            parts.append(f"[{t} ({p.get('mimeType') or '?'}), not shown]")
    if not any(parts) and r.get("structuredContent") is not None:
        parts.append(json.dumps(r["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts).strip() or "(no output)"
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... [truncated, {len(text) - MAX_OUT} more characters]"


async def call(sid: str, tool: str, args: dict) -> tuple[str, bool]:
    c = await connect(sid)
    r = await c.request("tools/call", {"name": tool, "arguments": args or {}}, CALL_TIMEOUT)
    text = _text(r)
    if sid == "playwright":        # after an action the page is in a file in the server's folder, out of the model's reach
        text = re.sub(r"- \[Snapshot\]\(\./[^)\s]+\.yml\)", "- not shown here: browser_snapshot returns it", text)
    return text, bool(r.get("isError"))


async def stop(sid: str) -> None:
    async with _locks.setdefault(sid, asyncio.Lock()):
        c = _conns.pop(sid, None)
        _tools.pop(sid, None)
        _errors.pop(sid, None)
        if c:
            await c.close()


async def close_all() -> None:
    await asyncio.gather(*(stop(sid) for sid in list(_conns)), return_exceptions=True)
