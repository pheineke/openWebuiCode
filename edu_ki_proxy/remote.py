"""SSH backend: the same tools, file panel, preview and terminal, executed on a remote host.

Uses the system `ssh` client, so ~/.ssh/config, keys and ssh-agent work as usual, and connections are
shared through ControlMaster (one login, many quick commands). Tools run with BatchMode, i.e. key/agent
only; the interactive terminal may still ask for a password. Host key checking is never disabled.

Every remote command is a small POSIX `sh` script that receives its inputs as arguments (never spliced
into the script text), so file names and commands cannot break out of their quoting.
"""
import asyncio
import json
import os
import posixpath
import re
import secrets
import shlex
import tempfile
from pathlib import Path

from . import auth
from .tools import MAX_OUT, SKIP_DIRS, ToolError, _clip, apply_edit, format_read

HOST_RE = re.compile(r"[A-Za-z0-9._%@:\[\]-]+")
MAX_READ = 2_000_000
MAX_PREVIEW = 20_000_000


class SshError(ToolError):
    pass


# ---- saved connections --------------------------------------------------------------------------
def _store() -> Path:
    return auth.STATE_DIR / "ssh.json"


def load() -> list[dict]:
    try:
        return json.loads(_store().read_text())
    except (OSError, ValueError):
        return []


def _save(conns: list[dict]) -> None:
    auth.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _store().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(conns, f)
    tmp.replace(_store())


def get(conn_id: str | None) -> dict | None:
    return next((c for c in load() if c["id"] == conn_id), None) if conn_id else None


def normalize(d: dict) -> dict:
    """Validate user input. The host ends up as an ssh argument, so it must never look like an option."""
    name = str(d.get("name", "")).strip()[:60]
    host = str(d.get("host", "")).strip()
    if not name:
        raise SshError("Bitte einen Namen angeben.")
    if not host or host.startswith("-") or not HOST_RE.fullmatch(host):
        raise SshError("Ungültiger Host. Erlaubt sind user@host, ein Host aus ~/.ssh/config oder eine IP-Adresse.")
    port = d.get("port")
    if port in (None, ""):
        port = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise SshError("Der Port muss eine Zahl sein.")
        if not 1 <= port <= 65535:
            raise SshError("Der Port muss zwischen 1 und 65535 liegen.")
    key = str(d.get("key", "")).strip()
    if "\n" in key or "\0" in key:
        raise SshError("Ungültiger Schlüsselpfad.")
    folder = str(d.get("folder", "")).strip()
    if "\n" in folder or "\0" in folder:
        raise SshError("Ungültiger Ordner.")
    return {"name": name, "host": host, "port": port, "key": key, "folder": folder, "accept_new": bool(d.get("accept_new"))}


def add(d: dict) -> dict:
    conn = {"id": secrets.token_hex(4), **normalize(d)}
    _save(load() + [conn])
    return conn


def remove(conn_id: str) -> None:
    _save([c for c in load() if c["id"] != conn_id])


# ---- running commands ---------------------------------------------------------------------------
def _ctl_dir() -> str:
    d = Path(tempfile.gettempdir()) / f"edu-ki-ssh-{os.getuid()}"   # short path: unix sockets have a ~100 char limit
    d.mkdir(mode=0o700, exist_ok=True)
    return str(d)


def argv(conn: dict, tty: bool = False, fresh: bool = False) -> list[str]:
    a = ["ssh"]
    cfg = os.environ.get("EDU_KI_SSH_CONFIG")          # a dedicated ssh config instead of ~/.ssh/config
    if cfg:
        a += ["-F", cfg]
    a += ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if fresh:   # a connection test must check the given credentials, not ride on an already open connection
        a += ["-o", "ControlPath=none"]
    else:
        a += ["-o", "ControlMaster=auto", "-o", f"ControlPath={_ctl_dir()}/%C", "-o", "ControlPersist=300"]
    if not tty:
        a += ["-o", "BatchMode=yes"]                   # tools never wait for a password
    if conn.get("accept_new"):
        a += ["-o", "StrictHostKeyChecking=accept-new"]  # trust a host on first sight; changed keys are still refused
    if conn.get("port"):
        a += ["-p", str(conn["port"])]
    if conn.get("key"):
        a += ["-i", os.path.expanduser(conn["key"]), "-o", "IdentitiesOnly=yes"]
    if tty:
        a += ["-tt"]
    return a + ["--", conn["host"]]                    # `--`: the host can never be taken for an option


def explain(err: str) -> str:
    e = err.lower()
    if "host key verification failed" in e or "identification has changed" in e:
        hint = ("Der Hostschlüssel ist unbekannt oder hat sich geändert. Bei einem neuen Host hilft „Hostschlüssel beim "
                "ersten Verbinden akzeptieren“; hat er sich geändert, bitte prüfen, ob der Server wirklich derselbe ist.")
    elif "permission denied" in e:
        hint = ("Anmeldung abgelehnt. Die Werkzeuge nutzen nur Schlüssel oder den ssh-agent (kein Passwort). "
                "Schlüsselpfad prüfen oder den Schlüssel per ssh-add laden.")
    elif "could not resolve" in e:
        hint = "Der Host wurde nicht gefunden."
    elif "connection refused" in e or "timed out" in e or "no route" in e:
        hint = "Der Server ist nicht erreichbar (Adresse, Port, VPN oder Firewall prüfen)."
    else:
        hint = "SSH-Verbindung fehlgeschlagen."
    return f"{hint}\n{err}".strip()


def _is_ssh_failure(err: str) -> bool:
    e = err.lower()
    return any(k in e for k in ("ssh:", "permission denied", "host key verification", "connection refused", "connection timed out",
                                "could not resolve", "kex_exchange", "no route to host", "connection closed", "connection reset"))


async def run(conn: dict, script: str, args=(), stdin: bytes | None = None, timeout: float = 60, fresh: bool = False) -> tuple[int, bytes, str]:
    """Run `script` with `sh -c` on the host. Returns (exit code, stdout, stderr)."""
    cmd = "sh -c " + shlex.quote(script) + " sh" + "".join(" " + shlex.quote(a) for a in args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv(conn, fresh=fresh), cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise SshError("Der ssh-Client ist nicht installiert.")
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SshError(f"SSH-Befehl nach {int(timeout)} s abgebrochen (keine Antwort).")
    text = err.decode("utf-8", errors="replace").strip()
    if proc.returncode == 255 and _is_ssh_failure(text):
        raise SshError(explain(text))
    return proc.returncode, out, text


# Remote scripts. Inputs arrive as $1, $2, ...; exit codes 90-95 mean "tool error", the message is on stderr.
_ROOT = r'''d=$1; case "$d" in "~") d=$HOME;; "~/"*) d=$HOME/${d#"~/"};; esac; [ -n "$d" ] || d=$HOME
root=$(cd "$d" 2>/dev/null && pwd -P) || { echo "no such folder: $1" >&2; exit 90; }
'''
# Resolve $2 on the host (following symlinks) and refuse anything outside the working folder.
_PATH = _ROOT + r'''case "$2" in /*) p=$2;; *) p=$root/$2;; esac
t=$(realpath -m -- "$p") || { echo "cannot resolve path: $2" >&2; exit 92; }
case "$t" in "$root"|"$root"/*) ;; *) echo "path is outside the working folder: $2" >&2; exit 91;; esac
'''
_FILE = r'''[ -f "$t" ] || { echo "not a file: $2" >&2; exit 93; }
sz=$(wc -c <"$t")
'''


def _check(rc: int, err: str, out: bytes = b"") -> None:
    if rc != 0:
        raise ToolError(err or out.decode("utf-8", errors="replace").strip() or f"remote command failed (exit code {rc})")


async def _read_text(conn, workdir, path) -> str:
    rc, out, err = await run(conn, _PATH + _FILE + f'[ "$sz" -le {MAX_READ} ] || {{ echo "file too large (over 2 MB)" >&2; exit 95; }}\ncat -- "$t"', [workdir, path])
    _check(rc, err)
    if b"\0" in out[:8192]:
        raise ToolError("binary file")
    return out.decode("utf-8", errors="replace")


async def _write_text(conn, workdir, path, content: str) -> int | None:
    """Write the file; returns its previous line count, or None when it is new."""
    script = _PATH + r'''if [ -e "$t" ]; then [ -f "$t" ] || { echo "not a file: $2" >&2; exit 93; }; grep -c '' -- "$t"; else echo NEW; fi
mkdir -p -- "$(dirname -- "$t")" && cat > "$t"'''
    rc, out, err = await run(conn, script, [workdir, path], stdin=content.encode("utf-8"))
    _check(rc, err)
    first = out.decode().strip().splitlines()[0] if out.strip() else "NEW"
    return None if first == "NEW" else int(first)


# ---- the tools (same names, arguments and outputs as the local ones) ----------------------------
async def read_file(conn, workdir, path, offset=1, limit=2000):
    return format_read(await _read_text(conn, workdir, path), offset, limit), None


async def write_file(conn, workdir, path, content):
    old_lines = await _write_text(conn, workdir, path, content)
    meta = {"created": old_lines is None, "added": len(content.splitlines()), "removed": old_lines or 0}
    return f"{'Created' if old_lines is None else 'Overwrote'} {path} ({len(content.encode())} bytes)", meta


async def edit_file(conn, workdir, path, old_string, new_string, replace_all=False):
    new_text, n = apply_edit(await _read_text(conn, workdir, path), old_string, new_string, replace_all)
    await _write_text(conn, workdir, path, new_text)
    meta = {"created": False, "added": len(new_string.splitlines()) * n, "removed": len(old_string.splitlines()) * n}
    return f"Edited {path} ({n} replacement{'s' if n > 1 else ''})", meta


async def list_dir(conn, workdir, path="."):
    rc, out, err = await run(conn, _PATH + r'''[ -d "$t" ] || { echo "not a directory: $2" >&2; exit 94; }
ls -1Ap -- "$t" | head -n 1000''', [workdir, path])
    _check(rc, err)
    names = [n for n in out.decode("utf-8", errors="replace").splitlines() if n.rstrip("/") not in SKIP_DIRS]
    names.sort(key=lambda n: (not n.endswith("/"), n.lower()))
    return "\n".join(names) or "(empty directory)", None


def _glob_regex(pattern: str) -> re.Pattern:
    i, out = 0, ""
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pattern.startswith("**", i):
            out += ".*"; i += 2; continue
        c = pattern[i]
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[" and (j := pattern.find("]", i + 2)) != -1:
            out += "[^" + pattern[i + 2:j] + "]" if pattern[i + 1] == "!" else pattern[i:j + 1]
            i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + r"\Z")


async def glob(conn, workdir, pattern, path="."):
    prune = " -o ".join(f"-name {n}" for n in sorted(SKIP_DIRS))
    rc, out, err = await run(conn, _PATH + r'''[ -d "$t" ] || { echo "not a directory: $2" >&2; exit 94; }
rel=${t#"$root"}; rel=${rel#/}; [ -n "$rel" ] || rel=.
printf '%s\n' "$rel"
cd "$root" && find "$rel" \( ''' + prune + r''' \) -prune -o -type f -print | head -n 20000''', [workdir, path])
    _check(rc, err)
    lines = out.decode("utf-8", errors="replace").splitlines()
    rel, files = (lines[0] if lines else "."), lines[1:]
    rx, hits = _glob_regex(pattern), []
    for f in files:
        f = f[2:] if f.startswith("./") else f
        inner = f if rel == "." else f[len(rel) + 1:]
        if rx.match(inner):
            hits.append(f)
    hits.sort()
    return "\n".join(hits[:500]) or "no matches", None


async def grep(conn, workdir, pattern, path=".", include=None):
    try:
        re.compile(pattern)                                  # same early check as the local tool
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    excl = " ".join(f"--exclude-dir={n}" for n in sorted(SKIP_DIRS))
    rc, out, err = await run(conn, _PATH + r'''rel=${t#"$root"}; rel=${rel#/}; [ -n "$rel" ] || rel=.
cd "$root" || exit 90
if echo x | grep -P x >/dev/null 2>&1; then m=-P; else m=-E; fi    # PCRE is closest to Python's regex
if [ -n "$4" ]; then grep -rnI $m --include="$4" ''' + excl + r''' -e "$3" -- "$rel" 2>&1
else grep -rnI $m ''' + excl + r''' -e "$3" -- "$rel" 2>&1; fi | head -n 201''', [workdir, path, pattern, include or ""])
    _check(rc, err)
    lines = out.decode("utf-8", errors="replace").splitlines()
    if lines and lines[0].startswith("grep:") and not re.match(r"^[^:]+:\d+:", lines[0]):
        raise ToolError(lines[0])
    res = []
    for ln in lines[:200]:
        ln = ln[2:] if ln.startswith("./") else ln
        res.append(ln[:300])
    text = "\n".join(res) or "no matches"
    if len(lines) > 200:
        text += "\n... [limited to 200 matches]"
    return text, None


async def bash(conn, workdir, command, timeout=120):
    timeout = min(max(int(timeout), 1), 600)
    script = 'exec 2>&1\n' + _ROOT + r'''cd "$root" || exit 90
b=bash; command -v bash >/dev/null 2>&1 || b=sh
if command -v timeout >/dev/null 2>&1; then timeout -k 5 "$3" "$b" -c "$2"; else "$b" -c "$2"; fi'''
    rc, out, err = await run(conn, script, [workdir, command, str(timeout)], timeout=timeout + 20)
    text = out.decode("utf-8", errors="replace")
    if rc == 90:
        raise ToolError(text.strip() or "no such folder")
    if rc == 124:
        raise ToolError(f"command timed out after {timeout}s")
    if len(text) > MAX_OUT:
        text = f"[truncated {len(text) - MAX_OUT} chars]\n" + text[-MAX_OUT:]
    return f"{text}\n[exit code {rc}]".strip(), None


TOOLS = {"read_file": read_file, "write_file": write_file, "edit_file": edit_file, "list_dir": list_dir,
         "glob": glob, "grep": grep, "bash": bash}


async def run_tool(conn: dict, workdir: str, name: str, args: dict) -> tuple[str, dict | None]:
    fn = TOOLS.get(name)
    if not fn:
        raise ToolError(f"unknown tool: {name}")
    try:
        text, meta = await fn(conn, workdir, **args)
    except TypeError as e:
        raise ToolError(f"bad arguments for {name}: {e}")
    return _clip(text), meta


# ---- for the file panel, folder picker, preview and terminal ------------------------------------
async def read_raw(conn, workdir, path) -> str:
    return await _read_text(conn, workdir, path)


async def read_bytes(conn, workdir, path) -> bytes:
    rc, out, err = await run(conn, _PATH + r'''[ -d "$t" ] && t=$t/index.html
''' + _FILE + f'[ "$sz" -le {MAX_PREVIEW} ] || {{ echo "file too large" >&2; exit 95; }}\ncat -- "$t"', [workdir, path or "."], timeout=120)
    _check(rc, err)
    return out


async def ls_entries(conn, workdir, path=".") -> list[dict]:
    text, _ = await list_dir(conn, workdir, path)
    if text == "(empty directory)":
        return []
    return [{"name": n.rstrip("/"), "dir": n.endswith("/")} for n in text.splitlines()]


async def fs_list(conn, path: str = "") -> dict:
    """Directories for the folder picker (no confinement: the user is choosing where to work)."""
    rc, out, err = await run(conn, r'''d=$1; case "$d" in "~") d=$HOME;; "~/"*) d=$HOME/${d#"~/"};; esac; [ -n "$d" ] || d=$HOME
cd "$d" 2>/dev/null || { echo "$1: no such directory" >&2; exit 90; }
pwd -P
for e in *; do [ -d "$e" ] && printf '%s\n' "$e"; done
exit 0''', [path])   # the loop's own status is that of its last test, not an error
    _check(rc, err)
    lines = out.decode("utf-8", errors="replace").splitlines()
    here = lines[0] if lines else "/"
    return {"path": here, "parent": posixpath.dirname(here) if here != "/" else None, "dirs": sorted(lines[1:], key=str.lower)}


MKDIR_SCRIPT = r'''d=$1; case "$d" in "~") d=$HOME;; "~/"*) d=$HOME/${d#"~/"};; esac; [ -n "$d" ] || d=$HOME
cd "$d" 2>/dev/null || { echo "$1: Ordner nicht gefunden" >&2; exit 90; }
[ -e "$2" ] && { echo "„$2“ gibt es hier schon." >&2; exit 91; }
mkdir -- "$2" || exit 92
cd -- "$2" && pwd -P'''


async def mkdir(conn, path: str, name: str) -> dict:
    """New folder `name` inside `path` (folder picker); returns its full path."""
    rc, out, err = await run(conn, MKDIR_SCRIPT, [path, name])
    _check(rc, err)
    return {"path": out.decode("utf-8", errors="replace").strip().splitlines()[-1]}


async def test(fields: dict) -> dict:
    """Try a connection without saving it."""
    conn = normalize(fields)
    rc, out, err = await run(conn, r'''echo "$(id -un)@$(hostname 2>/dev/null || echo host) $(uname -sr)"
echo "$HOME"
for c in realpath timeout find grep; do command -v $c >/dev/null 2>&1 || echo "MISSING:$c"; done
realpath -m / >/dev/null 2>&1 || echo "MISSING:realpath -m"''', timeout=25, fresh=True)
    _check(rc, err)
    lines = out.decode("utf-8", errors="replace").splitlines()
    return {"ok": True, "info": lines[0] if lines else "", "home": lines[1] if len(lines) > 1 else "",
            "warnings": [l[8:] for l in lines[2:] if l.startswith("MISSING:")]}


def terminal_argv(conn: dict, workdir: str) -> list[str]:
    """Interactive shell on the host, started in the chat's folder (password prompts work here)."""
    script = (r'''d=$1; case "$d" in "~") d=$HOME;; "~/"*) d=$HOME/${d#"~/"};; esac; [ -n "$d" ] || d=$HOME
cd "$d" 2>/dev/null || cd; exec "${SHELL:-/bin/sh}" -l''')
    return argv(conn, tty=True) + ["sh -c " + shlex.quote(script) + " sh " + shlex.quote(workdir or "")]
