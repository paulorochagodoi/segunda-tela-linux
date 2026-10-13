#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RULE="$ROOT/packaging/udev/99-segunda-tela-samsung.rules"
if [[ ${EUID:-} -ne 0 ]]; then
  echo "Reexecutando com sudo…"
  exec sudo "$0" "$@"
fi
install -m 0644 "$RULE" /etc/udev/rules.d/99-segunda-tela-samsung.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04e8 || true
echo "Regra udev instalada. Reconecte o cabo USB do tablet."
