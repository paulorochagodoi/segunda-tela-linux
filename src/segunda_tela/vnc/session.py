"""`--usb-vnc`: EVDI extended desktop, RFB server, uinput touch, adb reverse."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..display.evdi import EvdiError
from ..display.randr import find_evdi_output, query_layout
from ..display.virtual import create_virtual_display
from .encode import describe_jpeg_encoder, set_jpeg_hardware
from .input import EvdiKeyboard, EvdiTouch, UinputError
from .va_jpeg import close_va_jpeg
from .layout import apply_position, install_touch_map, is_extended_right, primary_output, target_right_of
from .link import VncLinkError, launch_bvnc, prepare_vnc_transport
from .server import VncServer
from .wayvnc import assess_wayvnc, detect_protocols, detect_wayvnc_text
from .auth import random_vnc_password

log = logging.getLogger(__name__)


class UsbVncSession:
    def __init__(
        self,
        width: int = 1920,
        height: int = 1200,
        fps: int = 60,
        port: int = 5900,
        wait_tether_s: float = 20.0,
        launch: bool = True,
        allow_hw: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.port = port
        self.wait_tether_s = wait_tether_s
        self.launch = launch
        self.allow_hw = allow_hw
        self.notes: list[str] = []
        self.password = ""
        self._display = None
        self._transport = None
        self._server: Optional[VncServer] = None
        self._touch: Optional[EvdiTouch] = None
        self._keys: Optional[EvdiKeyboard] = None
        self._pump: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._bvnc_auto = False

    def start(self) -> None:
        assessment = assess_wayvnc(detect_wayvnc_text(), detect_protocols())
        self.notes.append(assessment.summary)
        for line in assessment.details:
            log.info("wayvnc: %s", line)
        try:
            self._display = create_virtual_display(
                width=self.width, height=self.height, mode="extend",
                fps=self.fps, capture="evdi",
            )
        except EvdiError as exc:
            raise EvdiError(str(exc)) from exc
        self.notes.extend(self._display.notes)
        self._place_output()
        try:
            self._transport = prepare_vnc_transport(port=self.port, wait_tether_s=self.wait_tether_s)
        except VncLinkError:
            self._close_display()
            raise
        self.notes.extend(self._transport.notes)
        width, height = self._framebuffer_size()
        self.password = random_vnc_password()
        self._touch = EvdiTouch()
        output_name = self._evdi_output_name()
        if output_name:
            try:
                path = install_touch_map(output_name)
                self.notes.append(f"Toque mapeado só para {output_name} ({path}).")
            except OSError as exc:
                self.notes.append(f"Não gravei o map_to_output do COSMIC: {exc}")
        else:
            self.notes.append(
                "A saída EVDI ainda não tem nome no cosmic-randr; o toque pode cair no eDP-1 "
                "até ela aparecer. Veja docs/CABO-VNC.md."
            )
        try:
            self._touch.open(width, height)
        except UinputError as exc:
            self.notes.append(f"Toque indisponível: {exc}")
            self._touch = None
        else:
            # Write the map again once the device exists. cosmic-comp matches
            # the uinput name; a file written before the node appears is easy
            # to miss until the next login.
            if output_name:
                try:
                    path = install_touch_map(output_name)
                    self.notes.append(f"Toque do uinput confirmado em {output_name} ({path}).")
                except OSError as exc:
                    self.notes.append(f"Não gravei o map_to_output do COSMIC: {exc}")
        self._keys = EvdiKeyboard()
        try:
            self._keys.open()
        except UinputError as exc:
            self.notes.append(f"Teclado/roda indisponível: {exc}")
            self._keys = None
        set_jpeg_hardware(self.allow_hw)
        encoder = describe_jpeg_encoder(self.allow_hw)
        log.info("VNC encoder: %s", encoder)
        self.notes.append(f"Encoder: {encoder}.")
        self._server = VncServer(
            password=self.password,
            width=width,
            height=height,
            hosts=list(self._transport.bind_hosts),
            port=self._transport.port,
            fps=self.fps,
            on_pointer=self._touch.pointer if self._touch else None,
            on_wheel=self._keys.wheel if self._keys else None,
            on_key=self._keys.key if self._keys else None,
        )
        if self._touch:
            self._server.on_resize = self._touch.reconfigure
        try:
            self._server.start()
        except OSError as exc:
            self.stop()
            raise VncLinkError(f"não escutei a porta VNC: {exc}") from exc
        where = ", ".join(f"{host}:{self._server.port}" for host in self._transport.bind_hosts)
        self.notes.append(f"VNC escutando {where}.")
        self._pump = threading.Thread(target=self._pump_frames, name="vnc-evdi", daemon=True)
        self._pump.start()
        if self.launch and self._transport.can_shell and self._transport._adb is not None:
            ok, detail = launch_bvnc(
                self._transport._adb, self._transport.client_host, self._server.port, self.password,
            )
            self._bvnc_auto = ok
            self.notes.extend(line for line in detail.splitlines() if line.strip())

    def banner_lines(self) -> list[str]:
        """User-visible lines. The password is only here, never in the log."""
        if not self._server or not self._transport:
            return list(self.notes)
        lines = list(self.notes)
        lines.append(f"Senha VNC: {self.password}")
        where = f"{self._transport.client_host}:{self._server.port}"
        if self._bvnc_auto:
            lines.append(f"No tablet: o bVNC já abriu em {where}.")
        else:
            lines.append(f"No tablet: bVNC → {where}. Se não conectou, use o endereço e a senha acima.")
        return lines

    def wait(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(3600)

    def stop(self) -> None:
        self._stop.set()
        if self._pump:
            self._pump.join(timeout=2)
            self._pump = None
        if self._server:
            self._server.stop()
            self._server = None
        if self._touch:
            self._touch.close()
            self._touch = None
        if self._keys:
            self._keys.close()
            self._keys = None
        close_va_jpeg()
        if self._transport:
            self._transport.close()
            self._transport = None
        self._close_display()

    # ----------------------------------------------------------------- internals
    def _close_display(self) -> None:
        if self._display is not None:
            try:
                self._display.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("fechar EVDI: %s", exc)
            self._display = None

    def _framebuffer_size(self) -> tuple[int, int]:
        mon = getattr(self._display, "evdi", None)
        mode = getattr(mon, "mode", None) if mon is not None else None
        if mode and mode[0] and mode[1]:
            return int(mode[0]), int(mode[1])
        return self.width, self.height

    def _evdi_output_name(self) -> str:
        layout = query_layout()
        evdi = find_evdi_output(layout.outputs, set()) if layout.outputs else None
        if evdi is None and getattr(self._display, "layout", None) is not None:
            evdi = getattr(self._display.layout, "output", None)
        return evdi.name if evdi is not None else ""

    def _place_output(self) -> None:
        layout = query_layout()
        if not layout.outputs:
            self.notes.append(
                "Layout do compositor indisponível; posicione 'Segunda Tela' à direita do eDP-1 "
                "em Configurações → Telas."
            )
            return
        evdi = find_evdi_output(layout.outputs, set())
        if evdi is None and getattr(self._display, "layout", None) is not None:
            evdi = getattr(self._display.layout, "output", None)
        anchor = primary_output(layout.outputs, evdi)
        if evdi is None or anchor is None:
            self.notes.append("Não achei a saída EVDI ao lado do eDP-1 no cosmic-randr.")
            return
        if is_extended_right(evdi, anchor):
            self.notes.append(f"{evdi.name} já está à direita de {anchor.name} ({evdi.x},{evdi.y}).")
            return
        try:
            x, y = target_right_of(anchor)
        except ValueError as exc:
            self.notes.append(str(exc))
            return
        w = evdi.width or self.width
        h = evdi.height or self.height
        ok, detail = apply_position(evdi.name, x, y, w, h)
        if ok:
            self.notes.append(f"{evdi.name} posicionada à direita de {anchor.name}: {detail}")
        else:
            self.notes.append(
                f"Não movi {evdi.name} para {x},{y} ({detail}). "
                "Arraste 'Segunda Tela' para a direita do eDP-1 em Configurações → Telas."
            )

    def _pump_frames(self) -> None:
        mon = getattr(self._display, "evdi", None)
        server = self._server
        if mon is None or server is None:
            return
        seq = 0
        while not self._stop.is_set():
            try:
                new_seq, frame, damage = mon.wait_frame_damage(seq, 0.5)
            except Exception as exc:  # noqa: BLE001
                log.warning("EVDI wait: %s", exc)
                time.sleep(0.2)
                continue
            try:
                if frame is None or new_seq == seq:
                    continue
                seq = new_seq
                mode = getattr(mon, "mode", None)
                if not mode:
                    continue
                width, height = int(mode[0]), int(mode[1])
                need = width * height * 4
                if len(frame) < need:
                    continue
                # Only the clips evdi_grab_pixels returned. Inventing a full
                # framebuffer here made every update one 1920×1200 rectangle.
                if not damage:
                    continue
                server.submit(frame[:need], damage, width, height)
            finally:
                release = getattr(mon, "release_frame", None)
                if release:
                    release()
