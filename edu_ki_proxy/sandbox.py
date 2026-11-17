"""The model's shell commands in a sandbox (bubblewrap), so the chat's folder is really the limit.

Inside a sandbox there is
  - the system (/usr, /etc, ...) read-only, and a few toolchain folders of the home (~/.local/bin, ...) read-only;
  - the working folder, writable, except .git/hooks and .git/config: git runs those when the person works in the
    folder later, outside any sandbox;
  - a home of its own per working folder (caches stay between commands, nothing of the person's home is there,
    nothing from other projects) and a fresh /tmp for each command;
  - no environment from the app: no SSH agent, D-Bus, keyring or display; not this computer's name or machine ID;
  - its own processes, which all end with the command;
  - network only when the chat allows it, and then through pasta: the internet, but nothing on this computer
    itself (the app's server listens on 127.0.0.1 and would hand out an unsandboxed terminal).

Folders that hold keys and sessions (the app's own state, ~/.ssh, ~/.gnupg) stay hidden even when the person
chose a working folder that contains them, e.g. the whole home.  Without bubblewrap commands run as before,
and the app asks before every one.
"""
import functools
import hashlib
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import auth

HOME = Path.home()
SYSTEM = ("/usr", "/etc", "/opt", "/nix", "/gnu")
MERGED = ("bin", "sbin", "lib", "lib64", "lib32")              # usually symlinks into /usr
TOOLCHAINS = (".local/bin", ".local/lib", ".cargo/bin", ".rustup", ".nvm", ".pyenv", ".volta", ".deno", ".bun", "go/bin")
DNS_IN = "169.254.1.53"                                         # the sandbox's nameserver, forwarded by pasta


def protected() -> list[Path]:
    """Never readable or writable for the model, wherever the working folder is."""
    return [p.resolve() for p in (auth.STATE_DIR, HOME / ".ssh", HOME / ".gnupg")]


@functools.cache
def available() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False          # e.g. user namespaces switched off


@functools.cache
def _pasta() -> list[str] | None:
    """pasta's options for a network without this computer (None: no pasta)."""
    if not shutil.which("pasta"):
        return None
    try:
        usage = subprocess.run(["pasta", "--help"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # pasta stays until the command ends (--foreground).  No port forwarding either way, and no address that leads
    # to the host's loopback.  Its stdout must be a pipe: output to a socket is dropped (see tools._run).
    a = ["pasta", "--config-net", "--quiet", "--foreground", "-t", "none", "-u", "none", "-T", "none", "-U", "none",
         *(["--map-host-loopback", "none"] if "--map-host-loopback" in usage else ["--no-map-gw"])]
    host_dns = _nameserver()
    if host_dns:
        a += ["--dns-forward", DNS_IN, "--dns-host", host_dns]
    try:
        ok = subprocess.run([*a, "--", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return a if ok else None


def network_available() -> bool:
    return available() and _pasta() is not None


def _nameserver() -> str | None:
    try:
        text = Path("/etc/resolv.conf").read_text()
    except OSError:
        return None
    m = re.search(r"^\s*nameserver\s+(\S+)", text, re.M)
    return m.group(1) if m else None


def _home(root: Path) -> Path:
    """The sandbox's home for one working folder: caches stay between its commands, and nothing that one project's
    commands leave there (a copied file, a ~/.npmrc or ~/.cargo/config.toml that changes builds) reaches another."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", root.name)[:40] or "root"
    d = auth.STATE_DIR / "sandbox-home" / f"{name}-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@functools.cache
def _git_identity() -> str:
    """The person's name and email for commits in the sandbox (and nothing else of their git config)."""
    lines = []
    for key in ("name", "email"):
        try:
            v = subprocess.run(["git", "config", "--global", "--get", f"user.{key}"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            v = ""
        if v and "\n" not in v:
            lines.append(f"\t{key} = {v}")
    return "[user]\n" + "\n".join(lines) + "\n" if lines else ""


def _file(name: str, content: str) -> Path:
    f = auth.STATE_DIR / name
    if not f.exists() or f.read_text() != content:
        f.write_text(content)
    return f


def _machine_id() -> Path:
    """The sandbox's own machine ID: the computer's is a permanent identifier, and what a command prints goes
    to the model's provider."""
    f = auth.STATE_DIR / "sandbox-machine-id"
    if not f.exists():
        f.write_text(secrets.token_hex(16) + "\n")
    return f


def _inside(p: Path, root: Path) -> bool:
    return p == root or p.is_relative_to(root)


def argv(root: Path, command: list[str], network: bool = False) -> list[str]:
    """The command line that runs ``command`` in a sandbox around the (resolved) working folder ``root``."""
    # --uid/--gid: the person's own ids inside, also under pasta (whose namespace makes them root)
    a = ["bwrap", "--unshare-all", *(["--share-net"] if network else []), "--die-with-parent", "--new-session",
         "--clearenv", "--uid", str(os.getuid()), "--gid", str(os.getgid())]
    mounted = []                       # what is visible: for PATH
    for d in SYSTEM:
        if os.path.isdir(d):
            a += ["--ro-bind", d, d]
            mounted.append(Path(d))
    for name in MERGED:
        p = Path("/" + name)
        if p.is_symlink():
            a += ["--symlink", os.readlink(p), str(p)]
        elif p.is_dir():
            a += ["--ro-bind", str(p), str(p)]
            mounted.append(p)
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/var", "--tmpfs", "/var/tmp", "--tmpfs", "/run"]
    a += ["--hostname", "sandbox"]                                      # not this computer's name and ID
    for target, source in (("/etc/hostname", lambda: _file("sandbox-hostname", "sandbox\n")), ("/etc/machine-id", _machine_id)):
        if os.path.exists(target):                     # /etc is read-only: a missing file can't become a mount point
            a += ["--ro-bind", str(source()), target]
    a += ["--bind", str(_home(root)), str(HOME)]
    for t in TOOLCHAINS:
        p = HOME / t
        if p.is_dir() and not p.is_symlink():
            a += ["--ro-bind", str(p), str(p)]
            mounted.append(p)
    if git := _git_identity():
        a += ["--ro-bind", str(_file("sandbox-gitconfig", git)), str(HOME / ".gitconfig")]
    a += ["--bind", str(root), str(root)]
    git_dir = root / ".git"
    if git_dir.is_dir() and not git_dir.is_symlink():
        (git_dir / "hooks").mkdir(exist_ok=True)
        a += ["--ro-bind", str(git_dir / "hooks"), str(git_dir / "hooks")]
        if (git_dir / "config").is_file():
            a += ["--ro-bind", str(git_dir / "config"), str(git_dir / "config")]
    for p in protected():
        if p.exists() and _inside(p, root):           # the working folder contains it: hide it again
            a += ["--tmpfs", str(p)] if p.is_dir() else ["--ro-bind", "/dev/null", str(p)]
    if network:
        conf = _file("sandbox-resolv.conf", f"nameserver {DNS_IN}\n")
        target = os.path.realpath("/etc/resolv.conf")        # often a link into /run (systemd-resolved)
        a += ["--ro-bind", str(conf), target if target.startswith("/run/") else "/etc/resolv.conf"]
    path = [p for p in os.environ.get("PATH", "").split(":")
            if p.startswith("/") and any(_inside(Path(p), m) for m in mounted)]
    for t in (".local/bin", ".cargo/bin", "go/bin", ".bun/bin", ".deno/bin", ".volta/bin"):
        if (HOME / t).is_dir() and str(HOME / t) not in path:
            path.append(str(HOME / t))
    path += [p for p in ("/usr/local/bin", "/usr/bin", "/bin") if p not in path]
    env = {"HOME": str(HOME), "PATH": ":".join(path), "TERM": "dumb", "TMPDIR": "/tmp", "SHELL": "/bin/bash",
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
           "LANG": os.environ.get("LANG") or "C.UTF-8"}
    for k, v in env.items():
        if v:
            a += ["--setenv", k, v]
    # bubblewrap's / is a writable in-memory folder: read-only too, once every mount point in it exists
    a += ["--remount-ro", "/", "--chdir", str(root), "--", *command]
    if network:
        a = [*_pasta(), "--", *a]
    return a


# ---- a git repository the model made (or replaced) during a command -----------------------------------
GIT_OK = re.compile(r"(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks)"
                    r"|user\.(name|email)|init\.defaultbranch|extensions\.(objectformat|refstorage)"
                    r"|remote\.[^\n]+\.(url|fetch)|branch\.[^\n]+\.(remote|merge))\Z")
DEFAULT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"


def git_id(root: Path) -> tuple | None:
    """Which .git the folder has right now (it was protected only if it already existed)."""
    try:
        st = (root / ".git").lstat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def clean_new_repo(root: Path) -> list[str]:
    """A .git made during a sandboxed command could carry hooks or config that the person's own git runs
    later, outside the sandbox: those are removed.  Returns what was removed, for the model."""
    git_dir, removed = root / ".git", []
    if git_dir.is_symlink():
        git_dir.unlink()
        return [".git (a link)"]
    if not git_dir.is_dir():
        return []
    hooks = git_dir / "hooks"
    if hooks.is_symlink() or (hooks.exists() and not hooks.is_dir()):
        hooks.unlink()
        removed.append(".git/hooks")
    elif hooks.is_dir():
        for f in hooks.iterdir():
            if not f.name.endswith(".sample") or f.is_symlink() or f.is_dir():
                shutil.rmtree(f) if f.is_dir() and not f.is_symlink() else f.unlink()
                removed.append(f".git/hooks/{f.name}")
    cfg = git_dir / "config"
    if cfg.is_symlink():
        cfg.unlink()
        cfg.write_text(DEFAULT_CONFIG)
        removed.append(".git/config (a link)")
    elif cfg.is_file():
        try:
            r = subprocess.run(["git", "config", "--file", str(cfg), "--name-only", "--list"],
                               capture_output=True, text=True, timeout=10)
            keys = sorted({k for k in r.stdout.splitlines() if not GIT_OK.match(k.lower())}) if r.returncode == 0 else None
            for key in keys or []:
                if subprocess.run(["git", "config", "--file", str(cfg), "--unset-all", key],
                                  capture_output=True, timeout=10).returncode:
                    keys = None
                    break
                removed.append(f"git config {key}")
        except (OSError, subprocess.SubprocessError):
            keys = None
        if keys is None:                               # unreadable, or no git here to read it with
            cfg.write_text(DEFAULT_CONFIG)
            removed.append(".git/config (replaced by a plain one)")
    return removed
