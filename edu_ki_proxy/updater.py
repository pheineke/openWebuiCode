"""Updates from the git checkout the app runs from (git clone + start.sh, also under Nix).

The app looks for new commits on the branch's upstream (git fetch), shows them in the profile card and
updates with a fast-forward only: local changes and local commits are never touched, the update is then
refused with the reason.  Afterwards the app restarts through start.sh, which installs changed requirements
(or, under Nix, builds the new version).
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

# start.sh says where the checkout is: under Nix the code itself runs from the store
CHECKOUT = Path(os.environ.get("EDU_KI_CHECKOUT") or Path(__file__).resolve().parents[1])
CHECK_EVERY = 3600              # seconds an answer of the server (git fetch) is reused
SHOW_COMMITS = 30

ARGS: list[str] = []            # the app's own arguments (after "app"), for the restart
_cache: dict = {"at": 0.0, "status": None}
_lock = asyncio.Lock()
_restart: dict = {"close": None, "requested": False}


class UpdateError(Exception):
    """The update was refused or failed; the message is for the person."""


async def _git(*args: str, timeout: float = 60) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    if "GIT_SSH_COMMAND" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"     # a background check never asks for a passphrase
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(CHECKOUT), *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise UpdateError("git ist nicht installiert.") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise UpdateError("git hat nicht rechtzeitig geantwortet (Netz?).") from None
    if proc.returncode:
        raise UpdateError(err.decode("utf-8", "replace").strip() or f"git {args[0]} ist fehlgeschlagen.")
    return out.decode("utf-8", "replace").rstrip()       # not lstrip: status --porcelain lines may start with a space


async def _version() -> dict:
    h, ts = (await _git("log", "-1", "--format=%h%x1f%ct", "HEAD")).split("\x1f")
    return {"commit": h, "date": int(ts)}


async def _fetch(remote: str, branch: str) -> None:
    """git fetch from the upstream; a GitHub remote over SSH without a usable key falls back to HTTPS."""
    try:
        await _git("fetch", "--quiet", "--no-tags", remote)
        return
    except UpdateError as e:
        url = await _git("remote", "get-url", remote)
        m = re.fullmatch(r"(?:ssh://)?git@github\.com[:/](.+?)(?:\.git)?/?", url)
        if not m:
            raise UpdateError(f"Konnte nicht nach Updates suchen: {e}") from None
    await _git("fetch", "--quiet", "--no-tags", f"https://github.com/{m.group(1)}.git",
               f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")


async def status(check: bool = False) -> dict:
    """What is installed and what is new.  check=True asks the server even when the last answer is recent."""
    async with _lock:
        if not check and _cache["status"] and time.time() - _cache["at"] < CHECK_EVERY:
            return {**_cache["status"], "restart": can_restart()}
        st = await _status()
        _cache.update(at=time.time(), status=st)
        return {**st, "restart": can_restart()}


async def _status() -> dict:
    out: dict = {"supported": False, "reason": None, "error": None, "version": None, "branch": None, "upstream": None,
                 "checkout": str(CHECKOUT), "behind": 0, "ahead": 0, "commits": [], "more": 0, "dirty": [],
                 "checked_at": int(time.time())}
    if not (CHECKOUT / ".git").exists():
        out["reason"] = "Die App läuft nicht aus einem git-Checkout; neue Versionen gibt es auf GitHub."
        return out
    try:
        out["version"] = await _version()
        out["branch"] = await _git("rev-parse", "--abbrev-ref", "HEAD")
        try:
            upstream = await _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except UpdateError:
            out["reason"] = f"Der Zweig „{out['branch']}“ folgt keinem Zweig auf GitHub."
            return out
        out.update(supported=True, upstream=upstream)
        remote, _, branch = upstream.partition("/")
        try:
            await _fetch(remote, branch)
        except UpdateError as e:
            out["error"] = str(e)                      # what is known locally is still shown
        ahead, behind = (int(n) for n in (await _git("rev-list", "--left-right", "--count", "HEAD...@{u}")).split())
        out.update(ahead=ahead, behind=behind)
        if behind:
            log = await _git("log", f"-n{SHOW_COMMITS}", "--format=%h%x1f%s%x1f%ct", "HEAD..@{u}")
            out["commits"] = [dict(zip(("commit", "subject", "date"), line.split("\x1f"))) for line in log.splitlines()]
            for c in out["commits"]:
                c["date"] = int(c["date"])
            out["more"] = max(0, behind - len(out["commits"]))
        changed = await _git("status", "--porcelain", "--untracked-files=no")
        out["dirty"] = [line[3:] for line in changed.splitlines()]
    except UpdateError as e:
        out["error"] = str(e)
    return out


async def update() -> dict:
    """Fast-forward to the upstream.  Refused with local changes or local commits."""
    st = await status(check=True)
    if not st["supported"]:
        raise UpdateError(st["reason"] or "Update nicht möglich.")
    if st["error"]:
        raise UpdateError(st["error"])
    if not st["behind"]:
        raise UpdateError("Die App ist schon aktuell.")
    if st["ahead"]:
        raise UpdateError(f"Der Checkout hat {st['ahead']} eigene Commit(s), die nicht auf GitHub sind. "
                          "Bitte selbst zusammenführen (git pull).")
    if st["dirty"]:
        raise UpdateError("Geänderte Dateien im Checkout: " + ", ".join(st["dirty"][:5])
                          + (" …" if len(st["dirty"]) > 5 else "") + ". Bitte erst committen oder verwerfen.")
    await _git("merge", "--ff-only", "--quiet", "@{u}")
    _cache["status"] = None
    return {"updated": True, "version": await _version(), "restart": can_restart()}


# ---- restart into the new version --------------------------------------------------------------
def on_restart(close) -> None:
    """The app window registers how to close it (the restart follows once it is gone)."""
    _restart["close"] = close


def can_restart() -> bool:
    return _restart["close"] is not None


def request_restart() -> None:
    if not can_restart():
        raise UpdateError("Bitte die App neu starten.")
    _restart["requested"] = True
    _restart["close"]()


def restart_requested() -> bool:
    return _restart["requested"]


def restart() -> None:
    """Replace this process with the new version, started like the app itself (start.sh: requirements, Nix)."""
    start = CHECKOUT / "start.sh"
    if start.is_file() and os.access(start, os.X_OK):
        os.execv(str(start), [str(start), *ARGS])
    os.execv(sys.executable, [sys.executable, "-m", "edu_ki_proxy", "app", *ARGS])
