"""GTK4 / libadwaita window. Falls back to an apt hint if GI is missing.

Wi-Fi Direct peers are listed after "Procurar"; pick one (or type its name),
choose Estender/Espelhar and press Conectar.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

LINKS = ("Wi-Fi Direct (P2P)", "Rede local (LAN)", "Cabo USB")
MODES = ("extend", "mirror")
MODE_LABELS = ("Estender (monitor virtual)", "Espelhar")


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
    from ..wfd.p2p import NMWifiP2P, P2PError, find_and_connect

    holder: dict = {"session": None, "peers": [], "busy": False}

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
            win.set_default_size(480, 560)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                          margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)

            title = Gtk.Label(label="Segunda Tela para Linux")
            title.add_css_class("title-1")
            note = Gtk.Label(
                label="Projeto independente e não oficial — não afiliado à Samsung Electronics.\n"
                "Source Miracast/Wi-Fi Display. No tablet, deixe a recepção de tela "
                "(Smart View / tela sem fio) aberta."
            )
            note.set_wrap(True)

            def row(label_text: str, widget) -> Gtk.Box:
                r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lab = Gtk.Label(label=label_text)
                lab.set_xalign(0)
                lab.set_hexpand(True)
                r.append(lab)
                r.append(widget)
                return r

            mode_dd = Gtk.DropDown.new_from_strings(list(MODE_LABELS))
            mode_dd.set_selected(MODES.index(args.mode) if args.mode in MODES else 0)
            link_dd = Gtk.DropDown.new_from_strings(list(LINKS))
            link_dd.set_selected(2 if getattr(args, "usb", False) else 0)

            device_entry = Gtk.Entry()
            device_entry.set_placeholder_text("Nome do tablet (ex.: Galaxy Tab S9)")
            if args.device:
                device_entry.set_text(args.device)
            device_entry.set_hexpand(True)

            peer_list = Gtk.ListBox()
            peer_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(160)
            scroller.set_vexpand(True)
            scroller.set_child(peer_list)

            find_btn = Gtk.Button(label="Procurar dispositivos")
            status = Gtk.Label(label="Parado")
            status.set_wrap(True)
            status.set_xalign(0)
            btn = Gtk.Button(label="Conectar")
            btn.add_css_class("suggested-action")

            def set_status(text: str) -> bool:
                status.set_text(text)
                return False

            def fill_peers(peers) -> bool:
                holder["peers"] = peers
                while (child := peer_list.get_first_child()) is not None:
                    peer_list.remove(child)
                for p in sorted(peers, key=lambda p: (not p.is_wfd_sink, -p.strength)):
                    tag = "Miracast" if p.is_wfd_sink else ("WFD" if p.wfd_capable else "sem WFD")
                    lab = Gtk.Label(label=f"{p.label}   ·   {tag}   ·   {p.strength}%")
                    lab.set_xalign(0)
                    peer_list.append(lab)
                return False

            def on_peer_selected(_lb, lrow):
                if lrow is None:
                    return
                peers = sorted(holder["peers"], key=lambda p: (not p.is_wfd_sink, -p.strength))
                idx = lrow.get_index()
                if 0 <= idx < len(peers):
                    device_entry.set_text(peers[idx].name or peers[idx].hw_address)

            peer_list.connect("row-selected", on_peer_selected)

            def on_find(_b):
                if holder["busy"]:
                    return
                holder["busy"] = True
                find_btn.set_sensitive(False)
                status.set_text("Procurando via Wi-Fi Direct…")

                def work():
                    try:
                        nm = NMWifiP2P()
                        peers = nm.discover(12.0, on_update=lambda ps: GLib.idle_add(fill_peers, ps))
                        GLib.idle_add(fill_peers, peers)
                        n = sum(1 for p in peers if p.is_wfd_sink)
                        GLib.idle_add(set_status, f"{len(peers)} dispositivo(s), {n} sink(s) Miracast")
                    except P2PError as e:
                        GLib.idle_add(set_status, f"P2P: {e}")
                    finally:
                        holder["busy"] = False
                        GLib.idle_add(find_btn.set_sensitive, True)

                threading.Thread(target=work, daemon=True).start()

            find_btn.connect("clicked", on_find)

            def stopped() -> bool:
                status.set_text("Parado")
                btn.set_label("Conectar")
                btn.set_sensitive(True)
                return False

            def toggle(_b):
                if holder["session"] is not None:
                    s = holder["session"]
                    holder["session"] = None
                    btn.set_sensitive(False)
                    threading.Thread(target=lambda: (s.stop(), GLib.idle_add(stopped)), daemon=True).start()
                    return
                link_kind = link_dd.get_selected()
                mode = MODES[mode_dd.get_selected()]
                device = device_entry.get_text().strip()
                if link_kind == 0 and not device:
                    status.set_text("Escolha ou digite o nome do tablet (Procurar dispositivos).")
                    return
                btn.set_sensitive(False)

                def start():
                    s = None
                    try:
                        usb_link, advertise = None, None
                        if link_kind == 2:
                            GLib.idle_add(set_status, "USB: detectando / tether…")
                            usb_link = prepare_usb_link(
                                rtsp_port=args.port,
                                prefer_adb=bool(getattr(args, "prefer_adb", False)),
                                wait_tether_s=float(getattr(args, "usb_wait", 20.0)),
                            )
                            if usb_link.transport != "adb-reverse":
                                advertise = usb_link.local_ip
                        GLib.idle_add(set_status, "Preparando tela (monitor virtual / portal)…")
                        s = SecondScreenSession(
                            width=args.width, height=args.height, fps=args.fps, mode=mode,
                            rtsp_port=args.port, bitrate_kbps=args.bitrate, advertise_ip=advertise,
                            usb_link=usb_link, capture=args.capture, encoder=args.encoder, audio=args.audio,
                            probe_hdcp=bool(getattr(args, "probe_hdcp", False)),
                        )
                        s.start()
                        notes = "\n".join(s.notes)
                        if link_kind == 0:
                            GLib.idle_add(set_status, f"Conectando a '{device}' via Wi-Fi Direct…\n{notes}")
                            link = find_and_connect(device, discover_s=float(args.discover_timeout),
                                                    rtsp_port=args.port)
                            s.attach_p2p(link)
                            where = f"P2P {link.interface} ({link.peer.label})"
                        elif link_kind == 2:
                            where = f"USB/{usb_link.transport}"
                            s.start_usb_initiator(
                                launch=not getattr(args, "no_usb_launch", False),
                                sink_activity=getattr(args, "usb_sink_activity", None),
                                timeout_s=float(getattr(args, "usb_initiate_timeout", 60.0)),
                                say=lambda m: GLib.idle_add(set_status, m),
                            )
                        else:
                            where = "LAN"
                        holder["session"] = s
                        GLib.idle_add(set_status,
                                      f"Pronto ({where}, {mode}). Sink → rtsp://{s.advertise_ip}:{args.port}\n{notes}")
                        GLib.idle_add(btn.set_label, "Desconectar")
                    except (UsbLinkError, P2PError) as e:
                        if s:
                            s.stop()
                        GLib.idle_add(set_status, str(e))
                    except Exception as e:  # noqa: BLE001
                        if s:
                            s.stop()
                        GLib.idle_add(set_status, f"Erro: {e}")
                    finally:
                        GLib.idle_add(btn.set_sensitive, True)

                threading.Thread(target=start, daemon=True).start()

            btn.connect("clicked", toggle)

            box.append(title)
            box.append(note)
            box.append(row("Modo", mode_dd))
            box.append(row("Conexão", link_dd))
            dev_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dev_row.append(device_entry)
            dev_row.append(find_btn)
            box.append(dev_row)
            box.append(scroller)
            box.append(status)
            box.append(btn)
            win.set_content(box)
            def on_close(*_a) -> bool:
                if holder["session"] is not None:
                    holder["session"].stop()
                    holder["session"] = None
                return False

            win.connect("close-request", on_close)
            self.window = win
            win.present()

    app = App()
    return app.run(None)
