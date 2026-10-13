"""Simple GTK4 / AppIndicator-style window. Falls back to CLI if GI missing."""
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
        log.error("GTK4/libadwaita not available (%s). Use CLI without --ui.", exc)
        return 2

    from ..session import SecondScreenSession

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
            win.set_default_size(420, 320)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
            title = Gtk.Label(label="Segunda Tela para Linux")
            title.add_css_class("title-1")
            note = Gtk.Label(
                label="Projeto independente e não oficial.\nNão afiliado à Samsung Electronics.\n\n"
                "Inicia um source Miracast/Wi-Fi Display (RTSP :7236).\n"
                "No tablet: Smart View / tela sem fio — ou o recurso Second Screen se o aparelho aceitar WFD."
            )
            note.set_wrap(True)
            status = Gtk.Label(label="Parado")
            btn = Gtk.Button(label="Iniciar")
            btn.add_css_class("suggested-action")

            def toggle(_b):
                if session_holder["s"] is None:
                    s = SecondScreenSession(
                        width=args.width,
                        height=args.height,
                        fps=args.fps,
                        mode=args.mode,
                        rtsp_port=args.port,
                        bitrate_kbps=args.bitrate,
                    )

                    def start():
                        try:
                            s.start()
                            GLib.idle_add(status.set_text, f"Ouvindo em {s.advertise_ip}:{args.port}")
                            GLib.idle_add(btn.set_label, "Parar")
                            session_holder["s"] = s
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
            box.append(status)
            box.append(btn)
            win.set_content(box)
            self.window = win
            win.present()

    app = App()
    return app.run(None)
