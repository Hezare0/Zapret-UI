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
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This client requires Windows 10/11")
    if args.smoke_test:
        try:
            return run(args)
        except Exception:
            import json
            import traceback
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            args.smoke_test.write_text(json.dumps({"ok": False, "traceback": traceback.format_exc()},
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
            adjacent = Path(sys.executable).resolve().parent.parent / "zapret-discord-youtube"
            if (adjacent / "service.bat").is_file():
                repo = adjacent
        window = MainWindow(store, repo)
        window.show()
        return app.exec()
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
