"""Console + always-on rotating debug log file.

The file (default `~/.local/state/segunda-tela/segunda-tela.log`, honouring
`$XDG_STATE_HOME`) always gets DEBUG, including the verbatim RTSP M1–M16
exchange from `segunda_tela.wfd.rtsp.wire`. The console gets INFO, or DEBUG
with `-v`. `--dump-rtsp` adds only the RTSP wire records to the console.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

WIRE_LOGGER = "segunda_tela.wfd.rtsp.wire"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def default_log_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "segunda-tela" / "segunda-tela.log"


class _ConsoleFilter(logging.Filter):
    def __init__(self, level: int, dump_rtsp: bool) -> None:
        super().__init__()
        self.level = level
        self.dump_rtsp = dump_rtsp

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= self.level:
            return True
        return self.dump_rtsp and record.name == WIRE_LOGGER


def setup_logging(verbose: bool = False, dump_rtsp: bool = False,
                  log_file: Optional[str | os.PathLike] = None) -> Optional[Path]:
    """Configure logging; returns the log file path (None if it could not be opened)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_segunda_tela", False):
            root.removeHandler(h)
            h.close()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    console.addFilter(_ConsoleFilter(logging.DEBUG if verbose else logging.INFO, dump_rtsp))
    console._segunda_tela = True  # type: ignore[attr-defined]
    root.addHandler(console)

    path = Path(log_file) if log_file else default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                                  encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("cannot open log file %s: %s", path, exc)
        return None
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FORMAT))
    fh._segunda_tela = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    return path
