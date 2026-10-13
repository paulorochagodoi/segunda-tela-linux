# Virtual display on Pop!_OS

| Environment | Extend (extra monitor) | Mirror |
|-------------|------------------------|--------|
| Pop!_OS 24.04 COSMIC Wayland | **EVDI** connector + ScreenCast portal (pick the virtual output) or `--capture evdi` | Portal |
| Pop!_OS 22.04 GNOME Wayland | EVDI + portal (GNOME picker) | Portal |
| X11 sessions | EVDI + direct EVDI grab (`--capture evdi`) | ximagesrc |
| CI / headless | `--capture test` (videotestsrc) | same |

EVDI (DisplayLink's open-source module, `evdi-dkms` + `libevdi1`) gives a
**real** DRM connector without writing a kernel driver. We connect it with our
own EDID (`segunda_tela/display/edid.py`):

- preferred mode = requested/negotiated size (default 1920x1200@60),
- also 2560x1600@60 and 1920x1080@60 (CVT reduced-blanking timings),
- 237x148 mm physical size (11" 16:10 tablet), vendor id `SGT`, name "Segunda Tela".

Wi-Fi Display R1 can signal at most VESA 1920x1200; a 2560x1600 virtual
monitor is scaled to the negotiated stream size.

Setup: `sudo ./scripts/setup-evdi.sh`. COSMIC specifics: [COSMIC.md](COSMIC.md).
