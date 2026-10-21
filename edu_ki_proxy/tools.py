"""Local tools the model can call. File tools are confined to the chat's working folder."""
import asyncio
import os
import re
import signal
from pathlib import Path

MAX_OUT = 30_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


class ToolError(Exception):
    pass


def _root(workdir: str) -> Path:
    root = Path(workdir).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"working folder does not exist: {workdir}")
    return root


def _path(root: Path, p: str) -> Path:
    full = (root / p).resolve()  # absolute p replaces root; .resolve() follows symlinks
    if full != root and not full.is_relative_to(root):
        raise ToolError(f"path is outside the working folder: {p}")
    return full


def _clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    return text[:MAX_OUT] + f"\n... [truncated, {len(text) - MAX_OUT} more characters]"


def _read_text(f: Path) -> str:
    if not f.is_file():
        raise ToolError(f"not a file: {f.name}")
    data = f.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError("binary file")
    return data.decode("utf-8", errors="replace")


def read_file(root, path, offset=1, limit=2000):
    lines = _read_text(_path(root, path)).splitlines()
    offset = max(int(offset), 1)
    chunk = lines[offset - 1: offset - 1 + int(limit)]
    out = "\n".join(f"{i:>6}\t{l}" for i, l in enumerate(chunk, offset))
    if offset - 1 + len(chunk) < len(lines):
        out += f"\n... [{len(lines)} lines total, use offset to read more]"
    return out or "(empty file)"


def write_file(root, path, content):
    f = _path(root, path)
    old_lines = len(_read_text(f).splitlines()) if f.exists() else None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    meta = {"created": old_lines is None, "added": len(content.splitlines()), "removed": old_lines or 0}
    return f"{'Created' if old_lines is None else 'Overwrote'} {path} ({len(content.encode())} bytes)", meta


def edit_file(root, path, old_string, new_string, replace_all=False):
    f = _path(root, path)
    text = _read_text(f)
    n = text.count(old_string)
    if not old_string:
        raise ToolError("old_string must not be empty (use write_file to create files)")
    if n == 0:
        raise ToolError("old_string not found in file (it must match exactly, including whitespace)")
    if n > 1 and not replace_all:
        raise ToolError(f"old_string occurs {n} times; add more context to make it unique or set replace_all")
    f.write_text(text.replace(old_string, new_string), encoding="utf-8")
    meta = {"created": False, "added": len(new_string.splitlines()) * n, "removed": len(old_string.splitlines()) * n}
    return f"Edited {path} ({n} replacement{'s' if n > 1 else ''})", meta


def list_dir(root, path="."):
    d = _path(root, path)
    if not d.is_dir():
        raise ToolError(f"not a directory: {path}")
    entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries[:500]) or "(empty directory)"


def _walk(base: Path):
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            yield Path(dp) / fn


def glob(root, pattern, path="."):
    base = _path(root, path)
    hits = [p for p in base.glob(pattern) if not SKIP_DIRS.intersection(p.relative_to(root).parts)]
    hits.sort()
    return "\n".join(str(p.relative_to(root)) for p in hits[:500]) or "no matches"


def grep(root, pattern, path=".", include=None):
    base = _path(root, path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    out = []
    files = [base] if base.is_file() else _walk(base)
    for f in files:
        if include and not f.match(include):
            continue
        try:
            if f.stat().st_size > 1_000_000:
                continue
            text = _read_text(f)
        except (ToolError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{f.relative_to(root)}:{i}:{line[:300]}")
                if len(out) >= 200:
                    return "\n".join(out) + "\n... [limited to 200 matches]"
    return "\n".join(out) or "no matches"


async def bash(root, command, timeout=120):
    timeout = min(max(int(timeout), 1), 600)
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", command, cwd=root, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise ToolError(f"command timed out after {timeout}s")
    text = out.decode("utf-8", errors="replace")
    if len(text) > MAX_OUT:  # keep the tail: errors are usually at the end
        text = f"[truncated {len(text) - MAX_OUT} chars]\n" + text[-MAX_OUT:]
    return f"{text}\n[exit code {proc.returncode}]".strip()


SYNC = {"read_file": read_file, "write_file": write_file, "edit_file": edit_file,
        "list_dir": list_dir, "glob": glob, "grep": grep}


MAX_VIEW = 2_000_000


def read_raw(workdir: str, path: str) -> str:
    """Whole file for the UI's file panel (confined like the tools)."""
    f = _path(_root(workdir), path)
    if f.is_file() and f.stat().st_size > MAX_VIEW:
        raise ToolError("file too large to preview")
    return _read_text(f)


def ls(workdir: str, path: str = ".") -> list[dict]:
    root = _root(workdir)
    d = _path(root, path)
    if not d.is_dir():
        raise ToolError(f"not a directory: {path}")
    entries = sorted((e for e in d.iterdir() if e.name not in SKIP_DIRS), key=lambda e: (not e.is_dir(), e.name.lower()))
    return [{"name": e.name, "dir": e.is_dir()} for e in entries[:1000]]


async def run(workdir: str, name: str, args: dict) -> tuple[str, dict | None]:
    """Returns (output text for the model, optional metadata for the UI)."""
    root = _root(workdir)
    try:
        if name == "bash":
            return await bash(root, **args), None
        if name in SYNC:
            res = await asyncio.to_thread(SYNC[name], root, **args)
            text, meta = res if isinstance(res, tuple) else (res, None)
            return _clip(text), meta
    except TypeError as e:
        raise ToolError(f"bad arguments for {name}: {e}")
    except OSError as e:
        raise ToolError(str(e))
    raise ToolError(f"unknown tool: {name}")
