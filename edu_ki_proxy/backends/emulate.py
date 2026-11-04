"""Tool calls for models that never see the request's tools.

Some upstreams drop OpenAI ``tools`` on the way to the model (EDU-KI's Claude, for one), and
DeepSeek's web chat has no tools at all.  For those the tools are described in the system
prompt, the model answers with a fenced ```tool JSON block, and ToolStream turns that block
back into OpenAI tool_calls, so the client cannot tell the difference.
"""
import json
import re
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

from .. import trace

# Request fields that only make sense with native tools.
DROP = {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}


def text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def tool_instruction(tools: list[dict]) -> str:
    defs = []
    for t in tools or []:
        f = t.get("function") if isinstance(t, dict) else None
        if f:
            defs.append({"name": f.get("name"), "description": f.get("description"),
                         "parameters": f.get("parameters")})
    return (
        "You are a coding agent with access to the tools below.\n"
        "When you need a tool, reply with one fenced block tagged `tool` and nothing else in that block:\n"
        "```tool\n"
        '{"name": "read_file", "arguments": {"path": "src/main.py"}}\n'
        "```\n"
        "For several calls use a JSON array of such objects in one block. End your reply right after "
        "the block. Do not invent tool results; the result will be sent to you in the next message. "
        "If no tool is needed, answer normally.\n\nTools:\n" +
        json.dumps(defs, ensure_ascii=False, indent=2)
    )


# ---- tool-call stream filter -------------------------------------------------------------------
# DeepSeek's own tool markup (DSML), which it falls back to now and then instead of a ```tool block:
#   <｜DSML｜function_calls> <｜DSML｜invoke name="read_file">
#   <｜DSML｜parameter name="path" string="true">main.py</｜DSML｜parameter> </｜DSML｜invoke> ...
# The web chat writes the tags as "<｜｜DSML｜｜ calls>", so bars and spaces are matched loosely, and a
# block is recognised by its first invoke: a garbled or missing calls tag does not matter.
_DS = r"<\s*[｜|]+\s*DSML\s*[｜|]+\s*"
_DSE = r"<\s*/\s*[｜|]+\s*DSML\s*[｜|]+\s*"
_OPEN = re.compile(r"```tool(?:_calls?|s)?[ \t]*(?:\r?\n|(?=[\[{]))"
                   r"|(?P<ds>(?:<[^<>\n]{0,30}calls\s*>\s*)?(?=" + _DS + r"invoke\b))")
_HOLD = 64      # longest opener we must be able to see whole, plus slack
_NAME = re.compile(r'\s*\{\s*"name"\s*:\s*"([\w.\-]+)"')   # a single call's name, before its arguments
_DS_NAME = re.compile(_DS + r'invoke\s+name\s*=\s*"([\w.\-]+)"')
_DS_CLOSE = re.compile(r"<\s*/[^<>\n]{0,30}calls\s*>")
_DS_INVOKE = re.compile(_DS + r'invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)' + _DSE + r"invoke\s*>", re.S)
_DS_PARAM = re.compile(_DS + r'parameter\s+name\s*=\s*"([^"]+)"(?:\s+string\s*=\s*"(\w+)")?\s*>(.*?)'
                       + _DSE + r"parameter\s*>", re.S)
_PEEK = 300     # give up waiting for that name after this many characters


def _dsml(block: str) -> list[dict]:
    """The calls of a DSML block; string="false" parameters hold JSON values."""
    items = []
    for name, body in _DS_INVOKE.findall(block):
        args = {}
        for key, is_str, value in _DS_PARAM.findall(body):
            if is_str == "false":
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            args[key] = value
        items.append({"name": name, "arguments": args})
    if not items:
        raise ValueError("no complete DSML invoke")
    return items


class ToolStream:
    """Turns ```tool blocks in the model output into OpenAI-style tool calls.

    The block ends at the first closing fence that completes a valid JSON value, so fences
    inside string arguments (a README with code samples, say) do not cut the call short.
    A call is announced ("call_start") as soon as its name is known, so the client can show
    "writing index.html" while a long block is still arriving.  Text after a call is dropped:
    it can only be the model guessing the tool's result.  DeepSeek's DSML markup is read as well.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.buf = ""
        self.opener = ""        # the opening fence of the block being read
        self.dsml = False       # that block is DeepSeek markup, not JSON
        self.scan = 0           # in a block: where to look for the next closing fence candidate
        self.in_tool = False
        self.peeked = False     # in a block: looked for the call's name already
        self.announced = None   # the call announced for the block being read
        self.calls: list[dict] = []
        self.sent = 0           # tool calls already numbered by start()/delta()
        self.index: dict = {}   # announced call id -> its delta index
        self.raw = ""           # the model's text as it came, for the debug log

    def _text(self, out: list, text: str) -> None:
        if text and not self.calls and not self.announced:
            out.append(("content", text))

    def feed(self, text: str) -> list[tuple[str, object]]:
        self.raw += text
        if not self.enabled:
            return [("content", text)] if text else []
        self.buf += text
        out: list[tuple[str, object]] = []
        while True:
            if not self.in_tool:
                m = _OPEN.search(self.buf)
                if not m:
                    if len(self.buf) > _HOLD:
                        self._text(out, self.buf[:-_HOLD])
                        self.buf = self.buf[-_HOLD:]
                    break
                self._text(out, self.buf[:m.start()])
                self.opener, self.buf, self.dsml = m.group(0), self.buf[m.end():], m.group("ds") is not None
                self.in_tool, self.scan, self.peeked = True, 0, False
                continue
            dsml = self.dsml
            head = self.buf.lstrip()
            if not dsml and head and head[0] not in "[{":          # not JSON after all: plain text
                self._text(out, self.opener + self.buf)
                self.buf, self.in_tool = "", False
                continue
            if not self.peeked:
                m = _DS_NAME.search(self.buf) if dsml else _NAME.match(self.buf)
                if m:
                    self.announced = {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                                      "function": {"name": m.group(1), "arguments": ""}}
                    out.append(("call_start", self.announced))
                self.peeked = bool(m) or (not dsml and head[:1] == "[") or len(self.buf) > _PEEK
            span = self._close_dsml() if dsml else self._close()
            if span is None:
                break
            block, closer, self.buf = self.buf[:span[0]], self.buf[span[0]:span[1]], self.buf[span[1]:]
            self.in_tool = False
            out.extend(self._parse(block, closer))
        return out

    def _close(self) -> tuple[int, int] | None:
        """Span of the closing fence, once the JSON before it parses.  Each candidate is
        tried once: the text before it never changes."""
        while (j := self.buf.find("```", self.scan)) >= 0:
            self.scan = j + 1
            try:
                json.loads(self.buf[:j])
                return j, j + 3
            except ValueError:
                continue
        self.scan = max(self.scan, len(self.buf) - 2)
        return None

    def _close_dsml(self) -> tuple[int, int] | None:
        m = _DS_CLOSE.search(self.buf, self.scan)
        if m:
            return m.span()
        self.scan = max(self.scan, len(self.buf) - _HOLD)
        return None

    def finish(self) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = []
        if self.in_tool:
            out.extend(self._parse(self.buf))
        else:
            self._text(out, self.buf)
        self.buf, self.in_tool = "", False
        trace.log("model output", cap=20000, tools=self.enabled, text=self.raw,
                  calls=[f"{c['function']['name']}({len(c['function']['arguments'])} chars)" for c in self.calls])
        self.raw = ""
        return out

    def start(self, call: dict) -> dict:
        """Streaming delta for an announced call: id and name now, arguments when the block closes."""
        self.index[call["id"]] = self.sent
        self.sent += 1
        return {"index": self.index[call["id"]], "id": call["id"], "type": "function",
                "function": {"name": call["function"]["name"], "arguments": ""}}

    def delta(self, calls: list[dict]) -> list[dict]:
        """Streaming tool_calls delta; indices keep counting across blocks."""
        out = []
        for c in calls:
            if c["id"] in self.index:
                out.append({"index": self.index.pop(c["id"]), "function": {"arguments": c["function"]["arguments"]}})
            else:
                out.append({"index": self.sent, "id": c["id"], "type": "function",
                            "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}})
                self.sent += 1
        return out

    def _parse(self, block: str, closer: str = "") -> list[tuple[str, object]]:
        """The calls of a finished block; closer is its closing fence or tag ("" if cut off)."""
        raw = self.opener + block + closer
        announced, self.announced = self.announced, None
        try:
            obj = _dsml(block) if self.dsml else json.loads(block)
        except ValueError:
            if announced:       # the client already shows this call: let the tool report the broken arguments
                self.calls.append({**announced, "function": {**announced["function"], "arguments": block}})
                return [("tool_calls", [self.calls[-1]])]
            out: list = []
            self._text(out, raw if block.strip() else "")
            return out
        if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
            items = obj["tool_calls"]
        else:
            items = obj if isinstance(obj, list) else [obj]
        made = []
        for item in items:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = item.get("name") or fn.get("name") or item.get("tool")
            args = item.get("arguments", fn.get("arguments", item.get("args", {})))
            if not name:
                continue
            made.append({"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                         "function": {"name": str(name),
                                      "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
        if announced:
            if made and made[0]["function"]["name"] == announced["function"]["name"]:
                made[0]["id"] = announced["id"]
            else:
                made.insert(0, {**announced, "function": {**announced["function"], "arguments": block}})
        if not made:
            out = []
            self._text(out, raw)
            return out
        self.calls.extend(made)
        return [("tool_calls", made)]


# ---- tool calls written as text --------------------------------------------------------------
# An upstream that hides the request's tools from the model (but describes them in the prompt, as
# LiteLLM does for models it thinks cannot call functions) gets answers like
#   Ich schaue mir den Ordner an.
#   Tool: list_dir
#   {"path": "."}
# A line like "Tool: name" (also **bold** or `code`) followed by JSON is taken as such a call.
_TEXT_CALL = re.compile(r"[ \t>]*(?:\*\*|__|`)?\s*tool(?:[ _-]?(?:call|use|name))?\s*:\s*(?:\*\*|__|`)?\s*`?"
                        r"[A-Za-z_][\w.\-]*`?\s*(?:\*\*|__|`)?\s*", re.I)
_LINE_MAX = 160     # a line this long without the call's name is ordinary text


class TextCallWatch:
    """Passes the text of a native-tools answer through until such a call shows up.  Only the start of a
    line that could still become "Tool: ..." is held back, so normal answers stream as before."""

    def __init__(self):
        self.buf = ""
        self.free = False       # the current line is known to be ordinary text

    def feed(self, text: str) -> tuple[str, bool]:
        """(text that can be shown now, whether a call written as text was found)."""
        self.buf += text
        out = []
        while self.buf:
            nl = self.buf.find("\n")
            if self.free:
                if nl < 0:
                    out.append(self.buf)
                    self.buf = ""
                    break
                out.append(self.buf[:nl + 1])
                self.buf, self.free = self.buf[nl + 1:], False
                continue
            line = self.buf if nl < 0 else self.buf[:nl]
            head = line.lstrip(" \t>*_`").lower()
            if not "tool".startswith(head[:4]) or len(line) > _LINE_MAX:
                self.free = True
                continue
            if nl < 0:
                break                              # wait for the rest of the line
            if _TEXT_CALL.fullmatch(line):
                after = self.buf[nl + 1:].lstrip()
                if after.startswith("`"):          # the JSON may come in a ```json block
                    fence = re.match(r"`{3,}[\w-]*[ \t]*\n\s*", after)
                    if fence:
                        after = after[fence.end():]
                    elif "\n" not in after and len(after) < 20:
                        break                      # the fence is still arriving
                if not after:
                    break                          # wait for what follows the line
                if after[0] in "{[":
                    return "".join(out), True
            out.append(line + "\n")
            self.buf = self.buf[nl + 1:]
        return "".join(out), False

    def flush(self) -> str:
        rest, self.buf, self.free = self.buf, "", False
        return rest


def _with_content(o: dict, content: str, keep_finish: bool = True) -> dict:
    o = json.loads(json.dumps(o))
    ch = o["choices"][0]
    ch.setdefault("delta", {})["content"] = content
    if not keep_finish:
        ch["finish_reason"] = None
    return o


async def watch_text_calls(body, retry):
    """A native-tools SSE answer, passed on as it comes, unless the model writes a tool call as text.
    Then the rest is dropped and retry() (tools in the prompt) answers in the same stream."""
    watch = TextCallWatch()
    try:
        async for o in _objects(body):
            ch = (o.get("choices") or [None])[0] if isinstance(o, dict) else None
            d = ch.get("delta") if isinstance(ch, dict) else None
            c = d.get("content") if isinstance(d, dict) else None
            if isinstance(c, str) and c:
                show, found = watch.feed(c)
                if found:
                    if show:
                        yield _sse(_with_content(o, show, keep_finish=False))
                    trace.log("route", how="model wrote a tool call as text: retrying with tools in the prompt")
                    await _close(body)
                    resp = await retry()
                    if "text/event-stream" in resp.headers.get("content-type", ""):
                        async for chunk in resp.body_iterator:
                            yield chunk
                    else:
                        msg = (getattr(resp, "body", b"") or b"").decode("utf-8", "replace")[:300]
                        yield _sse({"error": {"message": f"Anbieter-Fehler: {msg}"}})
                        yield b"data: [DONE]\n\n"
                    return
                if not show and not ch.get("finish_reason") and len(d) == 1:
                    continue                       # everything in this chunk is held back for now
                o = _with_content(o, show)
            elif isinstance(ch, dict) and ch.get("finish_reason") and (rest := watch.flush()):
                yield _sse(_with_content(o, rest, keep_finish=False))    # held text before the end
            yield _sse(o)
        if rest := watch.flush():
            yield _sse({"choices": [{"index": 0, "delta": {"content": rest}, "finish_reason": None}]})
        yield b"data: [DONE]\n\n"
    finally:
        await _close(body)


# ---- chat-completions upstreams that drop tools -----------------------------------------------
def wanted(p: dict, model: str) -> bool:
    return model.lower() in {m.lower() for m in p.get("emulate_tools") or []}


def messages(msgs: list[dict], tools: list[dict]) -> list[dict]:
    """The conversation as plain text turns: tools (if any) described in the system prompt, earlier
    calls as ```tool blocks, their results as user turns.  A history in the format the instruction
    asks for keeps the model to it; DeepSeek switches to its own markup otherwise.  Neighbouring turns of one role are joined, since
    some upstreams insist on alternating roles."""
    out: list[dict] = []
    names: dict = {}

    def put(role: str, content) -> None:
        if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str) and isinstance(content, str):
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})

    for m in msgs:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                names[c.get("id")] = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = fn.get("arguments")
                calls.append({"name": fn.get("name"), "arguments": args})
            block = "```tool\n" + json.dumps(calls[0] if len(calls) == 1 else calls, ensure_ascii=False) + "\n```"
            put("assistant", (text(m.get("content")) + "\n\n" if text(m.get("content")) else "") + block)
        elif role == "tool":
            put("user", f"Tool result ({names.get(m.get('tool_call_id')) or 'tool'}):\n{text(m.get('content'))}")
        elif role == "user":
            put("user", m.get("content") if isinstance(m.get("content"), list) else text(m.get("content")))
        elif role in ("system", "assistant"):
            put(role, text(m.get("content")))
    if not tools:
        return out
    if out and out[0]["role"] == "system":
        out[0]["content"] += "\n\n" + tool_instruction(tools)
    else:
        out.insert(0, {"role": "system", "content": tool_instruction(tools)})
    return out


async def _objects(body):
    """Parsed `data:` objects of an OpenAI SSE byte stream, up to [DONE]."""
    buf = b""
    async for chunk in body:
        buf += chunk
        while (i := buf.find(b"\n")) >= 0:
            line, buf = buf[:i].strip(), buf[i + 1:]
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue
    line = buf.strip()
    if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
        try:
            yield json.loads(line[5:])
        except ValueError:
            pass


async def _events(body, ts: ToolStream):
    """(kind, value) from the upstream stream, with ```tool blocks taken out of the content.
    kinds: meta (id/model), delta (other delta fields), content, call_start, tool_calls, error,
    usage, finish."""
    finish = None
    async for o in _objects(body):
        if not isinstance(o, dict):
            continue
        if o.get("error"):
            yield "error", o
            continue
        yield "meta", (o.get("id"), o.get("model"))
        if o.get("usage"):
            yield "usage", o["usage"]
        for ch in (o.get("choices") or [])[:1]:
            d = ch.get("delta") or {}
            rest = {k: v for k, v in d.items() if k not in ("content", "tool_calls") and v not in (None, "")}
            if rest:
                yield "delta", rest
            for ev in ts.feed(text(d.get("content"))):
                yield ev
            finish = ch.get("finish_reason") or finish
    for ev in ts.finish():
        yield ev
    yield "finish", "tool_calls" if ts.calls else (finish or "stop")


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def _close(body) -> None:
    """Close the upstream body (relay's generator closes the connection) even if the client left early."""
    if hasattr(body, "aclose"):
        await body.aclose()


async def _stream(body, ts: ToolStream, model: str, again=None):
    """OpenAI SSE for the client.  An answer with neither text nor a tool call is asked for once
    more through again() (a new upstream body, or None)."""
    cid, created, usage, shown, failed = "chatcmpl-" + uuid.uuid4().hex[:20], int(time.time()), None, False, False

    def chunk(delta: dict, finish=None) -> bytes:
        return _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    try:
        while True:
            finish = "stop"
            async for kind, value in _events(body, ts):
                if kind == "meta":
                    cid, model = value[0] or cid, value[1] or model
                elif kind == "usage":
                    usage = value
                elif kind == "error":
                    failed = True
                    yield _sse(value)
                elif kind == "delta":
                    yield chunk(value)
                elif kind == "content":
                    shown = shown or bool(value.strip())
                    yield chunk({"content": value})
                elif kind == "call_start":
                    shown = True
                    yield chunk({"tool_calls": [ts.start(value)]})
                elif kind == "tool_calls":
                    shown = True
                    yield chunk({"tool_calls": ts.delta(value)})
                elif kind == "finish":
                    finish = value
            if shown or failed or again is None:
                break
            trace.log("retry", why="empty answer")
            retry, again = await again(), None
            if retry is None:
                break
            await _close(body)
            body = retry
        yield chunk({}, finish)
        if usage:
            yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                        "choices": [], "usage": usage})
        yield b"data: [DONE]\n\n"
    finally:
        await _close(body)


async def _collect(body, ts: ToolStream, model: str, again=None) -> dict:
    content, extra, calls, usage, finish, cid = [], {}, [], None, "stop", None
    try:
        while True:
            async for kind, value in _events(body, ts):
                if kind == "meta":
                    cid, model = value[0] or cid, value[1] or model
                elif kind == "error":
                    return value
                elif kind == "usage":
                    usage = value
                elif kind == "delta":
                    for k, v in value.items():
                        if isinstance(v, str) and k != "role":
                            extra[k] = extra.get(k, "") + v
                elif kind == "content":
                    content.append(value)
                elif kind == "tool_calls":
                    calls.extend(value)
                elif kind == "finish":
                    finish = value
            if calls or "".join(content).strip() or again is None:
                break
            trace.log("retry", why="empty answer")
            retry, again = await again(), None
            if retry is None:
                break
            await _close(body)
            body = retry
    finally:
        await _close(body)
    msg = {"role": "assistant", "content": "".join(content) or None, **extra}
    if calls:
        msg["tool_calls"] = calls
    out = {"id": cid or "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion", "created": int(time.time()),
           "model": model, "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


NUDGE = ("Your last reply was empty. Continue with the task: call the next tool with a ```tool block, "
         "or give your answer.")


def _nudged(msgs: list[dict]) -> list[dict]:
    out = [dict(m) for m in msgs]
    if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], str):
        out[-1]["content"] += "\n\n" + NUDGE
    else:
        out.append({"role": "user", "content": NUDGE})
    return out


def _sse_ok(resp) -> bool:
    return resp.status_code < 400 and "text/event-stream" in resp.headers.get("content-type", "")


async def chat(backend, p: dict, data: dict):
    """backend.chat with the tools moved into the prompt.  Always streams upstream (some only can)
    and answers in whatever form the client asked for."""
    body = {k: v for k, v in data.items() if k not in DROP}
    body["messages"] = messages(data.get("messages") or [], data.get("tools") or [])
    body["stream"] = True
    if trace.on():
        trace.log("rewritten", messages=trace.messages(body["messages"]))
    resp = await backend.chat(p, body)
    if not _sse_ok(resp):
        return resp
    trace.response(resp, "upstream")

    async def again():
        try:
            r = await backend.chat(p, {**body, "messages": _nudged(body["messages"])})
        except Exception as e:
            trace.log("retry", failed=repr(e))
            return None
        if _sse_ok(r):
            return trace.response(r, "upstream").body_iterator
        await _close(r.body_iterator)
        return None

    ts, model = ToolStream(True), data.get("model") or ""
    if data.get("stream"):
        return StreamingResponse(_stream(resp.body_iterator, ts, model, again), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"}, background=resp.background)
    result = await _collect(resp.body_iterator, ts, model, again)
    return JSONResponse(result, status_code=502 if "error" in result and "choices" not in result else 200)
