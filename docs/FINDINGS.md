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
| USB second screen | Out of scope for v1 | Would need separate USB protocol RE |
| Samsung trademarks / UI clone | No | Independent name and UI |

## Limitations & ethics

- Official MSIX was obtained from Microsoft Store delivery endpoints for analysis
  only; it must not be redistributed.
- No Hopper/Ghidra decompilation of native code was required for this summary;
  managed inventories + strings were enough to choose the open Miracast path.
- Without a Galaxy Tab on the bench, end-to-end hardware validation is **untested**.
- Do not claim affiliation with Samsung.

## Evidence artifacts (local, not in git)

Kept under `/workspace/segundatela-re/` on the build machine: MSIX bundle,
extracted x64 package, REA JSON inventories, string dumps. SHA-256 of the
bundle can be regenerated from the Store CDN; do not commit binaries.
