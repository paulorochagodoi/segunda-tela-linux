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
| USB cabo: tether/adb → PC **inicia** a sessão (abre o receptor via adb, acha a porta do sink, disca M1–M7) | Implementado (sem Connlib) · testado contra sink simulado · **não testado em hardware** |
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
- Para **estender**: `evdi-dkms` + `libevdi1` (pacotes da distro)

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
segunda-tela --mode extend --device "Galaxy Tab S9" --capture evdi   # sem diálogo do portal

segunda-tela --ui              # UI: procurar, escolher, estender/espelhar, conectar
segunda-tela --loopback-test   # CI / sem tablet (M1–M7 + RTSP simples)
segunda-tela doctor            # checa EVDI, encoders, P2P, portal, USB/adb
```

No modo estender, o COSMIC mostra o seletor de tela: **escolha “Segunda Tela”**
(o monitor virtual). Na primeira vez, posicione-o em Configurações → Telas.

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

No cabo **o PC toma a iniciativa** (nada no tablet disca para o PC sozinho):
via adb faz `adb reverse tcp:7236`, procura e abre apps/atividades/blocos do
painel rápido com cara de receptor, lê as portas TCP em escuta no tablet e
**conecta no sink** (pelo IP do tether ou por `adb forward`) rodando M1→M7
como source. O vídeo (RTP/UDP) vai para o IP do tablet no tether. Se nada
aceitar, o programa diz exatamente o que fazer.

O app Windows usa Connlib proprietário para obter IP+porta no cabo; a
“Segunda tela” Samsung **por cabo** depende dele e provavelmente **não**
funciona aqui — use Wi-Fi Direct (`--device`). Detalhes: [docs/USB.md](docs/USB.md).

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
  *only that output* via the xdg-desktop-portal ScreenCast (or directly from
  EVDI with `--capture evdi`).
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

USB cable mode (0.3.1): with `--usb` the PC now *initiates*. Over adb (if
authorized) it sets `adb reverse tcp:7236`, tries to open receiver-looking
apps/activities/quick-settings tiles, reads the tablet's listening TCP ports,
then dials the sink (tether IP or `adb forward`) and runs the source M1–M7 on
that socket; RTP goes to the tablet's tether IP. Extend uses the same EVDI path
as Wi-Fi. If nothing on the tablet accepts, it prints concrete next steps —
Samsung's cable "Second Screen" needs the proprietary Connlib, so Wi-Fi Direct
(`--device`) is the supported route. `segunda-tela doctor` has a USB section
(adb, authorization, tether iface/IP, peer reachability, ports). Details:
[docs/USB.md](docs/USB.md). Samsung's MCF one-tap pairing is out of scope.

License: MIT. Wi-Fi P2P/WFD behaviour was studied in GNOME Network Displays
(GPL-3.0) as a reference only; no GND code was copied — see [NOTICE](NOTICE).
