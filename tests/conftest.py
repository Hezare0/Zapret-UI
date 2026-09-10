import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def sample_repo(tmp_path):
    root = tmp_path / "zapret with spaces"
    root.mkdir()
    (root / "service.bat").write_text('@echo off\nset "LOCAL_VERSION=fixture"\n', encoding="utf-8")
    for name in ("general.bat", "general (ALT).bat", "general (ALT3).bat", "general (ALT11).bat", "other.bat"):
        (root / name).write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin/winws.exe").write_bytes(b"fixture, never execute")
    (root / "lists").mkdir()
    (root / "lists/ipset-all.txt").write_bytes(b"203.0.113.113/32\r\n")
    (root / "utils").mkdir()
    return root


@pytest.fixture(scope="session")
def app():
    from zapret_client.gui import create_application
    from PySide6.QtGui import QFontDatabase
    application = create_application(["zapret-client-tests"])
    application.setQuitOnLastWindowClosed(False)
    for name in ("segoeui.ttf", "segoeuib.ttf", "consola.ttf"):
        font = Path(os.environ["SystemRoot"]) / "Fonts" / name
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    return application
