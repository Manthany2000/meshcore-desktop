# PyInstaller spec: a one-folder build of MeshCore Desktop for Linux or Windows.
# Build with packaging/build-linux.sh or packaging/build-windows.ps1.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
WIN = sys.platform == "win32"

hidden = ["qasync"]
if WIN:
    hidden += ["meshcore_desktop.win_pair", "winrt.windows.devices.bluetooth", "winrt.windows.devices.enumeration",
               "bleak.backends.winrt.client", "bleak.backends.winrt.scanner"]
else:
    hidden += ["meshcore_desktop.bluez_pair", "bleak.backends.bluezdbus.client", "bleak.backends.bluezdbus.scanner"]

a = Analysis(
    [str(ROOT / "packaging/launch.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "meshcore_desktop/web"), "meshcore_desktop/web"),
           (str(ROOT / "meshcore_desktop/icons"), "meshcore_desktop/icons")],
    hiddenimports=hidden,
    # Big Qt pieces the app doesn't use.
    excludes=["PySide6.Qt3DCore", "PySide6.QtQuick3D", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
              "tkinter", "pytest"],
)
# PyInstaller pulls in every QML module and Qt add-on that ships with PySide6;
# a widgets app with a web view needs none of them.
UNUSED = ("/qml/", "\\qml\\", "Quick3D", "Multimedia", "SpatialAudio", "TextToSpeech", "VirtualKeyboard",
          "Qt6Pdf", "Qt6Charts", "DataVisualization", "Graphs", "Qt63D", "Qt3D", "Qt6Sensors", "Qt6Location",
          "Qt6Scxml", "Qt6RemoteObjects", "Qt6Designer", "Qt6Help", "Qt6ShaderTools", "Qt6SerialPort",
          "QtQuickControls2", "Qt6QuickControls2", "Qt6QuickDialogs", "Qt6QuickTemplates2", "Qt6Bluetooth",
          "platformthemes/libqgtk3", "printsupport/libcups")
a.binaries = [b for b in a.binaries if not any(u in b[1] or u in b[0] for u in UNUSED)]
# The graphics stack must come from the host: a libgbm/libEGL bundled from the
# build container's older Mesa doesn't match the host's GPU drivers, and Mesa
# refuses them ("outdated Mesa"). The host's drivers (Mesa, VA-API) also load
# into our process, so the libraries they share with us (libstdc++, expat, zlib,
# core libxcb extensions...) must be the host's newer ones too, or they fail with
# missing symbol versions like CXXABI_1.3.15. Every desktop Linux has all of these.
if not WIN:
    HOST_ONLY = ("libgbm.so", "libdrm.so", "libdrm_", "libGL.so", "libGLX", "libGLdispatch", "libOpenGL",
                 "libEGL", "libglapi", "libvulkan", "libwayland-client", "libwayland-egl", "libwayland-server",
                 "libwayland-cursor", "libxshmfence", "libva", "libLLVM", "libelf",
                 "libstdc++.so", "libgcc_s.so", "libexpat.so", "libz.so", "libzstd.so",
                 "libX11.so", "libX11-xcb.so", "libXau.so", "libXdmcp.so", "libXext.so", "libXfixes.so",
                 "libxcb.so", "libxcb-glx", "libxcb-dri", "libxcb-present", "libxcb-randr", "libxcb-render.so",
                 "libxcb-shm", "libxcb-sync", "libxcb-xfixes", "libxcb-xkb")
    a.binaries = [b for b in a.binaries if not Path(b[0]).name.startswith(HOST_ONLY)]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="meshcore-desktop", console=False,
          icon=str(ROOT / "packaging/app.ico") if WIN else None)
coll = COLLECT(exe, a.binaries, a.datas, name="MeshCore Desktop" if WIN else "meshcore-desktop")
