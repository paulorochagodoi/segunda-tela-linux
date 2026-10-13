# Pop!_OS 24.04 (COSMIC) — estender a área de trabalho para o tablet

Alvo testado em projeto: Pop!_OS 24.04, COSMIC (Wayland), AMD Phoenix (amdgpu,
VA-API), Wi-Fi MediaTek MT7922, NetworkManager com `p2p-dev-wlo1`, painel
interno eDP-1 1920x1200.

> **Status honesto:** o fluxo completo (P2P → RTSP WFD → EVDI → portal →
> VAAPI → tablet) **não foi executado em hardware real** neste repositório. A
> parte RTSP/WFD é testada em loopback contra um sink simulado; o pipeline
> GStreamer foi validado codificando e decodificando localmente. Veja
> «Incertezas» no fim.

## Como funciona

```
Galaxy Tab (sink Miracast)
      ▲  RTP/MP2T (H.264, PID 0x1011) → porta 19000 do tablet
      │  RTSP :7236 (tablet conecta em nós; nós falamos primeiro: M1…M7)
      │
 Wi-Fi Direct (NetworkManager, grupo p2p-wlo1-N, IP 192.168.49.x típico)
      │
 segunda-tela ── EVDI: conector virtual com EDID 1920x1200 / 2560x1600 / 1080p
             └─ captura SÓ esse output (leitura direta do EVDI; portal só como fallback)
             └─ vah264enc (VA-API amdgpu) → mpegtsmux → rtpmp2tpay → UDP
```

## Instalação (uma vez)

```bash
git clone https://github.com/paulorochagodoi/segunda-tela-linux
cd segunda-tela-linux
sudo ./scripts/install-deps-popos.sh   # GStreamer (+bad/va), VA-API, portal, gi, evdi-dkms, libevdi1
sudo ./scripts/setup-evdi.sh           # evdi com 1 dispositivo no boot + regra udev uaccess
./scripts/install.sh                   # venv --system-site-packages + atalho
segunda-tela doctor                    # tudo [ok]?
```

Notas sobre EVDI:

- Usamos o **evdi-dkms** e a **libevdi1** da distro (código aberto da
  DisplayLink). O app só chama a libevdi via `ctypes`; não há módulo de kernel
  próprio.
- Se o `doctor` disser *evdi kernel module* `!!` e você usa **Secure Boot**, o
  módulo DKMS precisa estar assinado: `sudo mokutil --import /var/lib/dkms/mok.pub`
  e reinicie (o MOK Manager pede a senha).
- `setup-evdi.sh` grava `/etc/modprobe.d/segunda-tela-evdi.conf`
  (`initial_device_count=1`), `/etc/modules-load.d/segunda-tela-evdi.conf` e
  `/etc/udev/rules.d/70-segunda-tela-evdi.rules` (dá ao usuário da sessão
  acesso ao `/dev/dri/cardN` do evdi, sem rodar o app como root).
- Se ainda assim faltar permissão: `ls -l /dev/dri/` e `getfacl /dev/dri/cardN`;
  reiniciar a sessão normalmente resolve.

## Uso

```bash
# 1. No tablet: abra a recepção de tela sem fio (Smart View / Tela sem fio /
#    "Segunda tela" do tablet, dependendo do One UI) e deixe na tela de espera.
segunda-tela list                       # deve listar o tablet com WFD=sim, tipo primary-sink
segunda-tela --mode extend --device "Galaxy Tab S9"
```

O que acontece, na ordem:

1. **EVDI**: aparece um monitor novo "Segunda Tela" para o COSMIC.
2. **Captura**: os quadros são lidos direto do EVDI (`evdi_grab_pixels`),
   sem portal e sem diálogo — só o monitor virtual pode ser capturado.
   Com `--capture portal` (ou se o EVDI não abrir) o seletor do COSMIC abre:
   escolha "Segunda Tela". Uma escolha lembrada em
   `~/.config/segunda-tela/portal.json` que aponte para o eDP-1 (ou outra
   saída que não seja o EVDI) é apagada automaticamente e o seletor reabre;
   se ainda assim vier a tela errada, voltamos para a leitura do EVDI.
3. **Wi-Fi Direct**: o NetworkManager forma o grupo P2P (o tablet pode pedir
   para aceitar a conexão).
4. **RTSP/WFD**: o tablet abre TCP 7236; negociamos o modo (preferimos
   1920x1200p60 VESA quando o tablet anuncia; senão 1080p).
5. **PLAY**: começa o vídeo.

### Organizar as telas (uma vez)

Na primeira vez, o COSMIC coloca o monitor virtual em algum lado do eDP-1.
Abra **Configurações → Telas** e arraste "Segunda Tela" para a posição real do
tablet (esquerda/direita/acima), e escolha a resolução (1920x1200 recomendado —
é o que o Wi-Fi Display R1 consegue transmitir sem reescalar; 2560x1600 também
é oferecido no EDID, mas será reduzido para o modo negociado). O COSMIC lembra
a posição pelo EDID (nome/serial fixos), então isso deve ser uma vez só.

### Opções úteis

| Opção | Efeito |
|-------|--------|
| `--capture evdi` | Padrão no modo estender: lê os pixels direto do EVDI (sem portal, sem diálogo). Falha em vez de cair no portal se o EVDI não abrir. |
| `--capture portal` | Captura pelo portal ScreenCast (fallback; padrão no modo espelhar). |
| `--width 1920 --height 1080` | Força modo (se o tablet anunciar). |
| `--fps 30` | Limita fps (padrão 60 com VA-API, 30 com x264). |
| `--encoder x264enc` | Força encoder por software. |
| `--audio` | Envia áudio do desktop (AAC) se o tablet aceitar. Padrão: só vídeo. |
| `-v` | Log detalhado (mostra corpo do M3/M4, pipeline GStreamer). |

### Sem P2P (mesma rede)

`segunda-tela --mode extend` sem `--device` só escuta na LAN (porta 7236). Só
funciona com sinks que se conectam a um source por IP, o que o Smart View
normalmente **não** faz — por isso o P2P é o caminho principal.

## Firewall

O Pop!_OS não ativa firewall por padrão. Se você usa `ufw`:

```bash
sudo ufw allow 7236/tcp            # RTSP do tablet
sudo ufw allow in on p2p-wlo1-0    # DHCP do grupo P2P (o nome pode ser p2p-wlo1-1, -2…)
```

## Solução de problemas

| Sintoma | O que verificar |
|---------|-----------------|
| `list` vazio | Tablet na tela de recepção? `nmcli device` mostra `p2p-dev-wlo1`? `segunda-tela list --all` mostra algo sem WFD? |
| Conecta P2P, mas nada no tablet | `-v`: chegou `RTSP sink connected`? Se não, firewall; se sim, veja qual M falhou. |
| Tablet mostra preto | Log `gst:`; `segunda-tela doctor` (vah264enc presente?); tente `--encoder x264enc`. |
| Capturou a tela errada | Só acontece com `--capture portal`/fallback; rode sem `--capture` (padrão: EVDI) e veja o log `Captura:`. |
| Monitor virtual não aparece | `segunda-tela doctor`; `ls /sys/bus/platform/devices/ | grep evdi`; `journalctl -b | grep -i evdi`. |

## Incertezas (não verificadas em hardware)

- **COSMIC `persist_mode`**: pedimos `persist_mode=2` e guardamos o
  `restore_token`, mas não confirmamos se a versão do
  `xdg-desktop-portal-cosmic` do Pop 24.04 respeita. Se não, o seletor aparece
  a cada execução (ou use `--capture evdi`).
- **COSMIC + EVDI**: o cosmic-comp precisa aceitar o card evdi como saída
  secundária (multi-GPU). Esperado funcionar (como DisplayLink), mas não
  testado aqui.
- **MT7922 como P2P GO**: o driver `mt7921e` suporta P2P, mas há relatos de
  negociação de GO instável. Se `AddAndActivateConnection2` expirar, tente de
  novo com o tablet já na tela de recepção, ou reinicie o NetworkManager.
- **Smart View aceitar nosso source**: anunciamos os mesmos WFD IEs de device
  info (source, sessão disponível, porta 7236, 200 Mbps) que o GNOME Network
  Displays usa — e o GND conectou no Galaxy Tab do Paulo. Não enviamos IEs
  proprietários da Samsung; se o One UI exigir algo além disso para o modo
  "segunda tela" (MCF), o tablet pode só espelhar ou recusar. Ver
  [SAMSUNG_MCF.md](SAMSUNG_MCF.md).
- **1920x1200 (VESA)**: alguns sinks anunciam VESA mas só aceitam CEA. Se o
  tablet recusar o M4, tentamos de novo automaticamente com 1080p30.
- **Pedido de IDR do tablet** (`wfd_idr_request`): como o encoder roda em
  `gst-launch`, não forçamos keyframe sob demanda; o GOP é de 1 s para
  recuperar rápido.

---

## English summary

1. `sudo ./scripts/install-deps-popos.sh && sudo ./scripts/setup-evdi.sh && ./scripts/install.sh`
2. `segunda-tela doctor` → all `[ok]` (Secure Boot users: enroll the DKMS MOK key).
3. On the tablet open the wireless display receiver; `segunda-tela list`.
4. `segunda-tela --mode extend --device "<tablet name>"` → accept the Wi-Fi
   Direct prompt on the tablet. No portal dialog: frames come straight from
   the EVDI output.
5. First time only: arrange the new display in COSMIC Settings → Displays.

`--capture portal` uses the ScreenCast portal instead (fallback); a remembered
portal choice that is not the EVDI output (e.g. eDP-1) is discarded. Untested on real hardware: COSMIC portal persistence, COSMIC
accepting the EVDI card, MT7922 GO stability, and whether Smart View accepts a
non-Samsung source for anything beyond mirroring.
