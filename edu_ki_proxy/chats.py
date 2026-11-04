"""Local chat store for providers that have no chat history of their own (DeepSeek, generic APIs).

Open WebUI chats stay in Open WebUI; this store only holds chats whose provider
type is not openwebui.
"""
import json
import os
import re
import secrets
import time
from pathlib import Path

from . import auth

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _dir() -> Path:
    d = auth.STATE_DIR / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(cid: str) -> Path:
    if not ID_RE.fullmatch(cid or ""):
        raise ValueError("invalid chat id")
    return _dir() / f"{cid}.json"


def _write(cid: str, chat: dict) -> None:
    p = _path(cid)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(chat, f, ensure_ascii=False)
    tmp.replace(p)


def save(chat: dict) -> dict:
    now = int(time.time())
    cid = str(chat.get("id") or "")
    if not cid:
        cid = "local-" + secrets.token_hex(8)
    elif not ID_RE.fullmatch(cid):
        raise ValueError("invalid chat id")
    old = load(cid) if _path(cid).exists() else None
    rec = dict(chat)
    rec["id"] = cid
    rec["source"] = "local"
    rec["updated_at"] = now
    rec.setdefault("created_at", (old or {}).get("created_at") or now)
    rec.setdefault("pinned", bool((old or {}).get("pinned")))      # the app does not send it: keep it
    _write(cid, rec)
    return rec


def update(cid: str, **fields) -> bool:
    """Change title or pinned; the chat keeps its place in the list (updated_at stays)."""
    if not ID_RE.fullmatch(cid or "") or not _path(cid).exists():
        return False
    rec = load(cid)
    if rec is None:
        return False
    rec.update(fields)
    _write(cid, rec)
    return True


def load(cid: str) -> dict | None:
    try:
        return json.loads(_path(cid).read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def delete(cid: str) -> None:
    try:
        _path(cid).unlink(missing_ok=True)
    except ValueError:
        pass


def list_all() -> list[dict]:
    out = []
    for f in _dir().glob("*.json"):
        try:
            c = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": c.get("id") or f.stem, "title": c.get("title") or "Chat",
                    "updated_at": c.get("updated_at") or 0, "model": c.get("model"),
                    "provider": c.get("provider"), "source": "local", "pinned": bool(c.get("pinned"))})
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out


def _text(chat: dict) -> str:
    msgs = ((chat.get("history") or {}).get("messages") or {}).values()
    return "\n".join(str(m.get("content") or "") for m in msgs)


def search(text: str) -> list[dict]:
    q = (text or "").strip().lower()
    if not q:
        return list_all()[:30]
    hits = []
    for f in _dir().glob("*.json"):
        try:
            c = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        title = str(c.get("title") or "")
        body = _text(c)
        at = body.lower().find(q)
        if q in title.lower() or at >= 0:
            snippet = title if q in title.lower() else body[max(0, at - 40):at + 120].replace("\n", " ")
            hits.append({"id": c.get("id") or f.stem, "title": title or "Chat",
                         "snippet": snippet, "updated_at": c.get("updated_at") or 0,
                         "provider": c.get("provider"), "source": "local"})
    hits.sort(key=lambda c: c["updated_at"], reverse=True)
    return hits
