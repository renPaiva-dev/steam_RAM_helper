#!/bin/bash
# Installs `steamlite` to ~/.local/bin and adds a "Steam (lite)" menu entry.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$DIR/steamlite.py" ~/.local/bin/steamlite
chmod +x "$DIR/steamlite.py"
cat > ~/.local/share/applications/steamlite.desktop <<DESKTOP
[Desktop Entry]
Name=Steam (lite)
Comment=Steam with ridiculously low RAM usage
Exec=$HOME/.local/bin/steamlite launch --freeze
Icon=steam
Terminal=false
Type=Application
Categories=Game;
DESKTOP
echo "installed: run 'steamlite launch' or pick 'Steam (lite)' in the app menu"
