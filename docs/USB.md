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
| Display | Same as Wi-Fi: `--mode extend` → EVDI connector + our EDID, capture only that output |
| **Initiate (0.3.1)** | `usb/initiate.py`, see below |
| Stream | WFD source (M1–M7) → H.264 / MPEG-TS / RTP to the tablet's **tether IP** |

### Why the PC must initiate

With the tether up (e.g. `enx… 10.108.120.2` ↔ tablet `10.108.120.1`) and the
source listening on `0.0.0.0:7236`, nothing on a Galaxy Tab dials a Linux PC:
its receivers only accept connections (Wi-Fi Direct) or speak Connlib (cable).
So after the link is up `UsbInitiator` runs, logging every step as `USB: …`:

1. **adb** (if installed and the device is *authorized*; the serial matching the
   Samsung USB device is preferred):
   - `adb reverse tcp:7236 tcp:7236` — a sink on the tablet can then reach us
     at `rtsp://127.0.0.1:7236`.
   - `ip -4 -o addr show` on the tablet → its real tether IP (replaces the
     `.1` guess).
   - `pm list packages -U` → packages that look like receivers
     (`secondscreen`, `sink`, `receiver`, `wfd`, `miracast`, `wireless display`…).
     Smart View (`smartmirroring`) is listed but **not** launched — it casts
     *from* the tablet.
   - Try to open a receiver: `--usb-sink-activity PKG/.Activity` if given,
     else receiver-named activities from `dumpsys package` (`am start -n`),
     else the launcher activity (`monkey -p`), else a matching custom
     quick-settings tile (`cmd statusbar click-tile`). Disable with
     `--no-usb-launch`.
   - `cat /proc/net/tcp /proc/net/tcp6` → LISTEN sockets on 7236/7250 or owned
     by a receiver app's uid.
2. **Dial** each candidate: `tether-IP:port` if the socket is bound to
   0.0.0.0/the tether IP, and/or `adb forward tcp:0 tcp:<port>` → `127.0.0.1`
   if bound to 0.0.0.0/loopback. Without adb we blind-dial `7236`/`7250` on the
   tether peer. The connected socket goes straight to
   `WfdRtspServer.connect_to()` (no separate probe — some sinks accept one
   connection): M1 immediately, then M2/M3/M4/M5, SETUP, PLAY. Success = the
   sink answered M1. RTP/UDP always goes to the tether IP (UDP can't ride adb).
3. Retry every few seconds for `--usb-initiate-timeout` (60 s); the server
   keeps listening, so an inbound connection also wins. If nothing worked, a
   guidance block says exactly what's missing (adb, authorization, receiver
   app, open port) and what to do — normally: **use Wi-Fi Direct**.

```bash
sudo ./scripts/install-udev-samsung.sh
sudo apt install adb
# Tablet: Tethering USB + Developer options → USB debugging; accept the RSA prompt
segunda-tela doctor                     # "-- USB --" section
segunda-tela -v --usb --mode extend     # verbose: every adb command + dial result
segunda-tela -v --usb --usb-sink-activity com.example/.SinkActivity
segunda-tela usb --prefer-adb
segunda-tela --usb-detect-test
```

### `doctor` USB section

Samsung device in sysfs · udev rule · local port 7236 free · tether
iface + IPv4 · adb installed · device authorized/unauthorized/offline ·
tablet tether IP (via adb) · receiver packages · tablet listening sink ports ·
peer reachable (TCP *refused* counts as reachable) · sink RTSP port open.
Lines are only failures (`!!`) when a Samsung device is plugged in.

## Honesty / limitations

- **Samsung “Second Screen” over the cable** depends on Connlib and will most
  likely **not** speak to this stack, even with the initiator. Its Wi-Fi
  Direct mode is plain Miracast → use `segunda-tela --device`.
- Receiver package/activity names on One UI are **not verified** on hardware;
  the initiator discovers them by pattern and logs what it found. Paste the
  `-v` log if it picks the wrong thing; `--usb-sink-activity` overrides it.
- Android 10+ may hide `/proc/net/tcp` from some contexts; then port discovery
  is empty and we fall back to blind dialing 7236/7250 over the tether.
- Over **USB tethering**, a WFD/Smart View sink (or any RTSP client) that can
  reach `rtsp://<PC-tether-IP>:7236` may work — **untested without hardware**.
- **adb reverse** makes RTSP available as `127.0.0.1:7236` *on the tablet*; RTP
  over UDP often still needs tether or extra forwards — also **untested**.
- Fallback if USB IP cannot be established: use Wi-Fi mode (`segunda-tela`
  without `--usb`), matching Windows’ Wi-Fi Direct fallback intent.
