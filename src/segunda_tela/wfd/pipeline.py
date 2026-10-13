"""GStreamer encode + RTP send pipeline."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)


class StreamPipeline:
    def __init__(
        self,
        capture_source: str,
        dest_host: str,
        dest_port: int,
        width: int = 1920,
        height: int = 1080,
        bitrate_kbps: int = 8000,
        fps: int = 30,
    ) -> None:
        self.capture_source = capture_source
        self.dest_host = dest_host
        self.dest_port = dest_port
        self.width = width
        self.height = height
        self.bitrate_kbps = bitrate_kbps
        self.fps = fps
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _build_cmd(self) -> list[str]:
        # Prefer hardware encoders when present; fall back to x264enc
        enc = "x264enc tune=zerolatency speed-preset=ultrafast bitrate={br} key-int-max={fps}".format(
            br=self.bitrate_kbps, fps=self.fps
        )
        # gst-launch parse-launch style as single string after gst-launch-1.0 -e
        pipeline = (
            f"{self.capture_source} ! "
            f"videoscale ! video/x-raw,width={self.width},height={self.height} ! "
            f"videoconvert ! video/x-raw,format=I420 ! "
            f"{enc} ! h264parse config-interval=1 ! "
            f"rtph264pay pt=96 config-interval=1 ! "
            f"udpsink host={self.dest_host} port={self.dest_port} sync=false async=false"
        )
        return ["gst-launch-1.0", "-e", "-q"] + pipeline.split()

    def start(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError(
                "GStreamer not found. Install: sudo apt install gstreamer1.0-tools "
                "gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
                "gstreamer1.0-plugins-ugly gstreamer1.0-libav"
            )
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            cmd = self._build_cmd()
            log.info("Starting stream to %s:%s", self.dest_host, self.dest_port)
            log.debug("Pipeline: %s", " ".join(cmd))
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        with self._lock:
            if not self._proc:
                return
            log.info("Stopping stream")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
