# Segunda Tela para Linux

**English below.**

Projeto **independente e não oficial** para usar um tablet (por exemplo Galaxy Tab)
como segunda tela no **Pop!_OS** e outros desktops Linux, via **Wi-Fi Display
(Miracast)** — source RTSP + H.264.

> Não é afiliado à Samsung Electronics. Não redistribui o app da Microsoft Store
> nem marcas/ícones da Samsung. Veja [docs/FINDINGS.md](docs/FINDINGS.md).

## O que funciona / o que não

| Recurso | Status |
|---------|--------|
| Source Miracast/WFD (RTSP :7236 + RTP/H.264 via GStreamer) | Implementado |
| Handshake RTSP em loopback (sem tablet) | Testado |
| Captura PipeWire / X11 / padrão de teste | Implementado |
| Monitor virtual real (EVDI) | Script opcional; não obrigatório |
| Pareamento MCF “Second Screen” one-tap da Samsung | **Não** — ver [docs/SAMSUNG_MCF.md](docs/SAMSUNG_MCF.md) |
| Teste em Galaxy Tab real | **Não testado** (sem hardware neste ambiente) |
| UIBC / S Pen | Não implementado (v0.1) |

## Requisitos

- Pop!_OS 22.04 ou 24.04 (ou Ubuntu equivalente)
- Python 3.10+
- GStreamer 1.0 + plugins (good/ugly/libav/pipewire)
- Opcional: GTK4 + libadwaita (`--ui`)

```bash
sudo ./scripts/install-deps-popos.sh
./scripts/install.sh
```

## Uso

```bash
# CLI
segunda-tela                 # estender / capturar e ouvir WFD
segunda-tela --mode mirror
segunda-tela --width 2560 --height 1600 --bitrate 12000
segunda-tela --loopback-test # CI / sem tablet

# UI
segunda-tela --ui
```

No tablet: abra **Smart View** / projeção sem fio e escolha o PC, ou use um sink
WFD apontando para `rtsp://<IP-do-PC>:7236`. PC e tablet na mesma rede (ou P2P
quando o driver permitir).

## Análise do app Windows

O app da Store `Segunda tela` (9PLTXW5DX5KB) foi inspecionado com
[REA](https://github.com/morluto/rea) (inventário PE/CLI + strings). Conclusão:
há caminho **MCF** (proprietário) e caminho **Miracast/P2P**. Este projeto
implementa o caminho aberto. Detalhes e evidências: `docs/FINDINGS.md`.

## Licença

MIT — ver [LICENSE](LICENSE). Dependências (GStreamer, EVDI, etc.) mantêm as
licenças delas.

---

# Segunda Tela for Linux (English)

Unofficial, independent **Wi-Fi Display (Miracast) source** so a tablet can act
as a second screen on **Pop!_OS** / Linux. **Not affiliated with Samsung.**

See the feature table above. Hardware with a Galaxy Tab is **untested** here.
Samsung’s proprietary MCF “Second Screen” one-tap pairing is **out of scope**.

```bash
sudo ./scripts/install-deps-popos.sh && ./scripts/install.sh
segunda-tela --loopback-test
segunda-tela
```

Protocol research notes: [docs/FINDINGS.md](docs/FINDINGS.md). License: MIT.
