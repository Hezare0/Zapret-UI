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
                                   log_directory=report.parent / ("git-smoke-" + report.stem)).stdout.strip()
        report.write_text(json.dumps({
            "ok": True, "frozen": bool(getattr(sys, "frozen", False)), "version": __version__,
            "executable": sys.executable, "qt": qVersion(), "platform": QGuiApplication.platformName(),
            "admin": is_admin(), "configurations": window.config_table.rowCount(),
            "restored_results": restored, "icon_loaded": not app.windowIcon().isNull(),
            "git_version": git_version,
            "git_executable": find_git(),
            "libraries": libraries, "preview": str(preview), "network_tests_run": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    finally:
        window.close()
        app.processEvents()


def run_cancel_smoke(app, repo: Path | None, report: Path) -> int:
    """Exercise the real GUI/worker/job lifecycle without executing zapret."""
    import threading
    import time
    from PySide6.QtCore import QTimer
    from .controller import TestOutcome
    from .results import ReportParser
    from .windows import ProcessJob, powershell_path

    if repo is None:
        raise ValueError("--smoke-cancel requires --repo; no BAT files will be executed")
    report.parent.mkdir(parents=True, exist_ok=True)
    window = MainWindow(Store(report.parent / "cancel-smoke-state"), repo)
    started = threading.Event()
    observations = []
    stage = [0]
    deadline = time.monotonic() + 15

    def harmless_test(repository, progress, control):
        job = ProcessJob()
        log = report.parent / f"cancel-helper-{len(observations)}.log"
        try:
            job.start([str(powershell_path()), "-NoLogo", "-NoProfile", "-Command", "Start-Sleep -Seconds 30"],
                      report.parent, log)
            started.set()
            if not control.cancel.wait(10):
                raise RuntimeError("GUI did not request cancellation")
        finally:
            job.stop()
        observations.append({"closing": control.closing.is_set(), "job_empty": not job.pids()})
        return TestOutcome(ReportParser(), log, {}, False, "Проверка остановлена", True, True)

    window.controller.run_tests = harmless_test
    timer = QTimer(window)
    def advance():
        if time.monotonic() > deadline:
            window.close()
            return
        if stage[0] == 0 and started.is_set():
            if not window.stop_button.isEnabled():
                raise RuntimeError("Cancel button is disabled during a sweep")
            window.stop_button.click()
            stage[0] = 1
        elif stage[0] == 1 and window.worker is None:
            if not window.isVisible():
                raise RuntimeError("Cancellation unexpectedly closed the window")
            started.clear()
            window.start_tests()
            stage[0] = 2
        elif stage[0] == 2 and started.is_set():
            window.close()
            stage[0] = 3
    timer.timeout.connect(advance)
    timer.start(20)
    window.show()
    window.start_tests()
    app.exec()
    ok = stage[0] == 3 and observations == [{"closing": False, "job_empty": True}, {"closing": True, "job_empty": True}]
    report.write_text(json.dumps({"ok": ok, "frozen": bool(getattr(sys, "frozen", False)),
                                 "version": __version__, "admin": is_admin(), "observations": observations,
                                 "window_closed": not window.isVisible(), "network_tests_run": False},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1
