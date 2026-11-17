#!/usr/bin/env bash
# Install EDU-KI Code for this user: set up what start.sh needs (.venv and requirements, or the Nix build)
# and add the app to the application menu with its icon, so it starts like any other app. It runs from
# this folder; updates come later through the app itself (profile card).
#   ./install.sh               install, or repair after the folder moved
#   ./install.sh --uninstall   remove the menu entry and the icon (this folder and your data stay)
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

ID=de.edu_ki_rlp.code      # the window's application id: the desktop matches window and menu entry by it
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
ENTRY=$DATA/applications/$ID.desktop
ICON=$DATA/icons/hicolor/scalable/apps/$ID.svg

refresh() {
    if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$DATA/applications" || true; fi
    # only an existing cache is updated: a new one that nobody keeps current would hide icons installed later
    if [[ -f $DATA/icons/hicolor/icon-theme.cache ]] && command -v gtk-update-icon-cache >/dev/null; then
        gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
    fi
}

case ${1:-} in
    --uninstall)
        rm -f "$ENTRY" "$ICON"
        refresh
        echo "EDU-KI Code ist nicht mehr im App-Menü. Dieser Ordner ($PWD) und deine Daten bleiben."
        exit 0 ;;
    "") ;;
    *)  echo "Aufruf: ./install.sh [--uninstall]" >&2
        exit 2 ;;
esac

# The menu entry starts this folder's start.sh; these characters would need escaping in it.
case $PWD in
    *[\\\"\`\$%]* | *$'\n'*)
        echo "Der Ordnerpfad enthält \\ \" \` \$ oder %: bitte den Ordner umbenennen oder verschieben." >&2
        exit 1 ;;
esac

echo "Richte die App ein …"
EDU_KI_SETUP_ONLY=1 ./start.sh

install -Dm644 edu_ki_proxy/static/icon.svg "$ICON"
mkdir -p "$(dirname "$ENTRY")"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=EDU-KI Code
GenericName=KI-Chat und Coding-Agent
Comment=Chat mit KI-Modellen und ein Agent, der in Projektordnern arbeitet
Exec="$PWD/start.sh"
Icon=$ID
Terminal=false
Categories=Development;
Keywords=KI;AI;Chat;Code;EDU-KI;Agent;
StartupNotify=true
StartupWMClass=$ID
EOF
refresh
echo "Fertig: „EDU-KI Code“ steht im App-Menü."
