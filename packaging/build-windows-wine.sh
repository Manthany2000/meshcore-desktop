#!/usr/bin/env bash
# Builds dist/MeshCore-Desktop-windows-x64.zip from Linux, with Windows Python under Wine.
# The Wine prefix and Python live in build/wine (delete it to start fresh).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export WINEPREFIX="$root/build/wine/prefix" WINEDEBUG=-all
py="$root/build/wine/python"
if [ ! -x "$py/venv/Scripts/python.exe" ]; then
  mkdir -p "$py" "$WINEPREFIX"
  curl -sSL -o "$py/py.nupkg" https://www.nuget.org/api/v2/package/python/3.12.10
  unzip -q -o "$py/py.nupkg" 'tools/*' -d "$py"
  wineboot -i >/dev/null 2>&1
  wine "$py/tools/python.exe" -m venv "$py/venv"
fi
wine "$py/venv/Scripts/python.exe" -m pip install -q -r requirements.txt pyinstaller
rm -rf build/dist build/work
wine "$py/venv/Scripts/pyinstaller.exe" --noconfirm --clean --log-level WARN \
  --distpath build/dist --workpath build/work packaging/meshcore-desktop.spec 2>&1 | grep -v "MESA\|pci id" || true
app="build/dist/MeshCore Desktop"
cp packaging/PORTABLE.txt "$app/"
mkdir -p "$app/data" dist
rm -f dist/MeshCore-Desktop-windows-x64.zip
(cd build/dist && python3 -c '
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk("MeshCore Desktop"):
        if not files and not dirs:
            z.write(root, root + "/")
        for f in files:
            z.write(os.path.join(root, f))
' "$root/dist/MeshCore-Desktop-windows-x64.zip")
wineserver -k 2>/dev/null || true
echo "built dist/MeshCore-Desktop-windows-x64.zip"
