# Findings: Samsung "Segunda tela" / Second Screen (Store 9PLTXW5DX5KB)

**Scope:** Clean-room interoperability research. No Samsung binaries, decompiled
code, assets, or trademarks are redistributed. Evidence comes from Microsoft
Store package metadata, PE/CLI type and method inventories (REA
`inspect-managed-members`), and UTF-16/ASCII string scans of the official MSIX
(kept outside this repository).

**Analyzed package:** `SAMSUNGELECTRONICSCoLtd.SecondScreen_1.16.7.0`
(PackageFamilyName `SAMSUNGELECTRONICSCoLtd.SecondScreen_wyx1vj98g3asy`),
x64 MSIX from the Microsoft Store delivery CDN. Publisher: Samsung Electronics.

## Architecture (Windows app)

| Piece | Role | Evidence |
|-------|------|----------|
| `SecondScreen.exe` + WinUI UI | Foreground app | AppxManifest `Executable`, firewall TCP in/out |
| `SecondScreenService.exe` | Full-trust packaged service; gRPC IPC host | Manifest `windows.service`; types `GrpcStartup`, `SecondScreenServiceInterface` |
| `Samsung.SecondScreen.MCF` + `McfSdkLib*` | Samsung Multi Connection Framework: discovery, advertisement, handshake | Types `McfManager`, `StartDiscovery`, `SetAdvertisementAsync`; Android package names below |
| `Samsung.SecondScreen.Connection` | TCP session + **RTSP-framed** control messages | Types `TcpClient`/`TcpServer`, `RtspPacket`, `RtspFormat`, method names `SETUP`/`TEARDOWN`/`play` |
| `WrapperStreamServer` / `WrapperStreamClient` (native) | Media path: H.264 + AAC via Media Foundation MFTs; RTSP URL strings | Strings: `rtsp://`, `RTSP/1.0`, `H.264 encoder MFT`, `AAC encoder MFT` |
| `Samsung.SecondScreen.Display` | Virtual / secondary monitor via Windows DisplayConfig + EDID | Types wrapping `DisplayConfig*`, strings `EDIDManufactureID`, `IsSecondScreenName`, `DisplaySecondScreenOnly` |
| `Samsung.SecondScreen.Input` + `UibcSettingsWatcher` | Touch / S Pen back-channel (UIBC-class) | Type/name inventory in main assembly |
| `Samsung.SecondScreen.NearbyDevices` | Device watcher IPC | `ConnectRequestData`, `DeviceStateRequestData` |
| `Samsung.SecondScreen.Usb` + USB driver EXE | Wired path | `SAMSUNG_USB_Driver_for_Mobile_Phones.exe`, `SecondScreenUsbDriver` |
| `InTheHand.Net.Bluetooth`, radios capability | BLE / radio discovery assist | Manifest `DeviceCapability Name="radios"` |

## Connection paths (important)

String evidence in `Samsung.SecondScreen.dll` shows **two** prepare paths:

- `PrepareMcfConnection` / `OnMcfHandshakeStarted` / `OnMcfHandshakeFinished`
- `PrepareMiracastConnection` / `ResolveDeviceForMiracastConnection` / `ConnectedMiracastDeviceId` / `IsSelectedP2PDevice`

So the Windows app is not Miracast-only: Samsung MCF handles ecosystem pairing
(auto-connect with Galaxy Tab "Second Screen"), while a Miracast / Wi-Fi Direct
path also exists. Nearby-device reasons mention `REASON_SMARTVIEW_IN_USE` and
`REASON_SECONDSCREEN_IN_USE`.

### Android package names seen in MCF assembly

- `com.samsung.android.secondscreen` (+ `.mcf.message`)
- `com.samsung.android.mirroringplayer` (+ `.mcf.message`)
- `com.samsung.android.smartmirroring` (+ `.mcf.message`)

Minimum One UI Second Screen version constant: `OneUI5SecondScreenMinimumVersion`
(matches Store requirement One UI 5.1.1+).

## Control protocol (high level)

`Samsung.SecondScreen.Connection.Protocol` implements an RTSP-shaped framing
layer over TCP (`BaseTcp`, `TcpServer`/`TcpClient`) with custom body parsers:

`Version`, `DeviceName`, `PlaybackStatus`, `Rotation`, `AutoRotation`,
`Cursor`, `HardwareCursor`, `ScreenSaver`, `WiFiConnection`, `Mcc`.

This is **not** proven to be bit-compatible with open Miracast WFD parameter
sets; it is Samsung’s session control on top of RTSP verbs. Exact binary body
layouts were **not** reconstructed (no CIL decompilation committed; clean-room
rule). Open Wi-Fi Display RTSP (`OPTIONS`/`GET_PARAMETER`/`SET_PARAMETER` with
`wfd_*` keys, RTP/H.264) remains the interoperable target for Linux.

## Streaming

Native wrappers activate **H.264** and **AAC** encoders (Media Foundation).
Transport is RTSP-signaled; RTP/UDP or interleaved TCP is typical for WFD but
exact Samsung interleaved mode was not confirmed from metadata alone.

## Virtual display on Windows

Uses Windows CCD / DisplayConfig APIs and monitor EDID identity labeled as
Second Screen (registry under `PerMonitorSettings`). Linux equivalent should
use existing mechanisms (Mutter/COSMIC virtual monitor + PipeWire portals, or
EVDI DKMS), **not** a from-scratch kernel driver.

## What Linux can reasonably implement

| Feature | Feasible? | Approach |
|---------|-----------|----------|
| Extend desktop to a wireless display sink (Miracast / WFD source) | Yes (open standards) | GStreamer H.264 + RTSP WFD source; Wi-Fi P2P via NetworkManager/wpa_supplicant when available |
| Virtual monitor (extend mode) | Yes | PipeWire / xdg-desktop-portal RemoteDesktop+Screencast; optional EVDI; X11 dummy sink |
| Tablet "Second Screen" one-tap via MCF | **Blocked / incomplete** | Requires Samsung MCF stack, app IDs, and likely account/device attestation — not available as open source |
| UIBC touch / S Pen | Partial | Standard WFD UIBC if sink exposes it; Samsung extensions unknown |
| USB second screen | Partial (v0.3.2) | See `docs/USB.md`: WFD inside Connlib port tunnel; we use adb + MS-MICE source-ready on 7250 |
| Samsung trademarks / UI clone | No | Independent name and UI |

## Limitations & ethics

- Official MSIX was obtained from Microsoft Store delivery endpoints for analysis
  only; it must not be redistributed.
- No Hopper/Ghidra decompilation of native code was required for this summary;
  managed inventories + strings were enough to choose the open Miracast path.
- Without a Galaxy Tab on the bench, end-to-end hardware validation is **untested**.
- Do not claim affiliation with Samsung.

## Addendum: managed members & native boundaries (REA, 2026-10-12)

Source: REA `inspect-managed-members` and `inspect-managed-native-boundaries`
over the four assemblies below (metadata only; no IL bodies decompiled or
committed). All four are IL-only, not ReadyToRun, and not strong-name signed.

| Assembly | SHA-256 (prefix) | P/Invoke | Native imports |
|----------|------------------|----------|----------------|
| `Samsung.SecondScreen.Connection.dll` | `d580f7ea…` | 0 | none |
| `Samsung.SecondScreen.MCF.dll` | `d098170c…` | 0 | none (MCF native work is in `McfSdkLib*`) |
| `Samsung.SecondScreen.Display.dll` | `dc80d946…` | 17 | `user32` CCD/GDI display APIs, `kernel32` Wow64 FS redirection |
| `Samsung.SecondScreen.Input.dll` | `0db1024e…` | 7 | `user32` cursor/WinEvent/last-input, `advapi32` registry |

Observations:

- **Connection is pure managed RTSP-shaped framing.** No native boundary, so
  the media path is entirely in the separate native wrappers. Body types have
  only `ToString`/`Parse` members, which fits short text `key: value` bodies.
  Enum names: `RotationDegree` (0/90/180/270) and `AutoRotationState`
  (enable/disable). Byte layouts were not reconstructed.
- **Display is not a virtual display driver.** It only calls documented Win32
  APIs (`QueryDisplayConfig`, `SetDisplayConfig`, `DisplayConfigGet/SetDeviceInfo`,
  `ChangeDisplaySettingsEx`, `EnumDisplay*`). `DisplayConfigVideoOutputTechnology`
  includes `Miracast`, `IndirectWired`, and `IndirectVirtual`, and
  `OrderByVideoOutputTechnology` / `SetActualVideoOutputTechnology` suggest the
  app finds the Windows-created Miracast or indirect display target and then
  sets its mode, scale, rotation, and position. `DisplayDeviceType` has
  values `Tablet`, `Laptop`, `Monitor`, `Tv`, `Vst`. `Change16to10Ratio` hints
  at 16:10 tablet modes, which the CEA table cannot express (see below).
- **UIBC switch on Windows is a registry setting.** `UibcSettings` has
  `EnableUibc`/`SetUibc`, `RegGetValue`/`RegSetKeyValue`, and a constant named
  `MiracastKeySuffix`. That suggests it toggles the OS Miracast UIBC setting per
  device rather than shipping its own UIBC stack. On Linux, UIBC has to be
  implemented in our own WFD source.
- **Cursor forwarding.** `MouseCursorListener` hooks WinEvents and maps the
  system cursor to `MouseCursorType` (Arrow, IBeam, Wait, Hand, …). Together
  with the Connection `Cursor`/`HardwareCursor` bodies, this suggests that
  cursor shape is sent separately from the video stream.
- **MCF roles.** `McfAdvertisementReason` = None/MainWindow/BackgroundWake/
  NearbyConnection; `McfFailureReason` includes `BluetoothConnectionFailed`,
  so MCF discovery depends on Bluetooth. `StartIntentRequest` has
  `CreateNewSecondScreenRequest`, `CreateSecondScreenRequest`, and
  `CreateLocalPcRequest`, which suggests the PC sends an intent that launches
  the sink app on the tablet. This is still proprietary and is not
  implemented here.

### Effect on the Linux implementation

Nothing above makes Samsung's private bodies necessary for a standard Miracast
sink. `wfd/rtsp.py` now builds its capability strings only from the public
WFD v1.0 tables: CEA bitmap and native index from width/height/fps (interlaced
modes are never advertised; 640x480p60 is always set), and the H.264 level
from the macroblock rate (1080p30 → 4.0, 1080p60 → 4.2). It also advertises
LPCM 44.1/48 kHz and AAC 48 kHz stereo, answers only the keys requested in
GET_PARAMETER, and treats an empty GET_PARAMETER as a keep-alive. 16:10 tablet
panels would need the VESA/HH tables or WFD R2 and are left as future work.

## Evidence artifacts (local, not in git)

Kept under `/workspace/segundatela-re/` on the build machine: MSIX bundle,
extracted x64 package, REA JSON inventories, string dumps. SHA-256 of the
bundle can be regenerated from the Store CDN; do not commit binaries.


## USB path (wired)

Evidence (`Samsung.SecondScreen.Usb`, P/Invoke to `ss_conn_lib2.dll`, strings):

- Device filter: `USB\VID_04E8&PID_6860\…` (Samsung vendor `04e8`).
- Native stack symbols: `ConnlibStartup`, `ConnlibDeviceOpen`,
  `ConnlibConnectionOpen`, **`ConnlibConnectionGetIpAddr`**,
  **`ConnlibConnectionGetPort`**.
- Managed follow-up: `OpenTcpClient` / `OpenTcpServer` / `OpenUdpClient` with
  fields/ports named `RtspPort`, `RtpPort`, `UibcPort`, `HdcpPort`, etc.
- MTP (`UsbMtp`) uploads a config file (`files/config/secondscreen.data`, `USB` flag); not the H.264 transport.
- Ports/handshake (verified 2026-10): 7250 source-ready (PC connects), 7236 RTSP (PC listens), RTP 19000/19001 — see `docs/USB.md`.
- Failure string: `Failed to open USB. Projecting over Wi-Fi Direct instead.`

**Linux clean-room mirror:** obtain an IP interface via RNDIS/NCM tether or adb,
then run the same open WFD RTSP source. Do **not** ship Connlib. Details:
[`docs/USB.md`](USB.md).

## Content protection / "not secure" on the tablet (v0.3.3)

Symptom (real test, Galaxy Tab S10 FE 5G over Wi-Fi Direct): the session got
through RTSP, and then the **tablet** showed "não é possível exibir o
conteúdo porque não é seguro". gnome-network-displays mirrors to the same
tablet without that error.

Evidence:

* The Samsung Windows app has no `wfd_*` strings at all. The WFD
  RTSP/M3/M4 exchange comes from Windows' own Miracast source (see the USB
  findings: `StartProjectingAsync` → OS projection API). Samsung's code only
  maps an **`HdcpPort` = 25030** (PC connects to the tablet) next to RTSP/RTP.
  So the reference source the tablet is tuned for always runs HDCP 2.x.
  Content-protection strings in `WrapperStreamClient.dll` belong to Media
  Foundation's protection API, not to WFD.
* gnome-network-displays' M3 asks only for `wfd_client_rtp_ports`,
  `wfd_audio_codecs`, `wfd_video_formats`, `wfd_display_edid`,
  `wfd_idr_request_capability` (and `microsoft_cursor`). Its M4 sends only
  video/audio formats, presentation URL and RTP ports. It **never** mentions
  `wfd_content_protection`.
* Our M3 up to v0.3.2 also asked for `wfd_content_protection` (and
  `wfd_connector_type`). Most likely mechanism (inferred, not yet confirmed
  from a tablet log): a source asks for that parameter when
  it intends to protect the stream. The sink answers `HDCP2.x port=N`, then
  waits for HDCP AKE. Since none happens, it flags the content as insecure.

Fix: the default M3 is the gnome-network-displays key set. M4 never carries
`wfd_content_protection`, and the code asserts that. `--probe-hdcp` restores
the query for diagnostics only. The full M1–M16 exchange is written verbatim
to `~/.local/state/segunda-tela/segunda-tela.log` so the next real-device
test confirms (or refutes) this from the tablet's actual M3 answer.
