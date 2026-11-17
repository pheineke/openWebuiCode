"""The user's own skills: instructions plus files (their text) that go into the system prompt.

One folder per skill under ~/.config/edu-ki-proxy/skills/<id>/: skill.json (name, description,
instructions, the files' names and sizes) and <file id>.txt, the text of each file.  The app reads
the files (PDF, Word and the like via extract.py) and sends their text; the originals are not kept.
The composer's + menu lists these skills under the provider id "local", next to Open WebUI's.
"""
import re
import secrets
import shutil
import time
from pathlib import Path

from . import auth
from .chats import _read_json, _write_json

PROVIDER = "local"                  # skillKey "local/<id>" in the app
ID_RE = re.compile(r"[sf]-[0-9a-f]{12}\Z")
MAX_TEXT = 2_000_000                # characters of one file
MAX_FILES = 50


def _root() -> Path:
    d = auth.STATE_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dir(sid: str) -> Path:
    if not ID_RE.fullmatch(sid or "") or not sid.startswith("s-"):
        raise KeyError(sid)
    return _root() / sid


def _clean(value, cap: int, one_line: bool = True) -> str:
    s = str(value or "")
    s = " ".join(s.split()) if one_line else s.replace("\r\n", "\n").strip()
    return s[:cap]


def _load(sid: str) -> dict | None:
    try:
        d = _read_json(_dir(sid) / "skill.json", {})
    except KeyError:
        return None
    return d if d.get("id") == sid else None


def list_all() -> list[dict]:
    """For the + menu and the skills view: newest change first."""
    out = []
    for d in _root().iterdir():
        s = _load(d.name) if d.is_dir() else None
        if s:
            out.append({"id": s["id"], "name": s["name"], "description": s.get("description") or "",
                        "files": len(s.get("files") or []), "updated_at": s.get("updated_at") or 0})
    return sorted(out, key=lambda s: s["updated_at"], reverse=True)


def get(sid: str) -> dict | None:
    """The whole skill for the editor (file texts not included: file_text)."""
    return _load(sid)


def file_text(sid: str, fid: str) -> str | None:
    s = _load(sid)
    if not s or not any(f["id"] == fid for f in s.get("files") or []) or not ID_RE.fullmatch(fid):
        return None
    try:
        return (_dir(sid) / f"{fid}.txt").read_text(encoding="utf-8")
    except OSError:
        return None


def save(sid: str | None, req: dict) -> dict:
    """Create (sid None) or replace a skill.  req["files"]: {"id"} keeps a stored file, {"name", "text",
    "size", "pages"} adds one; stored files left out are removed.  Raises ValueError (message for the
    user) or KeyError (no such skill)."""
    name = _clean(req.get("name"), 80)
    if not name:
        raise ValueError("Der Skill braucht einen Namen.")
    old = None
    if sid:
        old = _load(sid)
        if not old:
            raise KeyError(sid)
    else:
        sid = "s-" + secrets.token_hex(6)
    files_in = req.get("files") or []
    if not isinstance(files_in, list) or len(files_in) > MAX_FILES:
        raise ValueError(f"Höchstens {MAX_FILES} Dateien pro Skill.")
    d = _dir(sid)
    kept = {f["id"]: f for f in (old or {}).get("files") or []}
    files, new_texts = [], {}
    for f in files_in:
        if not isinstance(f, dict):
            continue
        if f.get("id") in kept:
            files.append(kept[f["id"]])
            continue
        text = f.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"„{_clean(f.get('name'), 120) or 'Datei'}“ enthält keinen Text.")
        if len(text) > MAX_TEXT:
            raise ValueError(f"„{_clean(f.get('name'), 120)}“ ist zu lang (höchstens {MAX_TEXT:,} Zeichen).".replace(",", "."))
        fid = "f-" + secrets.token_hex(6)
        new_texts[fid] = text
        files.append({"id": fid, "name": _clean(f.get("name"), 200) or "Datei", "chars": len(text),
                      "size": int(f.get("size") or 0), "pages": int(f.get("pages") or 0)})
    d.mkdir(parents=True, exist_ok=True)        # only now: a rejected new skill leaves no folder
    for fid, text in new_texts.items():
        (d / f"{fid}.txt").write_text(text, encoding="utf-8")
    now = int(time.time())
    rec = {"id": sid, "name": name, "description": _clean(req.get("description"), 300),
           "instructions": _clean(req.get("instructions"), 200_000, one_line=False), "files": files,
           "created_at": (old or {}).get("created_at") or now, "updated_at": now}
    _write_json(d / "skill.json", rec)
    for fid in set(kept) - {f["id"] for f in files}:
        (d / f"{fid}.txt").unlink(missing_ok=True)
    return rec


def delete(sid: str) -> bool:
    if not _load(sid):
        return False
    shutil.rmtree(_dir(sid), ignore_errors=True)
    return True


def content(sid: str) -> str | None:
    """What goes into the system prompt: the instructions, then each file's text."""
    s = _load(sid)
    if not s:
        return None
    parts = [s.get("instructions") or ""]
    files = [(f["name"], file_text(sid, f["id"])) for f in s.get("files") or []]
    files = [(n, t) for n, t in files if t]
    if files:
        parts.append("Files that belong to this skill (use them as reference material):\n\n" + "\n\n".join(
            f'<file name="{n.replace(chr(34), chr(39))}">\n{t.strip()}\n</file>' for n, t in files))
    return "\n\n".join(p for p in parts if p.strip())
