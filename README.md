# Segunda Tela para Linux

**English below.**

Projeto **independente e não oficial** para usar um tablet (por exemplo Galaxy Tab)
como **segunda tela estendida** no **Pop!_OS** e outros desktops Linux, via
**Wi-Fi Display (Miracast) sobre Wi-Fi Direct** — source RTSP + H.264 em MPEG-TS/RTP.

> Não é afiliado à Samsung Electronics. Não redistribui o app da Microsoft Store
> nem marcas/ícones da Samsung. Veja [docs/FINDINGS.md](docs/FINDINGS.md) e [NOTICE](NOTICE).

## O que funciona / o que não

| Recurso | Status |
|---------|--------|
| Descoberta Wi-Fi Direct (NetworkManager D-Bus, WFD IEs) — `segunda-tela list` | Implementado · **não testado em hardware** |
| Conexão P2P ao tablet (`--device`) com WFD IEs de source | Implementado · **não testado em hardware** |
| Source WFD completo: M1→M7, keep-alive M16, negociação de formato | Implementado · testado em loopback contra sink simulado |
| Negociação: 1920x1200p60 (VESA) / 1080p60/30 (CEA), fallback 1080p30 | Implementado · testes unitários |
| Mídia WFD: H.264 em MPEG-TS (PID 0x1011) sobre RTP/MP2T, RTCP opcional | Implementado · validado codificando+decodificando localmente |
| Encoder VA-API (`vah264enc` / `vaapih264enc`), fallback `x264enc` | Implementado |
| **Estender**: monitor virtual EVDI com EDID próprio (1920x1200, 2560x1600, 1080p) | Implementado · **não testado em hardware** |
| Captura só do monitor virtual: portal ScreenCast (COSMIC) ou leitura direta do EVDI | Implementado · **não testado em hardware** |
| Espelhar (`--mode mirror`) via portal / X11 | Implementado |
| USB cabo: adb como túnel de portas (no lugar do Connlib) → *source ready* MS-MICE na :7250 do tablet, tablet conecta de volta na :7236; RTP pelo tether | Implementado · testado contra tablet simulado · **não testado em hardware** |
| USB cabo VNC (`--usb-vnc`): o mesmo monitor EVDI, só essa saída, RFB (Tight/ZRLE) via `adb reverse` :5900 e bVNC; toque por uinput nessa saída | Implementado · testado sem hardware · **não testado no Tab** |
| Pareamento MCF “Second Screen” one-tap da Samsung | **Não** — ver [docs/SAMSUNG_MCF.md](docs/SAMSUNG_MCF.md) |
| UIBC / toque / S Pen | Não implementado |

### Por que a versão 0.2 “não fazia nada” no Wi-Fi

1. **Nunca fazia P2P** — só escutava RTSP na LAN; o tablet não tinha como achar o PC.
2. **Esperava o tablet falar primeiro.** No WFD o *source* envia o M1
   (`OPTIONS * Require: org.wfa.wfd1.0`) logo após o TCP; um sink real fica
   esperando para sempre.
3. **Mandava `rtph264pay`.** Sinks Miracast esperam **MPEG-TS** em RTP (PT 33).

A 0.3 corrige os três.

## Requisitos

- Pop!_OS 24.04 (COSMIC) ou 22.04 (GNOME), ou Ubuntu equivalente
- Wi-Fi com P2P no NetworkManager (`nmcli device` mostra `p2p-dev-wlan0/wlo1`)
- GStreamer 1.0 (base/good/**bad**/ugly/libav/pipewire) + VA-API (`mesa-va-drivers`)
- `python3-gi` (Gio para NetworkManager e portal; GTK4/libadwaita para a UI)
- Para **estender**: `evdi-dkms` + `libevdi1` (pacotes da distro; se não
  compilarem no seu kernel, o `setup-evdi.sh` instala o release do GitHub via
  DKMS). Secure Boot: cadastre a chave MOK — ver [docs/COSMIC.md](docs/COSMIC.md#secure-boot-mok)

```bash
sudo ./scripts/install-deps-popos.sh
sudo ./scripts/setup-evdi.sh      # monitor virtual (uma vez)
./scripts/install.sh
segunda-tela doctor
```

Guia completo do Pop!_OS 24.04 / COSMIC: **[docs/COSMIC.md](docs/COSMIC.md)**.

## Uso

```bash
# No tablet: abra a recepção de tela sem fio e deixe esperando.
segunda-tela list                                    # tablets/sinks Miracast por perto
segunda-tela --mode extend --device "Galaxy Tab S9"  # monitor virtual + Wi-Fi Direct + WFD
segunda-tela --mode mirror --device "Galaxy Tab S9"  # espelhar
segunda-tela --mode extend --device "Galaxy Tab S9" --capture portal # captura pelo portal (fallback)

segunda-tela --ui              # UI: procurar, escolher, estender/espelhar, conectar
segunda-tela --loopback-test   # CI / sem tablet (M1–M7 + RTSP simples)
segunda-tela doctor            # checa EVDI, encoders, P2P, portal, USB/adb
```

No modo estender, os quadros são lidos direto do monitor virtual EVDI — sem
seletor do portal, sem risco de capturar o eDP-1. Na primeira vez, posicione
“Segunda Tela” em Configurações → Telas.

### UI: «No module named gi»

O `gi` **não** se instala com pip no venv isolado. Reinstale assim:

```bash
sudo apt update
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire
cd /caminho/do/segunda-tela-linux
rm -rf .venv
./scripts/install.sh
segunda-tela --ui
```

### Modo USB (cabo)

```bash
sudo ./scripts/install-udev-samsung.sh
sudo apt install adb
# No tablet: Tethering USB **e** Depuração USB (aceite “Permitir depuração USB”)
segunda-tela doctor                   # seção "-- USB --": adb, autorização, tether, IP do tablet, portas
segunda-tela -v --usb --mode extend   # mesmo caminho EVDI (estender) do Wi-Fi
segunda-tela usb --prefer-adb
```

O app do Windows, no cabo, roda **WFD/RTSP comum** dentro de um túnel de
portas USB (Connlib): o PC manda um *source ready* (MS-MICE) para a porta
7250 do tablet e o tablet conecta de volta na 7236 do PC. Aqui o adb faz o
papel do túnel: `adb reverse tcp:7236`, grava a config de cabo
(`{"USB":1}`) do app `com.samsung.android.secondscreen`, abre o app,
envia o *source ready* por `adb forward` e espera o tablet conectar. O vídeo
(RTP/UDP) vai pelo IP do tether. O app do tablet normalmente é acordado por
Bluetooth (MCF), que não reproduzimos — abra a “Segunda tela” no painel
rápido antes. Se não atender, use Wi-Fi Direct (`--device`). Detalhes:
[docs/USB.md](docs/USB.md).

### Cabo com VNC (estender de verdade)

O modo `--usb` acima ainda depende do receptor Miracast do tablet. Para estender
o desktop pelo cabo sem Wi-Fi Display:

```bash
# Tablet: Depuração USB aceita, e o app bVNC (com.iiordanov.freebVNC) instalado.
segunda-tela doctor
segunda-tela --usb-vnc
```

O PC cria a "Segunda Tela" (EVDI, à direita do eDP-1), escuta VNC só em
`127.0.0.1:5900` e faz `adb reverse tcp:5900 tcp:5900`. A senha sai no
terminal. O bVNC abre sozinho em `vnc://127.0.0.1:5900`. O toque volta por um
dispositivo uinput mapeado só nessa saída. O `wayvnc` do Pop!_OS 24.04 (0.7.2)
não serve: o cosmic-comp não tem `wlr-screencopy`. Passo a passo:
[docs/CABO-VNC.md](docs/CABO-VNC.md).

### Log e «não é possível exibir o conteúdo porque não é seguro»

Todo start grava um log de depuração rotativo em
`~/.local/state/segunda-tela/segunda-tela.log` (o caminho aparece na
primeira linha), com a troca RTSP M1–M16 **literal**: inclusive a resposta do
tablet ao M3 (`wfd_video_formats`, `wfd_audio_codecs`, `wfd_client_rtp_ports`,
`wfd_content_protection` e parâmetros próprios da Samsung). `--dump-rtsp`
mostra a mesma troca no terminal, e `--log-file` muda o caminho.

Até a v0.3.2 o M3 perguntava `wfd_content_protection`. Os tablets Galaxy
respondem `HDCP2.x port=…` e então esperam uma sessão HDCP (é o que o
Miracast do Windows faz, na porta 25030). Como nunca fazemos HDCP, o tablet
recusava com «não é seguro». A partir da 0.3.3 fazemos como o
gnome-network-displays: o padrão (`--no-hdcp`) não menciona proteção de
conteúdo no M3 nem no M4. `--probe-hdcp` só serve para diagnóstico.

No Galaxy Tab S10 FE 5G (0.3.3, `ab466a7`) o M1–M7 já fechava sem HDCP e o
tablet ainda mostrava a mesma frase, com ecrã preto e TEARDOWN ~9 s depois.
O fluxo de mídia é que falhava: o M4 não negociava áudio (a Samsung trata
MPEG-TS só de vídeo como conteúdo inseguro) e o SETUP `19000-19001` metia
`rtpbin` mesmo com RTCP 0 no M3, o que gerava
`gst_mini_object_unref: assertion 'mini_object != NULL' failed`. O fluxo
agora segue o gnome-network-displays (MPEG-TS em RTP payload 33, porta
19000, sem `rtpbin`, AAC silenciosa). No Tab S10 FE o nível anunciado é só
3.1, então o modo é 1280×720p30 com SPS nível 3.1. `--gnd-compat` manda o
1080p30 fixo desse projeto.

O aviso `dnsmasq … CAP_CHOWN` no journal vem do dnsmasq que o NetworkManager
sobe para o grupo P2P (DHCP do tablet). Ele é inofensivo: o tablet recebe o IP
normalmente.

## Análise do app Windows

O app da Store `Segunda tela` (9PLTXW5DX5KB) foi inspecionado com
[REA](https://github.com/morluto/rea) (inventário PE/CLI + strings). Conclusão:
há caminho **MCF** (proprietário) e caminho **Miracast/P2P**. Este projeto
implementa o caminho aberto. Detalhes e evidências: `docs/FINDINGS.md`.

## Créditos e licença

MIT — ver [LICENSE](LICENSE). O comportamento de Wi-Fi P2P/WFD foi estudado no
[GNOME Network Displays](https://gitlab.gnome.org/GNOME/gnome-network-displays)
(GPL-3.0) como referência; o código aqui foi escrito do zero em Python a partir
da especificação Wi-Fi Display, sem copiar código do GND (detalhes em
[NOTICE](NOTICE)). EVDI, GStreamer, PipeWire e NetworkManager são usados em
tempo de execução, com as licenças deles.

---

# Segunda Tela for Linux (English)

Unofficial, independent **Wi-Fi Display (Miracast) source over Wi-Fi Direct**
so a tablet can act as an **extended** second screen on **Pop!_OS** / Linux.
**Not affiliated with Samsung.**

What's new in 0.3:

- `segunda-tela list` discovers Wi-Fi P2P peers through NetworkManager D-Bus
  and shows which ones advertise WFD sink IEs.
- `segunda-tela --mode extend --device <name|MAC|path>` forms a Wi-Fi Direct
  group carrying our source WFD IEs, then runs a proper WFD source: we send M1,
  query sink caps (M3), pick a mode (VESA 1920x1200p60 → CEA 1080p60/30 →
  1080p30 fallback), send M4/M5, handle SETUP/PLAY, keep-alives, RTCP if the
  sink asks, and stream H.264 in MPEG-TS over RTP (PT 33) to the sink's RTP
  port (usually 19000).
- **Extend** creates an EVDI virtual connector with our own EDID (1920x1200,
  2560x1600, 1080p @60) so COSMIC treats it as a real monitor, then captures
  *only that output* by grabbing its frames straight from EVDI (no portal
  dialog). The ScreenCast portal is a fallback (`--capture portal`, or EVDI
  unusable); a remembered portal choice that is not the EVDI output is
  discarded.
- H.264 via VA-API (`vah264enc`, then `vaapih264enc`) with `x264enc` fallback.
- GTK UI: discover peers, pick one, extend/mirror toggle, connect.

```bash
sudo ./scripts/install-deps-popos.sh && sudo ./scripts/setup-evdi.sh && ./scripts/install.sh
segunda-tela doctor
segunda-tela list
segunda-tela --mode extend --device "Galaxy Tab S9"
```

Pop!_OS 24.04 / COSMIC walkthrough and known uncertainties:
[docs/COSMIC.md](docs/COSMIC.md). Real-hardware paths (P2P, EVDI, portal,
VAAPI) are **untested in CI**; the RTSP/WFD exchange is covered by a loopback
test against a simulated sink, and the media pipeline was validated by
encoding and decoding locally.

USB cable mode (0.3.2): reverse engineering showed that Samsung's cable mode
is plain WFD/RTSP inside a per-port USB tunnel (Connlib). The PC sends an
MS-MICE SOURCE_READY to the tablet's port 7250, and the tablet connects back
to the PC's RTSP port 7236. With `--usb` we use adb as the tunnel
(`adb reverse tcp:7236`, `adb forward` → 7250). We also write the receiver's
cable config (`com.samsung.android.secondscreen`), open the app, send
SOURCE_READY, and fall back to dialling any RTSP sink port. RTP goes over the
tether IP. The tablet app is normally woken over Bluetooth (MCF), which we
don't reproduce, so open "Second screen" on the tablet first; Wi-Fi Direct
(`--device`) remains the fallback. `segunda-tela doctor` has a USB section
(adb, authorization, tether iface/IP, peer reachability, ports). Details:
[docs/USB.md](docs/USB.md). Samsung's MCF one-tap pairing is out of scope.

`segunda-tela --usb-vnc` (0.3.5) is the cable path that does not use Miracast:
the EVDI output is placed to the right of eDP-1 and served alone over VNC
through `adb reverse tcp:5900`, with bVNC Free launched on the tablet and
touch injected by a uinput device mapped to that output. noble's wayvnc 0.7.2
cannot select the output on COSMIC (no `wlr-screencopy`). Steps and limits:
[docs/CABO-VNC.md](docs/CABO-VNC.md).

Content protection (0.3.3): Galaxy sinks answer `wfd_content_protection:
HDCP2.x port=…` when M3 asks for it, then expect an HDCP session (Windows'
Miracast stack runs HDCP 2.x on port 25030). We never do HDCP, so the tablet
showed "can't display content because it's not secure". Like
gnome-network-displays, M3 and M4 now leave content protection out entirely
(`--no-hdcp`, the default). `--probe-hdcp` only asks, for diagnostics.

A later Tab S10 FE 5G run (`ab466a7`) still showed that message after a clean
M1–M7: the MPEG-TS had no audio (Samsung often rejects a video-only stream as
not secure) and SETUP `client_port=19000-19001` forced `rtpbin` even though
M3 reported RTCP port 0, which logged
`gst_mini_object_unref: assertion 'mini_object != NULL' failed` and ended in
TEARDOWN. The stream follows gnome-network-displays (MPEG-TS over RTP payload 33, port
19000, no `rtpbin`, silent AAC). On the Tab S10 FE the advertised level is
only 3.1, so the mode is 1280×720p30 with SPS level 3.1. `--gnd-compat`
sends that project's hardcoded 1080p30 instead.

Every run writes a rotating debug log,
`~/.local/state/segunda-tela/segunda-tela.log`, with the verbatim RTSP
exchange. `--dump-rtsp` echoes that exchange to the console.

License: MIT. Wi-Fi P2P/WFD behaviour was studied in GNOME Network Displays
(GPL-3.0) as a reference only; no GND code was copied — see [NOTICE](NOTICE).
