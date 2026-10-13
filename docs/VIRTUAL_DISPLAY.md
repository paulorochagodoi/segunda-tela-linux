# Virtual display on Pop!_OS

| Environment | Extend (extra monitor) | Mirror |
|-------------|------------------------|--------|
| Pop!_OS 22.04 GNOME Wayland | Portal screencast (region/whole); optional EVDI | PipeWire portal |
| Pop!_OS 22.04 GNOME X11 | xrandr dummy / EVDI | ximagesrc |
| Pop!_OS 24.04 COSMIC Wayland | Portal / COSMIC display APIs (evolving); EVDI | PipeWire |

EVDI (DisplayLink open module) is the practical way to get a **real** extra DRM
connector without writing a kernel driver. Install script: `scripts/install-evdi.sh`.

True Samsung-style "only second screen" exclusive mode is not implemented.
