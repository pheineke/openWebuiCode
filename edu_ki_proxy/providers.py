"""Provider store: which AI services are configured, with their sessions.

A provider is a small record in ~/.config/edu-ki-proxy/providers.json.  Secrets
(session cookies/token or an API key) live in the same file, which is created
with mode 0600.  The rest of the app only sees the public projection.  How to
talk to each type of provider lives in backends/.

Sessions are written from request handlers and from login/refresh threads, so
every read-modify-write runs under one lock.
"""
import copy
import json
import os
import re
import secrets
import shutil
import threading
import time
from urllib.parse import urlparse

from . import auth

DEFAULT_ID = "eduki"
FILE = auth.STATE_DIR / "providers.json"
_lock = threading.RLock()
_cache: tuple[tuple, list] | None = None      # (file signature, items)


def _backend(kind: str | None):
    from . import backends      # backends import this module
    return backends.get(kind)


def _now() -> int:
    return int(time.time())


def clean_base(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url:
        return ""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Die Basis-URL muss mit http:// oder https:// beginnen.")
    return url


def _models_list(models) -> list[str]:
    if isinstance(models, str):
        return [m.strip() for m in re.split(r"[,\n]", models) if m.strip()]
    return [str(m).strip() for m in models or [] if str(m).strip()]


def _seed() -> list[dict]:
    """First run: the previous single-instance config (state.json) becomes the EDU-KI provider."""
    return [{"id": DEFAULT_ID, "type": "openwebui", "name": os.environ.get("EDU_KI_NAME", "EDU-KI-RLP"),
             "base_url": auth.BASE, "session": auth.load_state(), "created_at": _now()},
            {"id": "deepseek", "type": "deepseek", "name": "DeepSeek",
             "base_url": "https://chat.deepseek.com", "session": None, "created_at": _now()}]


def _signature() -> tuple | None:
    try:
        st = FILE.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _read() -> list[dict]:
    global _cache
    sig = _signature()
    if sig and _cache and _cache[0] == sig:
        return _cache[1]
    try:
        data = json.loads(FILE.read_text())
        if not isinstance(data, list):
            raise ValueError
    except (OSError, ValueError):
        if FILE.exists():
            raise RuntimeError(f"{FILE} ist beschädigt; bitte prüfen oder löschen.")
        data = _seed()
        _write(data)
        return data
    _cache = (sig, data)
    return data


def _write(items: list[dict]) -> None:
    global _cache
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, ensure_ascii=False)
    tmp.replace(FILE)
    _cache = (_signature(), items)


def load() -> list[dict]:
    """All providers (a copy: change them through the functions below)."""
    with _lock:
        return copy.deepcopy(_read())


def get(pid: str | None) -> dict | None:
    if not pid:
        return None
    with _lock:
        p = next((p for p in _read() if p.get("id") == pid), None)
        return copy.deepcopy(p) if p else None


def update(pid: str, **patch) -> dict | None:
    with _lock:
        items = copy.deepcopy(_read())
        for p in items:
            if p.get("id") == pid:
                p.update(patch)
                _write(items)
                return copy.deepcopy(p)
    return None


def add(data: dict) -> dict:
    kind = str(data.get("type") or "").strip()
    b = _backend(kind)
    if not b:
        raise ValueError("Unbekannter Anbieter-Typ.")
    base = clean_base(data.get("base_url") or b.default_base)
    if b.base_required and not base:
        raise ValueError("Bitte eine Basis-URL angeben.")
    name = str(data.get("name") or "").strip()[:60] or (urlparse(base).hostname if b.base_required else "") or b.label
    rec = {"type": kind, "name": name, "base_url": base, "session": None, "created_at": _now()}
    if "api_key" in b.fields:
        rec["api_key"] = str(data.get("api_key") or "").strip()
    if "models" in b.fields:
        rec["models"] = _models_list(data.get("models"))
    with _lock:
        items = copy.deepcopy(_read())
        pid = secrets.token_hex(4)
        while any(p.get("id") == pid for p in items):
            pid = secrets.token_hex(4)
        rec["id"] = pid
        items.append(rec)
        _write(items)
    return copy.deepcopy(rec)


def edit(pid: str, data: dict) -> dict | None:
    """Settings change from the UI.  A new base URL drops the stored credentials: they belong to the old host."""
    cur = get(pid)
    if not cur:
        return None
    patch = {}
    if "name" in data:
        patch["name"] = str(data["name"] or "").strip()[:60] or cur.get("name")
    if "base_url" in data:
        base = clean_base(data["base_url"])
        if base != cur.get("base_url"):
            patch.update(base_url=base, session=None)
            if "api_key" not in data:
                patch["api_key"] = ""
    if "api_key" in data:
        patch["api_key"] = str(data["api_key"] or "").strip()
    if "models" in data:
        patch["models"] = _models_list(data["models"])
    if "emulate_tools" in data:       # model ids, with or without the "provider/" prefix
        patch["emulate_tools"] = [m.removeprefix(pid + "/") for m in _models_list(data["emulate_tools"])]
    return update(pid, **patch) if patch else cur


def remove(pid: str) -> None:
    with _lock:
        _write([p for p in _read() if p.get("id") != pid])
    shutil.rmtree(auth.STATE_DIR / f"browser-profile-{pid}", ignore_errors=True)


def set_session(pid: str, session: dict | None) -> dict | None:
    return update(pid, session=session)


def mark_expired(pid: str) -> None:
    """Keep the session (and its user) for display, but stop treating it as logged in."""
    with _lock:
        p = get(pid)
        if p and p.get("session"):
            update(pid, session={**p["session"], "expired": True})


def clear_session(pid: str) -> None:
    update(pid, session=None, api_key="")


def logged_in(p: dict) -> bool:
    b = _backend(p.get("type"))
    return bool(b and b.logged_in(p))


def public(p: dict) -> dict:
    b = _backend(p.get("type"))
    s = p.get("session") or {}
    out = {"id": p.get("id"), "type": p.get("type"), "name": p.get("name"), "base_url": p.get("base_url"),
           "logged_in": logged_in(p), "expired": bool(s.get("expired")), "user": s.get("user"),
           "login": b.login if b else None}
    if b:
        out.update(b.public(p))
        if b.native_tools:
            out["emulate_tools"] = p.get("emulate_tools") or []
    return out


def all_public() -> list[dict]:
    return [public(p) for p in load()]


def default_openwebui() -> dict | None:
    items = load()
    return next((p for p in items if p.get("type") == "openwebui" and logged_in(p)), None) \
        or next((p for p in items if p.get("type") == "openwebui"), None)


def default() -> dict | None:
    """The provider used for unprefixed model names (old opencode configs)."""
    items = [p for p in load() if logged_in(p)]
    return next((p for p in items if p.get("type") == "openwebui"), None) or (items[0] if items else None)


def split_model(model: str) -> tuple[str | None, str]:
    """'pid/model' -> (pid, model).  Unprefixed names (and unknown prefixes) -> (None, model)."""
    if "/" in model:
        pid, name = model.split("/", 1)
        if get(pid):
            return pid, name
    return None, model
