"""Entrypoint for source and PyInstaller builds."""

import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="Zapret desktop client")
    parser.add_argument("--repo", type=Path, help="Extracted zapret distribution")
    parser.add_argument("--admin", action="store_true", help="Request elevation once at startup")
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT_JSON",
                        help="Check packaged GUI startup and exit; never run BAT files")
    parser.add_argument("--smoke-cancel", type=Path, metavar="REPORT_JSON",
                        help="Check cancel/close with a harmless sleep process; requires --repo")
    parser.add_argument("--smoke-update-check", type=Path, metavar="REPORT_JSON",
                        help="Check private GitHub Release access from the packaged runtime")
    parser.add_argument("--post-update-marker", type=Path, help="Internal startup confirmation for the updater")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This client requires Windows 10/11")
    report = args.smoke_test or args.smoke_cancel or args.smoke_update_check
    if report:
        try:
            return run(args)
        except Exception:
            import json
            import traceback
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({"ok": False, "traceback": traceback.format_exc()},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
            return 1
    return run(args)


def run(args):
    from zapret_client.gui import MainWindow, create_application
    from zapret_client.storage import Store
    from zapret_client.windows import SingleInstance, elevate, is_admin
    from PySide6.QtWidgets import QMessageBox

    app = create_application()
    if args.smoke_test:
        from zapret_client.smoke import run_smoke
        return run_smoke(app, args.repo, args.smoke_test)
    if args.smoke_cancel:
        from zapret_client.smoke import run_cancel_smoke
        return run_cancel_smoke(app, args.repo, args.smoke_cancel)
    if args.smoke_update_check:
        from zapret_client.smoke import run_update_check_smoke
        return run_update_check_smoke(args.smoke_update_check)
    if args.admin and not is_admin():
        try:
            elevate(sys.argv[1:])
            return 0
        except Exception as exc:
            QMessageBox.warning(None, "Zapret Client", str(exc))
            return 1
    guard = SingleInstance()
    if guard.already_running:
        QMessageBox.information(None, "Zapret Client", "Клиент уже запущен. Открой существующее окно.")
        guard.close()
        return 0
    try:
        store = Store()
        repo = args.repo
        if not repo and not store.data.get("last_repository") and getattr(sys, "frozen", False):
            location = Path(sys.executable).resolve().parent
            for adjacent in (location / "zapret-discord-youtube", location.parent / "zapret-discord-youtube"):
                if (adjacent / "service.bat").is_file():
                    repo = adjacent
                    break
        window = MainWindow(store, repo)
        window.show()
        if args.post_update_marker:
            from PySide6.QtCore import QTimer
            def confirm_start():
                if window.isVisible() and window.winId():
                    args.post_update_marker.parent.mkdir(parents=True, exist_ok=True)
                    args.post_update_marker.write_text("ready", encoding="ascii")
            QTimer.singleShot(700, confirm_start)
        return app.exec()
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
