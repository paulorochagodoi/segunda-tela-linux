#!/usr/bin/env bash
# Prepara o EVDI (DisplayLink, open source) para o modo "estender":
#  - instala evdi-dkms + libevdi1 (se faltarem)
#  - carrega o módulo com 1 dispositivo genérico, também em todo boot
#  - regra udev "uaccess" para o usuário da sessão abrir /dev/dri/cardN do evdi
# Não compila nem distribui módulo próprio.
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then exec sudo "$0" "$@"; fi

if ! modinfo evdi >/dev/null 2>&1 || ! ldconfig -p | grep -q libevdi; then
  apt-get update
  apt-get install -y dkms "linux-headers-$(uname -r)" evdi-dkms libevdi1
fi

cat > /etc/modprobe.d/segunda-tela-evdi.conf <<'CONF'
# Segunda Tela: um conector EVDI genérico disponível desde o boot
options evdi initial_device_count=1
CONF
echo evdi > /etc/modules-load.d/segunda-tela-evdi.conf

# 70-* para rodar antes do 73-seat-late.rules (que aplica o uaccess).
cat > /etc/udev/rules.d/70-segunda-tela-evdi.rules <<'RULES'
SUBSYSTEM=="drm", KERNEL=="card*", DRIVERS=="evdi", TAG+="uaccess"
RULES
udevadm control --reload-rules

if ! lsmod | grep -q '^evdi'; then
  if ! modprobe evdi initial_device_count=1; then
    echo "Falha no modprobe evdi. Com Secure Boot ativo, o módulo DKMS precisa ser assinado (MOK):"
    echo "  sudo mokutil --import /var/lib/dkms/mok.pub   # e reinicie"
    exit 1
  fi
fi
# Módulo já carregado sem dispositivos (ex.: initial_device_count=0)? adiciona um.
if ! ls -d /sys/bus/platform/devices/evdi* >/dev/null 2>&1; then
  echo 1 > /sys/devices/evdi/add || true
fi
udevadm trigger --subsystem-match=drm || true
sleep 1
for c in /sys/bus/platform/devices/evdi*/drm/card*; do
  [[ -e "$c" ]] && echo "EVDI pronto: /dev/dri/$(basename "$c")"
done
echo "Teste: segunda-tela doctor"
