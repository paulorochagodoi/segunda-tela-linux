"""Mensagens quando PyGObject/GTK4 não está disponível no interpretador atual."""
from __future__ import annotations

GI_FIX_PT = """\
GTK4/libadwaita não disponível neste Python ({detail}).

O módulo «gi» vem do pacote do sistema (python3-gi), não do pip.
No Pop!_OS / Ubuntu:

  sudo apt update
  sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \\
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \\
    gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire

  cd ~/segunda-tela-linux   # ou o caminho do clone
  rm -rf .venv
  ./scripts/install.sh

O install.sh recria o venv com --system-site-packages para enxergar o gi.
Enquanto isso, use a CLI:  segunda-tela
"""


def gi_missing_message(exc: BaseException) -> str:
    return GI_FIX_PT.format(detail=exc)
