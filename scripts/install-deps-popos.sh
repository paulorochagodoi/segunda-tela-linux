#!/usr/bin/env bash
# Dependências Pop!_OS 22.04 (GNOME) / 24.04 (COSMIC) e Ubuntu equivalente.
# Rode com sudo (ou o install.sh chama isto quando faltar o gi).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-pip python3-venv python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-pipewire \
  pipewire pipewire-pulse wireplumber \
  network-manager \
  android-tools-adb
# headers/dkms são opcionais (EVDI); não falham a instalação base
apt-get install -y build-essential dkms "linux-headers-$(uname -r)" || true
echo "Dependências base OK (inclui python3-gi + GTK4/libadwaita + GStreamer)."
echo "Depois: ./scripts/install.sh   # cria venv com --system-site-packages"
