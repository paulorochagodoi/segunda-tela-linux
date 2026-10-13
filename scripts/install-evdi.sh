#!/usr/bin/env bash
# Mantido por compatibilidade: use scripts/setup-evdi.sh (pacotes da distro).
# Só se evdi-dkms não existir na sua distro, compile do upstream (GPL-2.0 / LGPL-2.1):
#   git clone https://github.com/DisplayLink/evdi && cd evdi && sudo make install_dkms
#   (library/: make && sudo make install  → libevdi.so.1)
exec "$(dirname "$0")/setup-evdi.sh" "$@"
