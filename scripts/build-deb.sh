#!/usr/bin/env bash
# Nice-to-have: simple .deb via fpm if available, else sdist tip.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m pip install --upgrade build
python3 -m build
if command -v fpm >/dev/null; then
  fpm -s python -t deb "dist/segunda_tela_linux-"*.tar.gz
else
  echo "Wheel/sdist in dist/. Install fpm for .deb packaging."
fi
