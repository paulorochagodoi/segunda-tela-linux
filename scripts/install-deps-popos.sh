#!/usr/bin/env bash
# Dependências Pop!_OS 22.04 (GNOME) / 24.04 (COSMIC) e Ubuntu equivalente.
# Rode com sudo (ou o install.sh chama isto quando faltar o gi).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-pip python3-venv python3-gi python3-gi-cairo gir1.2-glib-2.0 \
  gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-pipewire gstreamer1.0-pulseaudio \
  pipewire pipewire-pulse wireplumber \
  xdg-desktop-portal \
  network-manager wpasupplicant \
  android-tools-adb \
  python3-numpy \
  python3-pil

# Encoder H.264 por hardware (AMD/Intel): `vah264enc` vem do plugins-bad (plugin va);
# `vaapih264enc` do gstreamer1.0-vaapi (legado). mesa-va-drivers = VA-API do amdgpu.
apt-get install -y mesa-va-drivers vainfo || true
apt-get install -y gstreamer1.0-vaapi || true

# Portal do COSMIC (Pop!_OS 24.04). Em GNOME é o xdg-desktop-portal-gnome.
apt-get install -y xdg-desktop-portal-cosmic 2>/dev/null || apt-get install -y xdg-desktop-portal-gnome 2>/dev/null || true
# Layout das telas (verifica se a "Segunda Tela" está estendida): cosmic-randr; wlr-randr em outros compositores.
apt-get install -y cosmic-randr 2>/dev/null || apt-get install -y wlr-randr 2>/dev/null || true
# doctor --usb-vnc: wayland-info mostra se o compositor tem ext-image-copy-capture (o wayvnc do noble não usa).
apt-get install -y wayland-utils || true

# EVDI (monitor virtual para o modo estender): módulo DKMS + libevdi.
# Não falha a instalação base se o kernel/headers não casarem — ver scripts/setup-evdi.sh.
apt-get install -y build-essential dkms "linux-headers-$(uname -r)" || true
apt-get install -y evdi-dkms libevdi1 || echo "AVISO: evdi-dkms/libevdi1 indisponíveis — ver docs/COSMIC.md"

# --usb-vnc: /dev/uinput para o toque, só na saída EVDI. uaccess = usuário da sessão.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
install -m 0644 "$ROOT/packaging/udev/70-segunda-tela-uinput.rules" \
  /etc/udev/rules.d/70-segunda-tela-uinput.rules
udevadm control --reload-rules || true
modprobe uinput || true
udevadm trigger --name-match=uinput || true
echo "Regra udev do uinput instalada. Se o toque do --usb-vnc falhar, saia e entre de novo na sessão."

echo "Dependências OK (python3-gi + GTK4/libadwaita + GStreamer + VA-API + portal + EVDI + adb)."
echo "VNC do cabo (--usb-vnc): numpy empacota pixels; Pillow ou simplejpeg faz Tight JPEG (o install.sh instala os dois no .venv)."
echo "Depois:"
echo "  sudo ./scripts/setup-evdi.sh   # carrega evdi com 1 dispositivo + regra udev"
echo "  ./scripts/install.sh           # cria venv com --system-site-packages"
