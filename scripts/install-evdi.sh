#!/usr/bin/env bash
# Optional: DisplayLink's open EVDI kernel module (DKMS) for a real virtual DRM connector.
# https://github.com/DisplayLink/evdi — GPL-2.0
set -euo pipefail
if [[ ${EUID:-} -ne 0 ]]; then echo "Run as root"; exit 1; fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git clone --depth 1 https://github.com/DisplayLink/evdi.git "$TMP/evdi"
make -C "$TMP/evdi" 
# Module install paths vary; prefer distro package when available:
if apt-cache show evdi-dkms >/dev/null 2>&1; then
  apt-get install -y evdi-dkms
  echo "Installed evdi-dkms from apt"
else
  echo "Build EVDI from $TMP/evdi following upstream README (dkms install)."
  echo "After load: modprobe evdi initial_device_count=1"
fi
