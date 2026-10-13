"""xdg-desktop-portal ScreenCast client → PipeWire node + fd for `pipewiresrc`.

On COSMIC the portal backend (xdg-desktop-portal-cosmic) shows a picker; the
user selects the *virtual* output once. We request persist_mode=2 and store
the restore token so later runs can skip the dialog — whether COSMIC honours
persistence depends on the installed portal version.

Hardware/desktop path — only the pure helpers are unit-tested.
"""
from __future__ import annotations

import json
import logging
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ_PATH = "/org/freedesktop/portal/desktop"
IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
IFACE_REQUEST = "org.freedesktop.portal.Request"

SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2
CURSOR_METADATA = 4
PERSIST_NONE = 0
PERSIST_PERMANENT = 2


class PortalError(RuntimeError):
    pass


@dataclass
class PortalStream:
    node_id: int
    position: Optional[tuple[int, int]] = None
    size: Optional[tuple[int, int]] = None
    source_type: Optional[int] = None
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class PortalCapture:
    fd: int
    streams: list[PortalStream]
    restore_token: Optional[str] = None
    _keepalive: Any = None  # holds the D-Bus connection / session alive

    @property
    def node_id(self) -> int:
        return self.streams[0].node_id

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        sess = self._keepalive
        self._keepalive = None
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ helpers

def sender_to_path_component(unique_name: str) -> str:
    """`:1.42` -> `1_42` (portal request path convention)."""
    return unique_name.lstrip(":").replace(".", "_")


def request_path(unique_name: str, token: str) -> str:
    return f"{OBJ_PATH}/request/{sender_to_path_component(unique_name)}/{token}"


def new_token(prefix: str = "segtela") -> str:
    return prefix + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def parse_streams(raw) -> list[PortalStream]:
    """Portal `streams` result a(ua{sv}) (already unpacked) -> PortalStream list."""
    out = []
    for item in raw or []:
        node_id, props = item[0], dict(item[1] or {})
        pos = props.get("position")
        size = props.get("size")
        out.append(
            PortalStream(
                node_id=int(node_id),
                position=tuple(pos) if pos is not None else None,
                size=tuple(size) if size is not None else None,
                source_type=props.get("source_type"),
                props=props,
            )
        )
    return out


def looks_like_primary(stream: PortalStream, expected_size: Optional[tuple[int, int]]) -> bool:
    """Heuristic: did the user pick the built-in panel instead of the virtual output?

    The virtual output is normally placed right/left of eDP-1, so a stream at
    (0,0) is suspicious. Size alone cannot tell them apart when both are
    1920x1200 (as on Paulo's laptop).
    """
    if stream.position is not None and tuple(stream.position) == (0, 0):
        return True
    if expected_size and stream.size and tuple(stream.size) != tuple(expected_size):
        return True
    return False


def pipewiresrc_fragment(fd: int, node_id: int, fps: int) -> str:
    # keepalive-time re-sends the last buffer when the compositor emits no damage.
    return (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time={max(1, 1000 // max(1, fps))} "
        f"! videoconvert"
    )


def _token_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "segunda-tela" / "portal.json"


def load_restore_token(key: str) -> Optional[str]:
    try:
        return json.loads(_token_file().read_text()).get(key)
    except (OSError, ValueError):
        return None


def save_restore_token(key: str, token: Optional[str]) -> None:
    if not token:
        return
    path = _token_file()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[key] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ------------------------------------------------------------------ D-Bus

class _GioSession:
    """Synchronous wrapper around the async Request/Response portal pattern."""

    def __init__(self, timeout_s: float = 120.0) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"PyGObject (python3-gi) required for the ScreenCast portal: {exc}") from exc
        self.Gio, self.GLib = Gio, GLib
        self.timeout_s = timeout_s
        self.ctx = GLib.MainContext.new()
        self.ctx.push_thread_default()
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            self.ctx.pop_thread_default()
            raise
        self.session_handle: Optional[str] = None

    def finish_setup(self) -> None:
        self.ctx.pop_thread_default()

    def close(self) -> None:
        if self.session_handle:
            try:
                self.bus.call_sync(
                    BUS_NAME, self.session_handle, "org.freedesktop.portal.Session", "Close",
                    None, None, self.Gio.DBusCallFlags.NONE, 2000, None,
                )
            except Exception:  # noqa: BLE001
                pass
            self.session_handle = None

    def prop(self, name: str):
        res = self.bus.call_sync(
            BUS_NAME, OBJ_PATH, "org.freedesktop.DBus.Properties", "Get",
            self.GLib.Variant("(ss)", (IFACE_SCREENCAST, name)),
            self.GLib.VariantType("(v)"), self.Gio.DBusCallFlags.NONE, 5000, None,
        )
        return res.unpack()[0]

    def request(self, method: str, args_fmt: str, args: tuple, options: dict) -> dict:
        GLib = self.GLib
        token = new_token()
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        path = request_path(self.bus.get_unique_name(), token)
        result: dict = {}
        loop = GLib.MainLoop.new(self.ctx, False)

        def on_response(_conn, _sender, _path, _iface, _signal, params, *_a):
            code, results = params.unpack()
            result["code"] = code
            result["results"] = results
            loop.quit()

        def on_timeout(*_a):
            result.setdefault("code", -1)
            loop.quit()
            return False

        # Subscribe before calling so a fast Response cannot be missed.
        sub = self.bus.signal_subscribe(
            BUS_NAME, IFACE_REQUEST, "Response", path, None, self.Gio.DBusSignalFlags.NONE, on_response
        )
        try:
            variant_args = GLib.Variant(f"({args_fmt}a{{sv}})", args + (options,))
            self.bus.call_sync(
                BUS_NAME, OBJ_PATH, IFACE_SCREENCAST, method, variant_args,
                GLib.VariantType("(o)"), self.Gio.DBusCallFlags.NONE, 10000, None,
            )
            timer = GLib.timeout_source_new_seconds(int(self.timeout_s))
            timer.set_callback(on_timeout)
            timer.attach(self.ctx)
            loop.run()
            timer.destroy()
        finally:
            self.bus.signal_unsubscribe(sub)
        code = result.get("code", -1)
        if code != 0:
            reason = {1: "cancelled by user", 2: "portal error", -1: "timed out"}.get(code, f"code {code}")
            raise PortalError(f"ScreenCast.{method}: {reason}")
        return result.get("results", {})


def open_screencast(
    cursor_embedded: bool = True,
    persist_key: Optional[str] = "extend",
    restore_token: Optional[str] = None,
) -> PortalCapture:
    """Run CreateSession → SelectSources(monitor) → Start → OpenPipeWireRemote."""
    s = _GioSession()
    GLib = s.GLib
    try:
        try:
            version = s.prop("version")
            avail_cursor = s.prop("AvailableCursorModes")
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"ScreenCast portal not available: {exc}") from exc

        res = s.request(
            "CreateSession", "", (), {"session_handle_token": GLib.Variant("s", new_token("segtela_s"))}
        )
        s.session_handle = res["session_handle"]

        opts = {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", False),
        }
        if cursor_embedded and avail_cursor & CURSOR_EMBEDDED:
            opts["cursor_mode"] = GLib.Variant("u", CURSOR_EMBEDDED)
        if version >= 4 and persist_key:
            opts["persist_mode"] = GLib.Variant("u", PERSIST_PERMANENT)
            token = restore_token or load_restore_token(persist_key)
            if token:
                opts["restore_token"] = GLib.Variant("s", token)
        s.request("SelectSources", "o", (s.session_handle,), opts)

        res = s.request("Start", "os", (s.session_handle, ""), {})
        streams = parse_streams(res.get("streams"))
        if not streams:
            raise PortalError("portal returned no streams")
        new_restore = res.get("restore_token")
        if persist_key:
            save_restore_token(persist_key, new_restore)

        ret, fd_list = s.bus.call_with_unix_fd_list_sync(
            BUS_NAME, OBJ_PATH, IFACE_SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (s.session_handle, {})),
            GLib.VariantType("(h)"), s.Gio.DBusCallFlags.NONE, 10000, None, None,
        )
        fd = fd_list.get(ret.unpack()[0])
        os.set_inheritable(fd, True)
        log.info(
            "ScreenCast portal: node %s pos=%s size=%s",
            streams[0].node_id, streams[0].position, streams[0].size,
        )
        return PortalCapture(fd=fd, streams=streams, restore_token=new_restore, _keepalive=s)
    except Exception:
        s.close()
        raise
    finally:
        s.finish_setup()
