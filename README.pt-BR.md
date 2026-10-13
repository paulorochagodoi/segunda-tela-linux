# Segunda Tela para Linux

Documentação completa (PT + EN) em [README.md](README.md).

## UI e o erro «No module named gi»

O módulo `gi` vem do apt (`python3-gi`), não do pip. O `install.sh` usa
`python3 -m venv --system-site-packages`.

```bash
sudo apt update
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire
cd /caminho/do/segunda-tela-linux
rm -rf .venv
./scripts/install.sh
segunda-tela --ui
```
