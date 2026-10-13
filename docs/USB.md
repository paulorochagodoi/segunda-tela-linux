# USB cable mode

## What the Windows app does (verified with REA, 1.16.7.0)

Clean-room summary. The analysis used metadata, constants, call edges and IL
read-through of `Samsung.SecondScreen.Usb`, `Samsung.SecondScreen` (projection),
`Samsung.SecondScreen.MCF` and `Samsung.SecondScreen.Device`, plus strings from
`ss_conn_lib2.dll`. No Samsung code was copied. The full notes are kept outside
the repo.

### 1. Protocol: WFD/RTSP inside a port tunnel

The cable mode is **not** a separate proprietary display protocol. The
projection code uses one path for cable and Wi-Fi: it opens USB, then calls
Windows' own Miracast source (`StartProjectingAsync` on the tablet's Miracast
device). If USB can't be opened it logs *"Failed to open USB. Projecting over
Wi-Fi Direct instead."* and pairs over Wi-Fi Direct.

**Connlib** (`ss_conn_lib2.dll` plus the `ss_conn_service2` service from
Samsung's USB driver) is only a **per-port TCP/UDP tunnel** over the USB bulk
endpoints of `VID_04E8&PID_6860`. No RNDIS/NCM network or USB tethering is
involved. Its API opens one mapping per {listen|connect|bidirectional, IPv4,
TCP|UDP, port}. When Windows has no network profile at all, the app installs a
loopback network adapter so the Miracast stack has an interface to use.

### 2. Ports and handshake

The app opens these mappings. "PC listens" means the tablet connects to the PC
through the tunnel.

| Port  | Proto | Name in app        | PC role       | Required |
|-------|-------|--------------------|---------------|----------|
| 7260  | TCP   | CustomPort         | listens (Samsung side channel: rotation, cursor, screensaver… in RTSP-style messages) | yes |
| 7250  | TCP   | **SourceReadyPort**| **connects**  | yes |
| 7236  | TCP   | RtspPort           | **listens** (RTSP server, sends M1) | yes |
| 19000 | UDP   | RtpPort            | sends         | yes |
| 19001 | UDP   | RtcpPort           | sends         | yes |
| 50000 | TCP   | UibcPort           | listens       | best effort |
| 50001/50002 | UDP | hardware cursor | sends        | best effort |
| 25030 | TCP   | HdcpPort           | connects      | best effort |

The roles match Microsoft's public **Miracast over Infrastructure (MS-MICE)**
model:

1. The PC connects to the tablet's **7250** and sends **SOURCE_READY**
   (friendly name, RTSP port, source ID).
2. The tablet connects back to the PC's **7236**. The PC sends M1 and the
   normal WFD M1–M7 exchange follows.
3. H.264/MPEG-TS over RTP goes PC → tablet on UDP **19000** (RTCP 19001).

So **the PC does not send RTSP to the tablet**. It sends SOURCE_READY and
waits for the tablet to connect back.

### 3. How the tablet receiver is started

There is no adb usage, no broadcast and no mDNS anywhere in the USB path.

- **MCF start intent over Bluetooth.** The PC sends an MCF "simple message"
  (app id 34) to the package **`com.samsung.android.secondscreen`** with
  action `com.samsung.android.secondscreen.mcf.message`. On older One UI it
  goes instead to the legacy host **`com.samsung.android.smartmirroring`**
  with action `….smartmirroring.mcf.message`. The message is `start` or
  `stop`, and its extra data includes the PC's MCF/device id, name, type
  (PC) and a **`USB` flag**. The protocol is proprietary and travels over
  Bluetooth, so we don't reproduce it.
- **MTP config file.** When the tablet appears as an MTP device, the app
  writes `secondscreen.data` into `Android/data/<package>/files/config/`. It
  tries the new package first, then the legacy one. The file is a one-field
  JSON object, `USB` = 1|0. The app also reads the MTP vendor extension
  `samsung.com/devicestatus`.
- `com.samsung.android.mirroringplayer` belongs to a different feature
  ("local PC" mirroring).

## What Linux does (`--usb` / `segunda-tela usb`)

adb plays Connlib's role: `adb reverse` for the ports the PC listens on and
`adb forward` for the ports the PC dials. adb cannot carry UDP, so RTP needs
**USB tethering** as well.

| Step | Mechanism |
|------|-----------|
| Detect tablet | sysfs `idVendor=04e8` (+ udev rule `packaging/udev/99-segunda-tela-samsung.rules`) |
| IP link (RTP/UDP) | USB tethering → `rndis_host` / `cdc_ncm` / `cdc_ether` + NetworkManager |
| RTSP tunnel | `adb reverse tcp:7236 tcp:7236` → tablet reaches the PC at `127.0.0.1:7236` |
| Wake/config | adb writes `{"USB":1}` to `/sdcard/Android/data/<pkg>/files/config/secondscreen.data` for each installed Second Screen package, then opens the app (`--usb-sink-activity`, receiver-named activities, the launcher of `com.samsung.android.secondscreen`; never Smart View's launcher) |
| **Handshake** | MS-MICE **SOURCE_READY** → tablet `:7250` via `adb forward` (and the tether IP), then wait for the tablet to connect to 7236 |
| Fallback | generic sinks: dial a listening RTSP port (7236 or a port owned by a receiver app) and run M1–M7 on it |
| Stream | WFD source → H.264 / MPEG-TS / RTP to the tablet's **tether IP**:19000. When the sink came in via `adb reverse` (peer 127.0.0.1), RTP still goes to the tether IP |
| Display | Same as Wi-Fi: `--mode extend` → EVDI connector + our EDID |

Every step is logged as `USB: …` (`-v` also shows each adb command). It keeps
retrying for `--usb-initiate-timeout` (60 s) while the server keeps listening.
If nothing worked, a guidance block lists what's missing.

### Test on hardware (Paulo)

```bash
sudo ./scripts/install-udev-samsung.sh
sudo apt install adb
# Tablet: Settings → Connections → Mobile Hotspot and Tethering → USB tethering
#         Developer options → USB debugging; accept the RSA prompt
segunda-tela doctor                       # "-- USB --": Second Screen package, config, :7250
# Tablet: open Quick panel → "Second screen" and leave it on its waiting screen
segunda-tela -v --usb --mode extend
adb shell cat /proc/net/tcp6 | grep -i ':1C52 '   # is anything listening on 7250?
adb shell ls -l /sdcard/Android/data/com.samsung.android.secondscreen/files/config/
segunda-tela -v --usb --usb-sink-activity com.samsung.android.secondscreen/.SomeActivity
adb shell dumpsys package com.samsung.android.secondscreen | grep -A1 -i 'activity\|service' | head -60
```

Please send back: the `-v` log, the `doctor` USB section, and the `dumpsys
package` output if the tablet never shows a listener on 7250.

### `doctor` USB section

Samsung device in sysfs · udev rule · local port 7236 free · tether iface +
IPv4 · adb installed/authorized · tablet tether IP · **Second Screen package** ·
**cable config file** · **tablet listening on 7250** · other receiver apps ·
RTSP listeners · reachability of 7250/7236 over the tether. Failures are only
marked `!!` when a Samsung device is plugged in.

## Limitations / open questions

- The **Android side was not analysed** (no APK here). Without the MCF/Bluetooth
  start intent, the tablet app may not open 7250 even when its UI is open.
  That is the main risk. If so, Wi-Fi Direct (`--device`) is the route that works.
- The MS-MICE mapping is inferred from the port name, the port number, the
  roles and Windows' Miracast stack. The bytes we send follow Microsoft's
  public MS-MICE spec.
- On Android 11+ `adb shell` may not be allowed to write into another app's
  `Android/data`. The log says so if the config write fails.
- Connlib tunnels UDP. We rely on the tether IP, and a sink that binds RTP only
  to its tunnel endpoint would not receive video.
- UIBC (50000), the 7260 side channel, the hardware-cursor streams and HDCP are
  not implemented.
