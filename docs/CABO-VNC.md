# Cabo USB com VNC — estender a área de trabalho no Galaxy Tab

Modo `segunda-tela --usb-vnc`. O PC cria o monitor virtual **Segunda Tela** (EVDI,
à direita do eDP-1) e serve **só essa saída** por VNC. O tablet abre o bVNC em
`127.0.0.1:5900` através de `adb reverse` — sem tethering e sem Wi-Fi Display.

Alvo: Pop!_OS 24.04, COSMIC (Wayland), GPU AMD, módulo `evdi` carregado,
Galaxy Tab S10 FE no cabo USB.

> Projeto independente. Não é afiliado à Samsung. O app da Play Store usado no
> tablet é o **bVNC** (Iordan Iordanov), pacote `com.iiordanov.freebVNC`.

## Por que não é o wayvnc

O `wayvnc` do Ubuntu 24.04 / Pop!_OS 24.04 é o **0.7.2**. Ele só captura com
`wlr-screencopy-unstable-v1`. O cosmic-comp **não implementa** esse protocolo
([cosmic-comp#560](https://github.com/pop-os/cosmic-comp/issues/560)).

O compositor implementa `ext-image-copy-capture-v1` (desde o COSMIC
1.0.0~beta.8, [cosmic-comp#1280](https://github.com/pop-os/cosmic-comp/pull/1280)).
O wayvnc só aprendeu esse protocolo na versão **0.9**, que não está no apt do
noble (está no Ubuntu 25.10+). Com o 0.9 daria para escolher a saída
(`wayvnc --output DVI-I-1`), mas o ponteiro do wayvnc exige
`wlr-virtual-pointer`, que o cosmic-comp também não tem — a captura lá só
funciona com `--disable-input`. Sem os eventos de ponteiro não há como injetar
o toque num dispositivo uinput mapeado só na saída EVDI.

Por isso o `--usb-vnc` lê o framebuffer do EVDI (a mesma captura direta do modo
estender, formato RGBx) e fala RFB ele mesmo: só os retângulos que o
`evdi_grab_pixels` devolveu, Tight (JPEG ~70 em região de foto, zlib no resto)
ou ZRLE, no máximo ~30 fps na saída, senha VNC clássica.

## Uma vez no PC

```bash
sudo ./scripts/install-deps-popos.sh   # adb, evdi, regra udev do /dev/uinput
sudo ./scripts/setup-evdi.sh           # módulo evdi + permissão do /dev/dri
./scripts/install.sh
segunda-tela doctor                    # seção "-- USB VNC (cabo) --"
```

Saia e entre de novo na sessão se o doctor disser que `/dev/uinput` não tem
permissão de escrita. A regra `70-segunda-tela-uinput.rules` usa `uaccess`
(o usuário da sessão, sem root).

## Uma vez no tablet

1. **Depuração USB.** Configurações → Sobre o tablet → Informações do software
   → toque 7 vezes em "Número da compilação". Volte em Opções do desenvolvedor
   e ative **Depuração USB**.
2. Ligue o cabo. No aviso "Permitir depuração USB?", marque confiar neste
   computador.
3. Instale o **bVNC: Secure VNC Viewer** pela Play Store
   (pacote `com.iiordanov.freebVNC`). O app pago "bVNC Pro" é outro pacote; o
   comando abre o gratuito.

Não é preciso ativar o Tethering USB. Esse caminho só entra se o `adb reverse`
falhar.

## Estender

```bash
segunda-tela --usb-vnc
# opcional: segunda-tela --usb-vnc --width 1920 --height 1200 --fps 60
# não abrir o bVNC sozinho: --no-usb-launch
```

O que o comando faz, nesta ordem:

1. Sobe o monitor virtual EVDI "Segunda Tela" e, se ele não estiver já à
   direita do eDP-1, roda `cosmic-randr position <saída> <x> <y>`.
2. `adb reverse tcp:5900 tcp:5900`. O servidor escuta **só** em `127.0.0.1:5900`.
3. Gera uma senha VNC de 8 caracteres e imprime `Senha VNC: …` no terminal
   (não vai para o arquivo de log).
4. Cria um touchscreen uinput ("Segunda Tela VNC") do tamanho dessa saída e
   grava o mapa do COSMIC para que o toque fique **só** nela
   (`~/.config/cosmic/com.system76.CosmicComp/v1/input_devices`).
5. Se o adb está autorizado e `pm list packages` mostra
   `com.iiordanov.freebVNC`, abre o bVNC **pelo componente**
   `com.iiordanov.freebVNC/com.iiordanov.bVNC.RemoteCanvasActivity`
   (`am start -n … -d vnc://127.0.0.1:5900?VncPassword=…`).
   Um `vnc://` sem componente no One UI abriu o histórico de alertas de
   emergência. Se esse componente recusar o URI, o comando ainda faz
   `monkey -p com.iiordanov.freebVNC` e imprime endereço, porta e senha.

Arraste uma janela para a direita do eDP-1: ela aparece no tablet. O dedo no
bVNC toca essa tela, não o painel interno.

### Se o bVNC não abrir sozinho

No tablet, abra o bVNC, nova conexão:

| Campo | Valor |
|-------|--------|
| Endereço | `127.0.0.1` (ou o IP que o comando imprimir, no fallback de tether) |
| Porta | `5900` |
| Senha | a linha `Senha VNC:` |
| Segurança | VNC (senha clássica), cor 24-bit |

### Fallback: tethering

Se o `adb reverse` não ficar pronto (adb ausente, não autorizado, ou o reverse
falha), o comando espera uma interface `usb0`/`enx*` com IPv4. Aí:

- no tablet: Conexões → Hotspot e tethering → **Tethering USB**;
- o servidor escuta em `127.0.0.1` **e** no IP do PC nessa rede (nunca em
  `0.0.0.0`);
- o bVNC no tablet liga no **IP do PC**, não no IP do tablet.

## O que o doctor verifica

`segunda-tela doctor`, seção `-- USB VNC (cabo) --`:

- por que o wayvnc não é usado (versão e, se houver sessão Wayland, os
  protocolos que o `wayland-info` viu);
- `/dev/uinput` gravável;
- porta `5900` livre em `127.0.0.1`;
- lembrete do pacote bVNC.

A seção `-- USB --` continua valendo para o adb (autorização, cabo Samsung).

## Limites conhecidos

- Isto **não foi executado** num Tab S10 FE ligado a este repositório. Os
  testes cobrem o RFB (senha, Tight, ZRLE, dano), o túnel decidido em código
  e o relatório do toque. O EVDI, o cosmic-randr e o adb reais ficam na máquina
  do Paulo.
- O grab do EVDI é RGBx (fourcc AB24). O bVNC pede shifts 16,8,0. Raw e ZRLE
  seguem isso (byte 0 = azul). O Tight do bVNC **não**: `Decoder.handleTightRect`
  pinta o byte 0 como vermelho. O servidor manda R,G,B no Tight por causa disso.
  O byte de controle do Tight tem o tipo no nibble alto (preenchimento `0x80`,
  JPEG `0x90`, zlib com reset do stream 0 em `0x01`) — é assim que o bVNC
  desloca os 4 bits de reset antes de ler o subtipo.
- Cada cliente VNC tem uma thread só para ler. O `sendall` de um quadro no adb
  bloqueia; o ponteiro é lido e injetado no uinput nessa outra thread. O log
  mostra `VNC pointer` (mudança de botão na hora, movimento algumas vezes por
  segundo) e a linha `VNC fps` conta todos.
- A saída VNC fica em ~30 fps mesmo com `--fps 60`. O grab do EVDI continua na
  taxa do monitor. O log `EVDI grab fps` inclui sync/async/reask, o tempo do
  `grab_pixels` e o último retângulo (quantidade e pixels). Se `last 1 rects`
  for a tela inteira, o kernel só entregou um clip — o JPEG é o que reduz o
  tamanho, não dá para inventar dano menor.
- Foto vai em JPEG qualidade ~70 (simplejpeg, senão turbojpeg, senão Pillow),
  com nova tentativa em 55/40/30 se passar de ~200 KB. Interface com poucas
  cores ou borda dura fica em zlib. Sem Pillow e sem simplejpeg o JPEG não
  existe e a região fotográfica volta para zlib (o `install.sh` instala os dois).
- O cursor do compositor não entra no grab do EVDI. O VNC desenha uma seta na
  posição do dedo. O toque em si vai para o uinput mapeado em `DVI-I-1`.
- Um pedido incremental sem pixels novos fica pendente até chegar dano. Uma
  resposta vazia faz o bVNC esperar o timer dele e a imagem parece parada.
- O `map_to_output` do cosmic-comp vale para touchscreen. Versões recentes
  releem `input_devices` sozinhas; se o toque cair no eDP-1, saia e entre na
  sessão ou confira o nome da saída com `cosmic-randr list` (algo como
  `DVI-I-1`, não "Segunda Tela").
- O teclado do tablet não é injetado. O botão esquerdo do VNC é o dedo; roda
  do mouse não.
- A senha aparece no terminal e na linha de comando do `adb shell` (o processo
  local). O log em `~/.local/state/segunda-tela/` grava essa chamada com a
  senha trocada por `********`.
- Se o COSMIC ainda não ativou o conector EVDI, o VNC fica preto até
  Configurações → Telas mostrar "Segunda Tela".

---

## English

`segunda-tela --usb-vnc` extends the desktop onto a Galaxy Tab over USB. It
reuses the EVDI virtual monitor, places it to the right of eDP-1, and serves
only that framebuffer over RFB (Tight and ZRLE, damage rectangles, classic VNC
password). Transport is `adb reverse tcp:5900 tcp:5900` bound to `127.0.0.1`;
USB tethering is the fallback and then the server also binds the PC's tether
address. Touch is a uinput touchscreen mapped to that output via COSMIC's
`input_devices` file. With adb authorized, bVNC Free is started with
`am start -n com.iiordanov.freebVNC/com.iiordanov.bVNC.RemoteCanvasActivity`
and `vnc://127.0.0.1:5900`.

wayvnc is not used: noble's 0.7.2 speaks only `wlr-screencopy`, which
cosmic-comp does not implement. bVNC is started with `am start -n` on
`com.iiordanov.freebVNC/com.iiordanov.bVNC.RemoteCanvasActivity` (a bare
`vnc://` VIEW intent opened Emergency alerts on One UI). Tight bytes are
R,G,B because bVNC's Tight decoder paints byte 0 as red; Raw and ZRLE follow
the C24bit shifts. Photo damage is Tight JPEG (control `0x90`, quality about
70); other damage is zlib. Only EVDI's dirty rectangles are sent, capped
around 30 fps, and pointer events are read on a thread that does not wait
on `sendall`. See the Portuguese sections for the procedure.
