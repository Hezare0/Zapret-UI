from pathlib import Path
import time

from PySide6.QtCore import Qt

from zapret_client.gui import MainWindow
from zapret_client.results import ReportParser
from zapret_client.storage import Store
from zapret_client.updates import UpdateInfo


EXAMPLES = Path(__file__).parent / "fixtures/user-examples.txt"


def test_import_select_filter_and_reopen(app, sample_repo, tmp_path):
    store = Store(tmp_path / "state")
    window = MainWindow(store, sample_repo, preview=True)
    assert window.config_table.rowCount() == 4
    assert window.import_text(EXAMPLES.read_text()) == 2
    assert window.config_table.item(0, 2).text() == "16/16"
    assert window.config_table.item(0, 3).text() == "64.0 мс"
    assert window.config_table.item(1, 2).text() == "12/17"
    assert window.config_table.item(0, 1).text() == "Импорт"
    window.config_table.selectRow(1)
    app.processEvents()
    assert window.detail_table.rowCount() == 17
    window.search.setText("ALT3")
    assert not window.config_table.isRowHidden(1)
    assert window.config_table.isRowHidden(0)
    window.close()
    reopened = MainWindow(Store(tmp_path / "state"), preview=True)
    assert reopened.config_table.item(0, 2).text() == "16/16"
    reopened.close()


def test_dates_results_selection_and_log_survive_two_restarts(app, sample_repo, tmp_path):
    state = tmp_path / "persistent-state"
    window = MainWindow(Store(state), sample_repo)
    result = ReportParser.parse(EXAMPLES.read_text()).results["general (alt3).bat"]
    log = tmp_path / "saved.log"
    log.write_text("saved diagnostic output", encoding="utf-8")
    window.store.put(window.repo.key, result, window.repo.get(result.config).fingerprint, "live", str(log),
                     tested_at="2026-09-10T21:47:14+05:00")
    window.store.save()
    window.rebuild_table()
    window.config_table.selectRow(1)
    window.close()
    for _ in range(2):
        window = MainWindow(Store(state))
        assert window.config_table.item(1, 2).text() == "12/17"
        assert window.config_table.item(1, 4).text() == "10.09 21:47"
        assert window.selected_name() == "general (ALT3).bat"
        assert window.log.toPlainText() == "saved diagnostic output"
        assert window.open_log_button.isEnabled()
        assert "Восстановлены" in window.status.text()
        window.close()


def test_unknown_date_is_explicit_and_does_not_hide_scores(app, sample_repo, tmp_path):
    window = MainWindow(Store(tmp_path / "state"), sample_repo)
    result = ReportParser.parse(EXAMPLES.read_text()).results["general (alt).bat"]
    window.store.put(window.repo.key, result, "", "import", tested_at="invalid-date")
    window.store.save()
    window.rebuild_table()
    assert window.config_table.item(0, 2).text() == "16/16"
    assert "Дата неизвестна" in window.config_table.item(0, 4).text()
    window.close()


def test_update_button_appears_only_for_available_update(app, sample_repo, tmp_path):
    window = MainWindow(Store(tmp_path / "state"), sample_repo)
    assert window.apply_update_button.isHidden()
    info = UpdateInfo(str(sample_repo), "a" * 40, "b" * 40, "1.0", "2.0", 1, False,
                      ("general.bat",), "2026-09-10T22:00:00+05:00")
    window.operation_succeeded(info)
    window.update_controls()
    assert not window.apply_update_button.isHidden()
    assert window.apply_update_button.isEnabled()
    assert not window.update_note.isHidden()
    window.controller.active_name = "general.bat"
    window.update_controls()
    assert not window.apply_update_button.isEnabled()
    window.controller.active_name = None
    window.close()


def test_app_icon_has_all_shortcut_sizes(app):
    icon = app.windowIcon()
    assert not icon.isNull()
    for size in (16, 24, 32, 48, 64, 128, 256):
        assert not icon.pixmap(size, size).isNull()


def test_async_operation_and_disabled_controls(app, sample_repo, tmp_path):
    window = MainWindow(Store(tmp_path / "state"), sample_repo)
    window.begin("fixture", lambda progress: time.sleep(0.15))
    assert not window.start_button.isEnabled()
    assert not window.test_button.isEnabled()
    deadline = time.monotonic() + 3
    while window.worker and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.worker is None
    assert window.start_button.isEnabled()
    window.close()


def test_stale_and_incomplete_results_are_not_green(app, sample_repo, tmp_path):
    store = Store(tmp_path / "state")
    window = MainWindow(store, sample_repo)
    result = ReportParser.parse("[INFO] Targets loaded: 2\n[1/1] general.bat\nDNS Ping: 10 ms", imported=False).results["general.bat"]
    name = "general.bat"
    store.put(window.repo.key, result, window.repo.get(name).fingerprint, "incomplete")
    store.save()
    window.rebuild_table()
    row = 3
    assert window.config_table.item(row, 1).text() == "Неполный вывод"
    (sample_repo / name).write_text("echo changed")
    window.refresh_repository()
    assert window.config_table.item(row, 1).text() == "Устарел"
    window.close()
