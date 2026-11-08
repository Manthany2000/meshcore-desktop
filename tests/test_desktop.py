import os
import sys
import time

from meshcore_desktop import desktop


def test_short_date_has_no_padding():
    lt = time.strptime("2026-11-05 09:03", "%Y-%m-%d %H:%M")
    assert desktop.short_date(lt) == "Nov 5"


def test_portable_data_dir(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "meshcore-desktop"))
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert desktop.data_dir() == tmp_path / "data"


def test_profile_data_dir_without_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "meshcore-desktop"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    if sys.platform != "win32":
        assert desktop.data_dir() == tmp_path / "xdg" / "meshcore-desktop"
