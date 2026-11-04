#!/usr/bin/env bash
# Start EDU-KI Code. Sets up .venv on the first run (or repairs it) and reinstalls the requirements
# whenever requirements.txt or the Python version changes.
# Arguments are passed on to `python -m edu_ki_proxy app`, e.g. ./start.sh --debug or --port 8788.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"   # works through a symlink, e.g. in ~/.local/bin

PYTHON=${PYTHON:-python3}
VENV=.venv
PY=$VENV/bin/python
STAMP=$VENV/.requirements-stamp

if ! command -v "$PYTHON" >/dev/null; then
    echo "Python 3 fehlt (Fedora: sudo dnf install python3)." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 oder neuer nötig, gefunden: $("$PYTHON" --version 2>&1)" >&2
    exit 1
fi

# The venv needs pip and the system site-packages: PyGObject for the GTK window comes from the system.
# `venv --upgrade` repairs an existing one in place (missing pip, wrong flag, upgraded Python)
# without dropping the packages already installed.
if [[ ! -d $VENV ]]; then
    echo "Richte $VENV ein …"
    "$PYTHON" -m venv --system-site-packages "$VENV"
elif ! "$PY" -c 'import pip' 2>/dev/null \
        || ! grep -qs '^include-system-site-packages = true' "$VENV/pyvenv.cfg"; then
    echo "Repariere $VENV …"
    "$PYTHON" -m venv --system-site-packages --upgrade "$VENV"
fi

want="$(sha256sum requirements.txt | cut -d' ' -f1) $("$PY" -c 'import platform; print(platform.python_version())')"
if [[ ! -f $STAMP || "$(<"$STAMP")" != "$want" ]]; then
    echo "Installiere Abhängigkeiten …"
    "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
    printf '%s\n' "$want" > "$STAMP"
fi

exec "$PY" -m edu_ki_proxy app "$@"
