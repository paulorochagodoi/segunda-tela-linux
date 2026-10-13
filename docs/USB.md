# USB cable mode

## What the Windows app does (evidence)

From REA inventories / strings on `Samsung.SecondScreen.Usb` + `ss_conn_lib2.dll`
(see `docs/FINDINGS.md`):

1. Match Samsung USB: `VID_04E8` (e.g. `PID_6860` in the Windows AQS filter).
2. Open a proprietary **Connlib** stack (`ConnlibStartup` → `DeviceOpen` →
   `ConnectionOpen`) which exposes **`ConnlibConnectionGetIpAddr`** and
   **`ConnlibConnectionGetPort`**.
3. Open **TCP/UDP** sockets (`OpenTcpClient` / `OpenTcpServer` / `OpenUdpClient`)
   using named ports `RtspPort`, `RtpPort`, `RtcpPort`, `UibcPort`, etc.
4. Optional **MTP** side channel (`UsbMtp`) to push a small config file — not the
   video bitstream.
5. On failure: *"Failed to open USB. Projecting over Wi-Fi Direct instead."*

So the wired path is still **IP + RTSP/RTP**, not raw isochronous USB video.
Connlib itself is closed-source; we do not redistribute or reimplement it.

## What Linux does (`--usb` / `segunda-tela usb`)

| Step | Mechanism |
|------|-----------|
| Detect tablet | sysfs `idVendor=04e8` (+ udev rule `packaging/udev/99-segunda-tela-samsung.rules`) |
| IP link (preferred) | USB tethering → kernel `rndis_host` / `cdc_ncm` / `cdc_ether` + NetworkManager |
| IP link (alternate) | `adb reverse tcp:7236` if USB debugging is on |
| Stream | Existing open **WFD RTSP + H.264/RTP** source bound/advertised on that link |

```bash
sudo ./scripts/install-udev-samsung.sh
# No tablet: ative Tethering USB (ou USB debugging)
segunda-tela --usb
# or
segunda-tela usb --prefer-adb
segunda-tela --usb-detect-test
```

## Honesty / limitations

- **Samsung “Second Screen” USB one-tap** that depends on Connlib will **not**
  speak to this stack.
- Over **USB tethering**, a WFD/Smart View sink (or any RTSP client) that can
  reach `rtsp://<PC-tether-IP>:7236` may work — **untested without hardware**.
- **adb reverse** makes RTSP available as `127.0.0.1:7236` *on the tablet*; RTP
  over UDP often still needs tether or extra forwards — also **untested**.
- Fallback if USB IP cannot be established: use Wi-Fi mode (`segunda-tela`
  without `--usb`), matching Windows’ Wi-Fi Direct fallback intent.
