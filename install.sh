#!/usr/bin/env bash
# Installs MeshCore Desktop for this user on Linux. No root needed.
#   From a downloaded copy:  ./install.sh
#   From nothing:            curl -fsSL https://raw.githubusercontent.com/Manthany2000/meshcore-desktop/main/install.sh | bash
# With no copy nearby it downloads one to ~/meshcore-desktop, or to $MESHCORE_DIR if set.
set -euo pipefail

REPO=https://github.com/Manthany2000/meshcore-desktop.git
die() { echo "Error: $*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "this script is for Linux. On Windows, see the README."

# Find the project: beside this script, or download it.
here=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$here" && -f "$here/meshcore_desktop/__main__.py" ]]; then
  dir="$here"
else
  dir="${MESHCORE_DIR:-$HOME/meshcore-desktop}"
  if [[ -f "$dir/meshcore_desktop/__main__.py" ]]; then
    echo "Using the copy already in $dir"
  else
    [[ ! -e "$dir" ]] || die "$dir already exists and isn't MeshCore Desktop. Set MESHCORE_DIR to another folder."
    command -v git >/dev/null || die "git is needed. Install it with your package manager and run this again."
    echo "Downloading to $dir"
    git clone -q "$REPO" "$dir"
  fi
fi

# Python 3.12 or newer, under any of its usual names.
python=""
for p in python3 python3.14 python3.13 python3.12; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    python="$p"
    break
  fi
done
[[ -n "$python" ]] || die "Python 3.12 or newer is needed. Install it with your package manager and run this again."

cd "$dir"
if [[ ! -x .venv/bin/python ]]; then
  echo "Creating the Python environment"
  "$python" -m venv .venv || die "couldn't create it. On Debian or Ubuntu: sudo apt install python3-venv"
fi
echo "Installing dependencies (PySide6 is large, this can take a few minutes)"
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
chmod +x meshcore-desktop

# A command, and an entry in the desktop's app menu.
bin="$HOME/.local/bin"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$bin" "$apps"
ln -sf "$dir/meshcore-desktop" "$bin/meshcore-desktop"
cat > "$apps/meshcore-desktop.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=MeshCore Desktop
Comment=Chat over a MeshCore companion node by Bluetooth
Exec="$dir/meshcore-desktop"
Icon=$dir/meshcore_desktop/icons/app.png
Categories=Network;Chat;
StartupWMClass=meshcore-desktop
Terminal=false
ENTRY

command -v bluetoothctl >/dev/null || echo "Warning: BlueZ wasn't found. Install it (package 'bluez') to connect to a real node."
if command -v ldconfig >/dev/null && ! ldconfig -p 2>/dev/null | grep -c libxcb-cursor >/dev/null; then
  echo "Warning: libxcb-cursor wasn't found, and the window may not open without it. On Debian or Ubuntu: sudo apt install libxcb-cursor0"
fi

echo
echo "Installed. Open MeshCore Desktop from your app menu, or run:"
case ":$PATH:" in
  *":$bin:"*) echo "  meshcore-desktop --sim    (try it with no radio)" ;;
  *) echo "  $bin/meshcore-desktop --sim    (try it with no radio)" ;;
esac
