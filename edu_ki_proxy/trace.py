"""Debug log: ``python3 -m edu_ki_proxy app --debug`` (or EDU_KI_DEBUG=1).

One line per event in ~/.config/edu-ki-proxy/debug.log (rotated at 10 MB): the requests the app
sends, what goes to the upstream and what comes back, the chunks the app receives, the tools it
runs and the errors it shows.  Lines of one chat request share an id.  The log holds prompts and
file contents (never session tokens), so it is off by default.
"""
import contextvars
import json
import logging
import logging.handlers
import os
import time
import uuid

from . import auth

FILE = auth.STATE_DIR / "debug.log"
_log = logging.getLogger("edu_ki_proxy.trace")
_log.propagate = False      # stays out of uvicorn's console output
_rid = contextvars.ContextVar("trace_rid", default="-")


def enable() -> None:
    if _log.handlers:
        return
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(FILE, maxBytes=10_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%H:%M:%S"))
    _log.addHandler(h)
    _log.setLevel(logging.DEBUG)
    log("debug log on", file=str(FILE))


def on() -> bool:
    return bool(_log.handlers)


def start() -> str:
    """A new id for the lines of this request (and of everything it awaits or streams)."""
    rid = uuid.uuid4().hex[:6]
    _rid.set(rid)
    return rid


def _short(v, cap: int):
    if isinstance(v, str):
        return v if len(v) <= cap else f"{v[:cap // 2]} …[{len(v) - cap} chars]… {v[-cap // 2:]}"
    if isinstance(v, dict):
        return {k: _short(x, cap) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_short(x, cap) for x in v]
    return v


def log(what: str, cap: int = 2000, **fields) -> None:
    if not _log.handlers:
        return
    data = json.dumps(_short(fields, cap), ensure_ascii=False, default=str) if fields else ""
    _log.debug("%s %-12s %s", _rid.get(), what, data)


def messages(msgs: list) -> list:
    """A request's messages, short enough to read: role, text and the tool calls."""
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        e = {"role": m.get("role")}
        c = m.get("content")
        if c:
            e["content"] = _short(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False), 400)
        if m.get("tool_calls"):
            e["calls"] = [f"{(t.get('function') or {}).get('name')}({_short((t.get('function') or {}).get('arguments') or '', 200)})"
                          for t in m["tool_calls"] if isinstance(t, dict)]
        if m.get("tool_call_id"):
            e["id"] = m["tool_call_id"]
        out.append(e)
    return out


async def tap(chunks, label: str):
    """Logs an OpenAI SSE stream as it passes by.  Runs of content (or reasoning) deltas become one
    line, so the log shows when text, tool calls and the end arrived without a line per token."""
    t0, buf, run, done = time.monotonic(), b"", None, False      # run: [kind, text, deltas]

    def flush():
        nonlocal run
        if run:
            log(label, **{run[0]: run[1], "deltas": run[2], "t": round(time.monotonic() - t0, 1)})
            run = None

    def line(data: bytes):
        nonlocal run, done
        if data == b"[DONE]":
            flush()
            done = True
            log(label, done=True, t=round(time.monotonic() - t0, 1))
            return
        try:
            o = json.loads(data)
        except ValueError:
            flush()
            log(label, raw=data.decode("utf-8", "replace"))
            return
        ch = (o.get("choices") or [{}])[0] if isinstance(o, dict) else {}
        d = (ch.get("delta") or {}) if isinstance(ch, dict) else {}
        kinds = [k for k, v in d.items() if v not in (None, "") and k != "role"]
        if len(kinds) == 1 and kinds[0] in ("content", "reasoning_content") and not ch.get("finish_reason"):
            if run and run[0] != kinds[0]:
                flush()
            run = run or [kinds[0], "", 0]
            run[1] += d[kinds[0]]
            run[2] += 1
            return
        flush()
        fields = {k: d[k] for k in kinds}
        if isinstance(ch, dict) and ch.get("finish_reason"):
            fields["finish_reason"] = ch["finish_reason"]
        for k in ("error", "usage"):
            if isinstance(o, dict) and o.get(k):
                fields[k] = o[k]
        if fields:
            log(label, t=round(time.monotonic() - t0, 1), **fields)

    try:
        async for chunk in chunks:
            buf += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                if raw.strip().startswith(b"data:"):
                    line(raw.strip()[5:].strip())
            yield chunk
    finally:
        flush()
        if not done:
            log(label, ended="without [DONE] (client left, or the stream broke)", t=round(time.monotonic() - t0, 1))
        if hasattr(chunks, "aclose"):
            await chunks.aclose()       # whoever closes the tap means the stream underneath


def response(resp, label: str = "to app"):
    """resp, with its body logged on the way out."""
    if not on():
        return resp
    if hasattr(resp, "body_iterator"):
        resp.body_iterator = tap(resp.body_iterator, label)
    else:
        log(label, status=resp.status_code, body=bytes(resp.body).decode("utf-8", "replace"))
    return resp


if os.environ.get("EDU_KI_DEBUG"):
    enable()
