"""Exercise the actual frozen Qt application without running any BAT files."""

import json
from pathlib import Path
import sys

import psutil
from PySide6.QtCore import qVersion
from PySide6.QtGui import QGuiApplication

from . import __version__
from .gui import MainWindow
from .storage import Store
from .windows import is_admin
from .updates import find_git, run_external


def run_smoke(app, repo: Path | None, report: Path) -> int:
    report.parent.mkdir(parents=True, exist_ok=True)
    window = MainWindow(Store(report.parent / "smoke-state"), repo)
    try:
        window.show()
        app.processEvents()
        if not window.isVisible() or not window.winId():
            raise RuntimeError("Native Qt window was not created")
        if repo and (not window.repo or window.config_table.rowCount() == 0):
            raise RuntimeError("Repository discovery failed in the packaged application")
        preview = report.with_suffix(".png")
        if not window.grab().save(str(preview)):
            raise RuntimeError("Qt window rendering failed")
        libraries = sorted({item.path for item in psutil.Process().memory_maps()
                            if Path(item.path).name.casefold() in
                            {"icuuc.dll", "qt6core.dll", "qt6gui.dll", "qt6widgets.dll", "qwindows.dll"}})
        restored = [{"name": window.config_table.item(row, 0).text(),
                     "score": window.config_table.item(row, 2).text(),
                     "checked_at": window.config_table.item(row, 4).text()}
                    for row in range(window.config_table.rowCount())
                    if window.config_table.item(row, 2).text() != "—"]
        git_version = run_external([find_git(), "--version"], cwd=report.parent,
                                   log_directory=report.parent / "git-smoke").stdout.strip()
        report.write_text(json.dumps({
            "ok": True, "frozen": bool(getattr(sys, "frozen", False)), "version": __version__,
            "executable": sys.executable, "qt": qVersion(), "platform": QGuiApplication.platformName(),
            "admin": is_admin(), "configurations": window.config_table.rowCount(),
            "restored_results": restored, "icon_loaded": not app.windowIcon().isNull(),
            "git_version": git_version,
            "libraries": libraries, "preview": str(preview), "network_tests_run": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    finally:
        window.close()
        app.processEvents()
