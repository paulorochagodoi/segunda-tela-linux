#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -U pip
"$ROOT/.venv/bin/pip" install -e "$ROOT"
mkdir -p "$HOME/.local/bin"
ln -sfn "$ROOT/.venv/bin/segunda-tela" "$HOME/.local/bin/segunda-tela"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/segunda-tela-linux.desktop" <<DESK
[Desktop Entry]
Name=Segunda Tela para Linux
Comment=Unofficial Miracast/WFD second screen source
Exec=$HOME/.local/bin/segunda-tela --ui
Terminal=false
Type=Application
Categories=Utility;Network;
Keywords=miracast;second;screen;tablet;
DESK
echo "Installed. Run: segunda-tela   or   segunda-tela --ui"

echo "Opcional USB: sudo $ROOT/scripts/install-udev-samsung.sh"
