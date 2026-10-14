#!/usr/bin/env bash
# Instala Segunda Tela com venv que enxerga o PyGObject do sistema (gi / GTK4).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

need_apt() {
  python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null
}

if ! need_apt; then
  echo "Pacotes GTK/PyGObject do sistema ausentes ou incompletos."
  echo "Instalando com apt (vai pedir sudo)…"
  sudo "$ROOT/scripts/install-deps-popos.sh"
fi

# Recria o venv com acesso aos site-packages do sistema (onde mora o python3-gi).
if [[ -d "$ROOT/.venv" ]]; then
  if ! grep -q '^include-system-site-packages = true' "$ROOT/.venv/pyvenv.cfg" 2>/dev/null; then
    echo "Recriando .venv com --system-site-packages (necessário para import gi)…"
    rm -rf "$ROOT/.venv"
  fi
fi
python3 -m venv --system-site-packages "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -U pip
# --usb-vnc: numpy packs RGBx; Pillow encodes Tight JPEG. simplejpeg is faster
# when the wheel installs; Pillow covers the same path if it does not.
"$ROOT/.venv/bin/pip" install -e "$ROOT" "numpy>=1.24" "pillow>=10"
if ! "$ROOT/.venv/bin/pip" install "simplejpeg>=1.6"; then
  echo "AVISO: simplejpeg ausente; o JPEG do --usb-vnc usa Pillow."
fi

if ! "$ROOT/.venv/bin/python" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null; then
  cat <<'MSG'
ERRO: ainda sem gi/GTK4 dentro do venv.

No Pop!_OS / Ubuntu rode:

  sudo apt update
  sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire

  cd /caminho/para/segunda-tela-linux
  rm -rf .venv
  ./scripts/install.sh

MSG
  exit 1
fi

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
Keywords=miracast;second;screen;tablet;wifi;direct;extend;
DESK

echo "Instalado. Rode: segunda-tela --ui   ou   segunda-tela list"
echo "Estender (monitor virtual): sudo $ROOT/scripts/setup-evdi.sh  (uma vez; ver docs/COSMIC.md)"
echo "Diagnóstico: segunda-tela doctor"
echo "Opcional USB: sudo $ROOT/scripts/install-udev-samsung.sh"
echo "Cabo VNC (tablet como tela estendida): segunda-tela --usb-vnc   (docs/CABO-VNC.md)"
# sanity
"$ROOT/.venv/bin/segunda-tela" --version || true
