#!/usr/bin/env bash
# Dependencies for Pop!_OS 22.04 (GNOME) and 24.04 (COSMIC). Run with sudo.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-pip python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-pipewire \
  pipewire pipewire-pulse wireplumber \
  network-manager \
  build-essential dkms linux-headers-$(uname -r) || true
echo "Base deps installed. Optional real virtual monitor: scripts/install-evdi.sh"
