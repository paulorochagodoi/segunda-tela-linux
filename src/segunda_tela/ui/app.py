"""Simple GTK4 / libadwaita window. Falls back to CLI if GI missing."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


def run_ui(args) -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib, Gtk
    except Exception as exc:  # noqa: BLE001
        from .deps_error import gi_missing_message

        msg = gi_missing_message(exc)
        log.error("%s", msg)
        print(msg, flush=True)
        return 2

    from ..session import SecondScreenSession
    from ..usb.link import UsbLinkError, prepare_usb_link

    session_holder = {"s": None}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="br.techpartners.segundatela")
            self.window = None

        def do_activate(self):
            if self.window:
                self.window.present()
                return
            win = Adw.ApplicationWindow(application=self)
            win.set_title("Segunda Tela para Linux")
            win.set_default_size(440, 380)
            box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=12,
                margin_top=16,
                margin_bottom=16,
                margin_start=16,
                margin_end=16,
            )
            title = Gtk.Label(label="Segunda Tela para Linux")
            title.add_css_class("title-1")
            note = Gtk.Label(
                label="Projeto independente e não oficial.\nNão afiliado à Samsung Electronics.\n\n"
                "Source Miracast/Wi-Fi Display (RTSP :7236).\n"
                "Modo USB: tether RNDIS/NCM ou adb reverse — sem Connlib proprietário."
            )
            note.set_wrap(True)
            usb_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            usb_label = Gtk.Label(label="Conexão USB (cabo)")
            usb_label.set_hexpand(True)
            usb_label.set_xalign(0)
            usb_switch = Gtk.Switch()
            usb_switch.set_active(bool(getattr(args, "usb", False)))
            usb_row.append(usb_label)
            usb_row.append(usb_switch)
            status = Gtk.Label(label="Parado")
            status.set_wrap(True)
            btn = Gtk.Button(label="Iniciar")
            btn.add_css_class("suggested-action")

            def toggle(_b):
                if session_holder["s"] is None:
                    use_usb = usb_switch.get_active()

                    def start():
                        try:
                            usb_link = None
                            advertise = None
                            if use_usb:
                                GLib.idle_add(status.set_text, "USB: detectando / tether…")
                                usb_link = prepare_usb_link(
                                    rtsp_port=args.port,
                                    prefer_adb=bool(getattr(args, "prefer_adb", False)),
                                    wait_tether_s=float(getattr(args, "usb_wait", 20.0)),
                                )
                                if usb_link.transport != "adb-reverse":
                                    advertise = usb_link.local_ip
                            s = SecondScreenSession(
                                width=args.width,
                                height=args.height,
                                fps=args.fps,
                                mode=args.mode,
                                rtsp_port=args.port,
                                bitrate_kbps=args.bitrate,
                                advertise_ip=advertise,
                                usb_link=usb_link,
                            )
                            s.start()
                            extra = f" usb/{usb_link.transport}" if usb_link else ""
                            GLib.idle_add(
                                status.set_text,
                                f"Ouvindo em {s.advertise_ip}:{args.port}{extra}",
                            )
                            GLib.idle_add(btn.set_label, "Parar")
                            session_holder["s"] = s
                        except UsbLinkError as e:
                            GLib.idle_add(status.set_text, f"USB: {e}")
                        except Exception as e:  # noqa: BLE001
                            GLib.idle_add(status.set_text, f"Erro: {e}")

                    threading.Thread(target=start, daemon=True).start()
                else:
                    session_holder["s"].stop()
                    session_holder["s"] = None
                    status.set_text("Parado")
                    btn.set_label("Iniciar")

            btn.connect("clicked", toggle)
            box.append(title)
            box.append(note)
            box.append(usb_row)
            box.append(status)
            box.append(btn)
            win.set_content(box)
            self.window = win
            win.present()

    app = App()
    return app.run(None)
