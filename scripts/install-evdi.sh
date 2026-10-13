#!/usr/bin/env bash
# Mantido por compatibilidade: use scripts/setup-evdi.sh (pacote da distro; se
# não compilar no kernel em uso, ele instala o release do GitHub DisplayLink/evdi
# via DKMS + libevdi — GPL-2.0 / LGPL-2.1).
exec "$(dirname "$0")/setup-evdi.sh" "$@"
