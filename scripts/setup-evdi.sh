#!/usr/bin/env bash
# Prepara o EVDI (DisplayLink, open source) para o modo "estender":
#  - instala evdi-dkms + libevdi1 da distro; se o pacote não compilar para o
#    kernel em uso (ex.: evdi-dkms 1.14.2 no 7.1.x: "linux-headers not supported"),
#    baixa o último release de github.com/DisplayLink/evdi, instala o módulo via
#    DKMS (dkms add/build/install) e a libevdi do mesmo release em /usr/local/lib
#  - explica o cadastro da chave MOK quando o Secure Boot está ativo
#  - carrega o módulo com 1 dispositivo genérico, também em todo boot
#  - regra udev "uaccess" para o usuário da sessão abrir /dev/dri/cardN do evdi
#
# Variáveis:
#   EVDI_FROM_SOURCE=1   pula o pacote da distro e vai direto ao GitHub
#   EVDI_REF=v1.15.1     tag (ou "main") do GitHub em vez do último release
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then exec sudo --preserve-env=EVDI_FROM_SOURCE,EVDI_REF "$0" "$@"; fi
export DEBIAN_FRONTEND=noninteractive

KVER="$(uname -r)"
REPO="DisplayLink/evdi"
WORK=""
trap '[[ -n "$WORK" ]] && rm -rf "$WORK"' EXIT

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }

module_ok() { modinfo -k "$KVER" evdi >/dev/null 2>&1; }
lib_ok()    { ldconfig -p | grep -q 'libevdi\.so\.1'; }

# --------------------------------------------------------------- Secure Boot
secure_boot_on() {
  command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'
}

mok_cert() {
  local c
  c="$(sed -n 's/^[[:space:]]*mok_certificate=["]\{0,1\}\([^"]*\)["]\{0,1\}.*/\1/p' \
        /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf 2>/dev/null | tail -n1)"
  for f in "$c" /var/lib/shim-signed/mok/MOK.der /var/lib/dkms/mok.pub; do
    [[ -n "$f" && -f "$f" ]] && { echo "$f"; return 0; }
  done
  return 1
}

explain_mok() {
  secure_boot_on || return 0
  local cert
  if ! cert="$(mok_cert)"; then
    warn "Secure Boot ativo, mas não achei a chave MOK do DKMS (/var/lib/shim-signed/mok/MOK.der ou /var/lib/dkms/mok.pub)."
    warn "Rode de novo depois de um 'dkms build' bem-sucedido; o DKMS gera a chave ao assinar o módulo."
    return 0
  fi
  if mokutil --test-key "$cert" 2>&1 | grep -qi 'already enrolled'; then
    echo "Secure Boot: a chave do DKMS ($cert) já está cadastrada (MOK)."
    return 0
  fi
  cat <<EOF

------------------------------------------------------------------------------
 SECURE BOOT ATIVO: o kernel só carrega o módulo evdi assinado por uma chave
 cadastrada no firmware (MOK). O DKMS assinou com: $cert
 Essa chave ainda NÃO está cadastrada. Passos (uma vez só):

   1. sudo mokutil --import $cert
        → crie uma senha temporária (vai pedir de novo no boot)
   2. Reinicie. Na tela azul "Perform MOK management" (shim/MokManager):
        Enroll MOK → Continue → Yes → digite a senha → Reboot
      (se passar direto sem tela azul, repita o passo 1 e reinicie de novo)
   3. Depois do boot:  sudo modprobe evdi && segunda-tela doctor

 Sem isso, o modprobe falha com "Key was rejected by service".
 English: Secure Boot is on — import the DKMS key with 'mokutil --import',
 reboot, choose "Enroll MOK" in the blue MokManager screen, enter the password.
------------------------------------------------------------------------------
EOF
  if [[ -t 0 ]]; then
    read -r -p "Rodar 'mokutil --import $cert' agora? [s/N] " ans || ans=""
    if [[ "$ans" =~ ^[sSyY] ]]; then
      mokutil --import "$cert" && echo "OK — agora reinicie e cadastre a chave (Enroll MOK)."
    fi
  fi
}

# --------------------------------------------------------------- distro package
install_distro() {
  say "Tentando o pacote da distro (evdi-dkms + libevdi1) para o kernel $KVER"
  apt-get update || true
  apt-get install -y dkms build-essential "linux-headers-$KVER" || return 1
  apt-get install -y evdi-dkms libevdi1 || return 1
  module_ok
}

remove_broken_distro_dkms() {
  # Um postinst do evdi-dkms que falhou deixa o dpkg meio configurado e trava o apt.
  if dpkg -s evdi-dkms >/dev/null 2>&1; then
    say "Removendo o evdi-dkms da distro (não compila para $KVER)"
    apt-get remove --purge -y evdi-dkms \
      || dpkg --remove --force-remove-reinstreq evdi-dkms \
      || warn "não consegui remover evdi-dkms; veja 'sudo dpkg --configure -a'"
  fi
}

# --------------------------------------------------------------- upstream (GitHub)
latest_tag() {
  curl -fsSL --retry 3 "https://api.github.com/repos/$REPO/releases/latest" \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
}

install_upstream_ref() {
  local ref="$1" url ver src
  if [[ "$ref" == "main" ]]; then
    url="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
  else
    url="https://github.com/$REPO/archive/refs/tags/$ref.tar.gz"
  fi
  say "Baixando evdi $ref ($url)"
  [[ -n "$WORK" ]] && rm -rf "$WORK"
  WORK="$(mktemp -d)"
  src="$WORK/evdi"
  mkdir -p "$src" || return 1
  curl -fL --retry 3 -o "$WORK/evdi.tar.gz" "$url" || return 1
  tar -xzf "$WORK/evdi.tar.gz" -C "$src" --strip-components=1 || return 1
  [[ -f "$src/module/dkms.conf" && -d "$src/library" ]] || { warn "tarball inesperado (sem module/dkms.conf)"; return 1; }

  ver="$(sed -n 's/^PACKAGE_VERSION=["]\{0,1\}\([^"]*\)["]\{0,1\}/\1/p' "$src/module/dkms.conf" | head -n1)"
  [[ -n "$ver" ]] || ver="${ref#v}"
  [[ "$ref" == "main" ]] && ver="$ver.git$(date +%Y%m%d)"

  say "DKMS: evdi/$ver para $KVER"
  dkms remove -m evdi -v "$ver" --all >/dev/null 2>&1 || true
  rm -rf "/usr/src/evdi-$ver"
  cp -a "$src/module" "/usr/src/evdi-$ver" || return 1
  sed -i "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=$ver/" "/usr/src/evdi-$ver/dkms.conf" || return 1
  dkms add -m evdi -v "$ver" || return 1
  if ! dkms build -m evdi -v "$ver" -k "$KVER"; then
    warn "dkms build falhou para evdi/$ver. Fim do log:"
    tail -n 30 "/var/lib/dkms/evdi/$ver/build/make.log" 2>/dev/null >&2 || true
    dkms remove -m evdi -v "$ver" --all >/dev/null 2>&1 || true
    rm -rf "/usr/src/evdi-$ver"
    return 1
  fi
  dkms install -m evdi -v "$ver" -k "$KVER" --force || return 1

  say "libevdi $ver (mesmo release do módulo) → /usr/local/lib"
  make -C "$src/library" || return 1
  make -C "$src/library" install PREFIX=/usr/local || return 1
  # A libevdi1 da distro (1.14.x) não deve ser carregada junto do módulo novo.
  if dpkg -s libevdi1 >/dev/null 2>&1; then
    dpkg -r libevdi1 2>/dev/null || warn "libevdi1 da distro mantida (outro pacote depende dela); /usr/local/lib tem prioridade"
  fi
  ldconfig
  module_ok
}

install_upstream() {
  say "Instalando o evdi do GitHub ($REPO) via DKMS"
  apt-get install -y build-essential dkms "linux-headers-$KVER" libdrm-dev pkg-config curl ca-certificates || {
    warn "faltam pacotes de build (linux-headers-$KVER existe? 'apt-cache policy linux-headers-$KVER')"
    return 1
  }
  local ref="${EVDI_REF:-}"
  if [[ -z "$ref" ]]; then
    ref="$(latest_tag)" || ref=""
    [[ -n "$ref" ]] || { warn "não consegui consultar o último release; usando main"; ref="main"; }
  fi
  install_upstream_ref "$ref" && return 0
  if [[ "$ref" != "main" ]]; then
    warn "evdi $ref não compilou para $KVER; tentando o branch main (suporte a kernels novos chega lá antes)"
    install_upstream_ref main && return 0
  fi
  return 1
}

# --------------------------------------------------------------- main
if module_ok && lib_ok && [[ -z "${EVDI_FROM_SOURCE:-}" ]]; then
  echo "evdi já disponível para $KVER: $(modinfo -k "$KVER" -F version evdi 2>/dev/null || echo '?')"
elif [[ -z "${EVDI_FROM_SOURCE:-}" ]] && install_distro && lib_ok; then
  echo "evdi da distro OK para $KVER"
else
  [[ -z "${EVDI_FROM_SOURCE:-}" ]] && warn "evdi-dkms da distro não serve para o kernel $KVER"
  remove_broken_distro_dkms
  if ! install_upstream; then
    warn "Falha ao instalar o evdi do GitHub para $KVER."
    warn "Veja /var/lib/dkms/evdi/*/build/make.log e https://github.com/$REPO/issues"
    exit 1
  fi
  echo "evdi $(modinfo -k "$KVER" -F version evdi 2>/dev/null || echo '?') instalado via DKMS (GitHub)"
fi

explain_mok

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
  if ! err="$(modprobe evdi initial_device_count=1 2>&1)"; then
    warn "Falha no modprobe evdi: $err"
    if secure_boot_on || grep -qi 'key was rejected\|required key' <<<"$err"; then
      warn "Secure Boot: o módulo assinado pelo DKMS só carrega depois de cadastrar a chave MOK (passos acima)."
      warn "Depois do reboot + Enroll MOK, rode este script de novo."
    fi
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
