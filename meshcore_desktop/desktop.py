"""The bits that differ between Linux and Windows: where things are saved,
desktop notifications, icons."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

APP = "meshcore-desktop"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "meshcore_desktop"


def _exe_dir() -> Path | None:
    """The folder of the packaged app (next to the AppImage, if it is one)."""
    if os.environ.get("APPIMAGE"):
        return Path(os.environ["APPIMAGE"]).resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def portable_dir() -> Path | None:
    """A "data" folder beside the packaged app makes it portable: messages and
    settings stay in there instead of in the user's profile."""
    d = _exe_dir()
    return d / "data" if d and (d / "data").is_dir() else None


def data_dir() -> Path:
    if p := portable_dir():
        return p
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "MeshCore Desktop"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP


def settings() -> QSettings:
    if p := portable_dir():
        return QSettings(str(p / "settings.ini"), QSettings.IniFormat)
    return QSettings(APP, APP)


def app_icon() -> QIcon:
    return QIcon.fromTheme("network-wireless", QIcon(str(HERE / "icons/app.png")))


_STD = {"network-connect": QStyle.SP_MediaPlay, "network-disconnect": QStyle.SP_MediaStop,
        "edit-delete": QStyle.SP_TrashIcon, "list-add": QStyle.SP_FileDialogNewFolder,
        "document-send": QStyle.SP_ArrowRight, "network-wireless": QStyle.SP_DriveNetIcon}


def icon(name: str) -> QIcon:
    """The theme icon on Linux. Windows has no icon theme; Qt maps some names to
    Segoe Fluent glyphs, and the rest fall back to the style's own icons."""
    ic = QIcon.fromTheme(name)
    if ic.isNull() and name in _STD:
        ic = QApplication.style().standardIcon(_STD[name])
    return ic


_tray: QSystemTrayIcon | None = None


def notify(window, title: str, text: str):
    """A desktop notification without a lasting tray icon."""
    global _tray
    if sys.platform.startswith("linux"):
        if shutil.which("notify-send"):
            QProcess.startDetached("notify-send", ["-a", "MeshCore Desktop", "-i", "network-wireless", title, text])
        return
    # Windows (and macOS): Qt only shows toasts from a tray icon, so show one
    # just while the toast is up. Clicking the toast brings the window back.
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return
    if _tray is None:
        _tray = QSystemTrayIcon(app_icon(), window)
        _tray.setToolTip("MeshCore Desktop")
        _tray.messageClicked.connect(lambda: (window.showNormal(), window.raise_(), window.activateWindow()))
        _tray.activated.connect(lambda _r: (window.showNormal(), window.raise_(), window.activateWindow()))
        _tray.hide_timer = QTimer(window, singleShot=True, interval=12000, timeout=_tray.hide)
    _tray.show()
    _tray.showMessage(title, text, app_icon(), 8000)
    _tray.hide_timer.start()


def short_date(lt) -> str:
    """E.g. Nov 5. Not strftime's %-d, which Windows doesn't have."""
    import time
    return f"{time.strftime('%b', lt)} {lt.tm_mday}"
