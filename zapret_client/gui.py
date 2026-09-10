"""PySide6 desktop interface. All process operations run outside the UI thread."""

from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__
from .controller import Controller, TestOutcome
from .repository import ClientError, Repository
from .results import ReportParser, Result
from .storage import Store
from .updates import Updater, UpdateInfo, UpdateOutcome
from .windows import elevate, is_admin


GREEN = "#77e0b9"
RED = "#ff929a"
AMBER = "#ecc17d"
MUTED = "#93a0b6"

STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 13px; color: #e4eaf3; }
QMainWindow, QWidget#root { background: #111720; }
QFrame#card, QFrame#source, QFrame#detail { background: #19212e; border: 1px solid #2b3647; border-radius: 10px; }
QLabel { background: transparent; }
QLabel#title { font-size: 29px; font-weight: 700; }
QLabel#eyebrow { color: #77e0b9; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
QLabel#muted { color: #93a0b6; }
QLabel#metric { font-size: 23px; font-weight: 600; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#notice { background: #243b36; color: #9ce6c6; border-radius: 6px; padding: 9px; }
QPushButton { background: #263246; border: 1px solid #35435a; border-radius: 7px; padding: 9px 15px; font-weight: 600; }
QPushButton:hover { background: #33425a; border-color: #506482; }
QPushButton:pressed { background: #1c283a; }
QPushButton:disabled { background: #1c2431; color: #617087; border-color: #293345; }
QPushButton#primary { background: #80e2bb; color: #10261e; border-color: #80e2bb; }
QPushButton#primary:hover { background: #a4f0d0; }
QPushButton#primary:disabled { background: #293e38; color: #6a8c7e; border-color: #293e38; }
QPushButton#danger { color: #ffa0a6; }
QPushButton#quiet { background: transparent; color: #afbdd0; }
QPushButton#danger:disabled, QPushButton#quiet:disabled { color: #617087; border-color: #293345; }
QLineEdit { background: #19212e; border: 1px solid #35435a; border-radius: 7px; padding: 9px 12px; selection-background-color: #3c5d72; }
QLineEdit:focus { border-color: #77e0b9; }
QTableWidget { background: #151e2b; alternate-background-color: #182231; border: 1px solid #2b3647; border-radius: 7px; gridline-color: #253145; outline: none; }
QTableWidget::item { padding: 7px; border: none; }
QTableWidget::item:selected { background: #2b4353; color: #ffffff; }
QHeaderView::section { background: #202b3b; color: #a7b6cb; border: none; padding: 10px 8px; font-size: 12px; font-weight: 600; }
QTableCornerButton::section { background: #202b3b; border: none; }
QScrollBar:vertical { background: #151e2b; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3b4a61; min-height: 28px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background: #111720; width: 12px; }
QPlainTextEdit { background: #101823; color: #b8c7d8; border: 1px solid #2b3647; border-radius: 7px; padding: 8px; font-family: Consolas; font-size: 12px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #8fa0b8; padding: 9px 14px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #80e2bb; border-bottom-color: #80e2bb; }
QProgressBar { background: #263246; color: #c5d1e2; border: none; border-radius: 4px; height: 6px; }
QProgressBar::chunk { background: #77e0b9; border-radius: 4px; }
QToolTip { background: #263246; color: #edf3fb; border: 1px solid #506482; padding: 5px; }
QMessageBox, QFileDialog { background: #19212e; }
"""


def label(text: str, name: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, callback, name: str = "") -> QPushButton:
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(callback)
    return widget


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().setVisible(False)
    widget.verticalHeader().setDefaultSectionSize(43)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.setShowGrid(False)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str, object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            def progress(line, parser=None):
                if parser is None:
                    self.progress.emit(line, {})
                    return
                self.progress.emit(line, {
                    "results": {key: value.to_dict() for key, value in parser.results.items()},
                    "index": parser.index, "count": parser.config_count,
                    "current": parser.current.config if parser.current else None,
                })
            self.succeeded.emit(self.operation(progress))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, store: Store, repository: Path | None = None, *, preview: bool = False):
        super().__init__()
        self.store = store
        self.controller = Controller(store)
        self.repo: Repository | None = None
        self.worker: Worker | None = None
        self.operation = ""
        self.live_results: dict[str, Result] = {}
        self.test_current: str | None = None
        self.last_log: Path | None = None
        self.loaded_log = ""
        self.update_info: UpdateInfo | None = None
        self.preview = preview
        self.setWindowTitle(f"Zapret Client · {__version__}" + (" · ТЕСТОВЫЕ ДАННЫЕ" if preview else ""))
        self.resize(1320, 930)
        self.setMinimumSize(1040, 760)
        self._build()
        initial = repository or store.data.get("last_repository")
        if initial:
            try:
                self.load_repository(Path(initial))
            except (ClientError, OSError) as exc:
                self.status.setText(str(exc))
        if store.warning:
            self.status.setText(store.warning)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_runtime)
        self.timer.start(1500)
        self.update_controls()

    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(17)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("ZAPRET  /  DESKTOP", "eyebrow"))
        titles.addWidget(label("Выбери рабочую конфигурацию", "title"))
        titles.addWidget(label("Запуск и проверка стратегий Discord / YouTube", "muted"))
        head.addLayout(titles)
        head.addStretch()
        self.admin_button = button("Права администратора", self.request_admin, "quiet")
        head.addWidget(self.admin_button, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        source = QFrame()
        source.setObjectName("source")
        row = QHBoxLayout(source)
        row.setContentsMargins(16, 10, 12, 10)
        self.source_label = label("Выбери распакованную папку zapret", "muted")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.source_label, 1)
        self.folder_button = button("Выбрать папку", self.choose_repository)
        self.refresh_button = button("Обновить список", self.refresh_repository, "quiet")
        row.addWidget(self.folder_button)
        row.addWidget(self.refresh_button)
        layout.addWidget(source)

        update_box = QFrame()
        update_box.setObjectName("source")
        update_layout = QVBoxLayout(update_box)
        update_layout.setContentsMargins(16, 10, 12, 10)
        update_row = QHBoxLayout()
        self.update_label = label("Обновления репозитория · main", "muted", wrap=True)
        update_row.addWidget(self.update_label, 1)
        self.check_update_button = button("Проверить обновления", self.check_updates)
        self.apply_update_button = button("Обновить zapret", self.apply_update, "primary")
        self.apply_update_button.setVisible(False)
        update_row.addWidget(self.check_update_button)
        update_row.addWidget(self.apply_update_button)
        update_layout.addLayout(update_row)
        self.update_note = label("", "muted", wrap=True)
        self.update_note.setVisible(False)
        update_layout.addWidget(self.update_note)
        layout.addWidget(update_box)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.count_metric = self.add_card(cards, "КОНФИГУРАЦИИ", "—", "general*.bat из выбранной папки")
        self.active_metric = self.add_card(cards, "СЕЙЧАС РАБОТАЕТ", "Не запущен", "Контроль процесса winws.exe")
        self.tested_metric = self.add_card(cards, "РЕЗУЛЬТАТЫ ПРОВЕРКИ", "0 / 0", "HTTP · TLS 1.2 · TLS 1.3 · ping")
        layout.addLayout(cards)

        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти конфигурацию…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        actions.addWidget(self.search, 1)
        self.import_button = button("Импорт отчёта", self.import_report, "quiet")
        self.test_button = button("Проверить все", self.start_tests)
        actions.addWidget(self.import_button)
        actions.addWidget(self.test_button)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.config_table = table(["Конфигурация", "Состояние", "Цели", "Ø ping", "Проверка"])
        self.config_table.setColumnWidth(0, 250)
        self.config_table.setColumnWidth(1, 146)
        self.config_table.setColumnWidth(2, 66)
        self.config_table.setColumnWidth(3, 81)
        self.config_table.itemSelectionChanged.connect(self.show_details)
        splitter.addWidget(self.config_table)

        details = QFrame()
        details.setObjectName("detail")
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(16, 16, 16, 14)
        detail_layout.setSpacing(10)
        detail_layout.addWidget(label("ВЫБРАННАЯ КОНФИГУРАЦИЯ", "eyebrow"))
        self.detail_title = label("Пока ничего не выбрано", "section", wrap=True)
        detail_layout.addWidget(self.detail_title)
        self.detail_metrics = label("—", "metric")
        detail_layout.addWidget(self.detail_metrics)
        self.detail_note = label("Выбери строку слева, чтобы увидеть результаты по целям.", "muted", wrap=True)
        detail_layout.addWidget(self.detail_note)
        self.detail_table = table(["Цель", "Результат", "Ping"])
        self.detail_table.setColumnWidth(0, 152)
        self.detail_table.setColumnWidth(1, 83)
        self.detail_table.verticalHeader().setDefaultSectionSize(34)
        self.detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        detail_layout.addWidget(self.detail_table, 1)
        self.detail_warnings = label("", "muted", wrap=True)
        detail_layout.addWidget(self.detail_warnings)
        splitter.addWidget(details)
        splitter.setSizes([810, 400])
        layout.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        self.status = label("Подключи папку zapret, затем выбери батник или запусти проверку.", "muted", wrap=True)
        bottom.addWidget(self.status, 1)
        self.stop_button = button("Остановить", self.stop_active, "danger")
        self.start_button = button("Включить выбранную", self.start_selected, "primary")
        bottom.addWidget(self.stop_button)
        bottom.addWidget(self.start_button)
        layout.addLayout(bottom)

        log_header = QHBoxLayout()
        self.log_toggle = button("Показать журнал", self.toggle_log, "quiet")
        self.open_log_button = button("Открыть файл журнала", self.open_log, "quiet")
        log_header.addWidget(self.log_toggle)
        log_header.addStretch()
        log_header.addWidget(label(f"v{__version__}   ·   " + ("ТЕСТОВЫЕ ДАННЫЕ" if self.preview else "Windows"), "muted"))
        log_header.addWidget(self.open_log_button)
        layout.addLayout(log_header)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(10000)
        self.log.setFixedHeight(165)
        self.log.setVisible(False)
        layout.addWidget(self.log)

    def add_card(self, row, title, value, note):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(17, 14, 17, 14)
        layout.setSpacing(5)
        layout.addWidget(label(title, "muted"))
        metric = label(value, "metric")
        metric.setMinimumWidth(0)
        layout.addWidget(metric)
        layout.addWidget(label(note, "muted"))
        row.addWidget(frame, 1)
        return metric

    def selected_name(self) -> str | None:
        row = self.config_table.currentRow()
        item = self.config_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def load_repository(self, path: Path):
        self.repo = Repository(path)
        self.live_results.clear()
        self.update_info = None
        self.loaded_log = ""
        self.update_label.setText(f"zapret {self.repo.version} · обновления main не проверены")
        self.update_note.setVisible(False)
        self.store.data["last_repository"] = str(self.repo.root)
        self.store.data.setdefault("repository_paths", {})[self.repo.key] = str(self.repo.root)
        self.store.save()
        self.source_label.setText(str(self.repo.root))
        self.source_label.setToolTip(f"zapret {self.repo.version}\n{self.repo.root}")
        self.status.setText("Выбери конфигурацию для запуска. Для сравнения доступности нажми «Проверить все».")
        self.rebuild_table()
        restored = sum(c.name.casefold() in self.store.entries(self.repo.key) for c in self.repo.configs)
        if restored:
            self.status.setText(f"Восстановлены результаты {restored} конфигураций. Даты и журналы доступны в списке.")

    def choose_repository(self):
        path = QFileDialog.getExistingDirectory(self, "Папка с service.bat", str(self.repo.root if self.repo else Path.home()))
        if path:
            try:
                self.load_repository(Path(path))
            except Exception as exc:
                self.show_error(str(exc))

    def refresh_repository(self):
        if self.repo:
            try:
                self.repo.refresh()
                self.store.reload()
                self.rebuild_table()
                self.status.setText("Список обновлён. Результаты для изменённых файлов помечены устаревшими.")
            except Exception as exc:
                self.show_error(str(exc))

    def result_for(self, name: str):
        if not self.repo:
            return None, None
        key = name.casefold()
        entry = self.store.entries(self.repo.key).get(key)
        if key in self.live_results:
            return self.live_results[key], {"source": "running", "tested_at": "", "fingerprint": ""}
        if entry:
            try:
                return Result.from_dict(entry["result"]), entry
            except (KeyError, TypeError, ValueError, AttributeError):
                return None, None
        return None, None

    def result_state(self, name, result, entry):
        if self.operation == "tests":
            if name == self.test_current:
                return "Проверяется…", AMBER
            if name.casefold() not in self.live_results:
                return "Ожидает теста", MUTED
        if self.controller.active_name == name:
            return "● Работает", GREEN
        if not result or not entry:
            return "Не проверен", MUTED
        if entry["source"] == "import":
            return "Импорт", MUTED
        if entry["source"] != "running" and entry.get("fingerprint") != self.repo.get(name).fingerprint:
            return "Устарел", AMBER
        if entry["source"] == "checkpoint":
            return "Прогон прерван", AMBER
        if result.launch_failed:
            return "Ошибка запуска", RED
        if entry["source"] == "incomplete" or not result.complete or len(result.targets) < result.total:
            return "Неполный вывод", AMBER
        if result.total and result.passed == result.total:
            return "Все цели доступны", GREEN
        return "Есть ошибки", RED

    def rebuild_table(self):
        previous = self.selected_name()
        if self.repo:
            previous = self.store.data.get("selected_configs", {}).get(self.repo.key, previous)
        self.config_table.blockSignals(True)
        configs = self.repo.configs if self.repo else []
        self.config_table.setRowCount(len(configs))
        checked = 0
        for row, config in enumerate(configs):
            result, entry = self.result_for(config.name)
            state, color = self.result_state(config.name, result, entry)
            if result:
                checked += 1
            stamp = "Дата неизвестна" if entry and entry["source"] != "running" else "—"
            if entry and entry.get("tested_at"):
                try:
                    stamp = datetime.fromisoformat(entry["tested_at"]).strftime("%d.%m %H:%M")
                except ValueError:
                    pass
            if entry and entry["source"] == "import":
                stamp = "Импорт " + stamp
            values = [config.name, state,
                      f"{result.passed}/{result.total}" if result and result.total else "—",
                      f"{result.average_ping:.1f} мс" if result and result.average_ping is not None else "—", stamp]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, config.name)
                    item.setToolTip(str(config.path))
                if col in (1, 2):
                    item.setForeground(QColor(color))
                if col in (2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 3 and result:
                    item.setToolTip(f"Среднее по {result.ping_samples} числовым ответам; Timeout исключён. "
                                    "Для <1 ms берётся верхняя граница 1 мс.")
                self.config_table.setItem(row, col, item)
            if config.name == previous:
                self.config_table.selectRow(row)
        if configs and previous not in {c.name for c in configs}:
            self.config_table.selectRow(0)
        self.config_table.blockSignals(False)
        self.filter_rows()
        self.count_metric.setText(str(len(configs)) if configs else "—")
        self.active_metric.setText(self.controller.active_name.removesuffix(".bat") if self.controller.active_name else "Не запущен")
        self.active_metric.setToolTip(self.controller.active_name or "Клиент не запускал winws.exe")
        self.active_metric.setStyleSheet(f"color: {GREEN if self.controller.active_name else MUTED}; font-size: 19px;")
        self.tested_metric.setText(f"{checked} / {len(configs)}")
        self.show_details()
        self.update_controls()

    def filter_rows(self):
        query = self.search.text().casefold().strip()
        for row in range(self.config_table.rowCount()):
            self.config_table.setRowHidden(row, query not in self.config_table.item(row, 0).text().casefold())

    def show_details(self):
        name = self.selected_name()
        if name and self.repo:
            selections = self.store.data.setdefault("selected_configs", {})
            if selections.get(self.repo.key) != name:
                selections[self.repo.key] = name
                if not self.worker:
                    try:
                        self.store.save()
                    except OSError as exc:
                        self.status.setText(f"Не удалось сохранить выбранную конфигурацию: {exc}")
        self.detail_title.setText(name or "Пока ничего не выбрано")
        result, entry = self.result_for(name) if name else (None, None)
        self.detail_table.setRowCount(0)
        self.detail_warnings.setText("")
        if not result:
            self.detail_metrics.setText("Нет результатов")
            self.detail_note.setText("Запусти проверку всех конфигураций или импортируй отчёт service.bat.")
        else:
            ping = f"{result.average_ping:.1f} мс" if result.average_ping is not None else "нет ping"
            self.detail_metrics.setText(f"{result.passed}/{result.total}   ·   {ping}")
            note = f"Средний ping по {result.ping_samples} ответам. Timeout исключён."
            if entry["source"] == "import":
                note = "Импортированный отчёт: дата исходного теста и соответствие текущим файлам не подтверждены.\n" + note
            elif entry["source"] in ("incomplete", "checkpoint") or not result.complete:
                note = "Прогон не завершён — результат предварительный.\n" + note
            elif entry["source"] != "running" and entry.get("fingerprint") != self.repo.get(name).fingerprint:
                note = "Файлы изменились после теста. Запусти проверку заново.\n" + note
            self.detail_note.setText(note)
            if not self.worker and entry.get("log"):
                path = Path(entry["log"])
                if path.is_file():
                    self.last_log = path
                    if self.loaded_log != str(path):
                        try:
                            with path.open("rb") as stream:
                                stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                                self.log.setPlainText(stream.read().decode("utf-8-sig", errors="replace"))
                            self.loaded_log = str(path)
                        except OSError as exc:
                            self.log.setPlainText(f"Журнал не прочитан: {exc}")
            self.detail_table.setRowCount(len(result.targets))
            for row, target in enumerate(result.targets.values()):
                values = [target.name, "OK" if target.ok else "Ошибка", target.ping_text]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(target.reason + "\n" + " · ".join(f"{k}: {v}" for k, v in target.protocols.items()))
                    if col == 1:
                        item.setForeground(QColor(GREEN if target.ok else RED))
                    if col == 2 and target.ping_ms is None:
                        item.setForeground(QColor(AMBER))
                    self.detail_table.setItem(row, col, item)
            missing = result.total - len(result.targets)
            notes = []
            if missing:
                notes.append(f"Нет строк для {missing} целей; они не считаются успешными.")
            if result.warnings:
                notes.append(f"Предупреждений в журнале: {len(result.warnings)}.")
                self.detail_warnings.setToolTip("\n".join(result.warnings))
            self.detail_warnings.setText(" ".join(notes))
        self.update_controls()

    def update_controls(self):
        busy = self.worker is not None
        has_repo = self.repo is not None
        active = self.controller.active_name is not None
        self.folder_button.setEnabled(not busy and not active)
        self.refresh_button.setEnabled(not busy and has_repo)
        self.test_button.setEnabled(not busy and has_repo and not self.preview)
        self.check_update_button.setEnabled(not busy and has_repo and not self.preview)
        available = bool(self.update_info and self.update_info.available)
        self.apply_update_button.setVisible(available)
        self.apply_update_button.setEnabled(available and not busy and not active and not self.preview)
        self.apply_update_button.setToolTip("Сначала останови активную конфигурацию." if active else
                                           "Создать резервную копию и применить проверенный commit через fast-forward.")
        self.import_button.setEnabled(not busy and has_repo)
        self.start_button.setEnabled(not busy and bool(self.selected_name()) and not self.preview)
        self.stop_button.setEnabled(not busy and active)
        self.admin_button.setText("● Администратор" if is_admin() else "Права администратора")
        self.admin_button.setEnabled(not busy and not active and not is_admin() and not self.preview)
        self.open_log_button.setEnabled(bool(self.last_log and self.last_log.is_file()))

    def check_updates(self):
        if not self.repo:
            return
        self.update_info = None
        self.update_label.setText("Проверяю обновления main…")
        self.update_note.setVisible(False)
        self.status.setText("Проверяю GitHub. Рабочие файлы репозитория остаются на текущей версии.")
        self.begin("check_updates", lambda progress: Updater(self.repo, self.store).check(progress))

    def apply_update(self):
        if not self.repo or not self.update_info or self.controller.active_name:
            return
        info = self.update_info
        self.status.setText("Сохраняю резервную копию перед обновлением…")
        self.begin("apply_update", lambda progress: Updater(self.repo, self.store).apply(info, progress))

    def request_admin(self):
        try:
            args = ["--admin"]
            if self.repo:
                args += ["--repo", str(self.repo.root)]
            elevate(args)
            self.close()
        except ClientError as exc:
            self.show_error(str(exc))

    def begin(self, kind, operation):
        if self.worker:
            return
        self.operation = kind
        self.worker = Worker(operation, self)
        self.worker.succeeded.connect(self.operation_succeeded)
        self.worker.failed.connect(self.show_error)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.operation_finished)
        self.progress.setRange(0, 0)
        self.update_controls()
        self.worker.start()

    def start_selected(self):
        name = self.selected_name()
        if self.repo and name:
            self.status.setText(f"Запускаю {name}…")
            self.begin("start", lambda progress: self.controller.start(self.repo, name))

    def stop_active(self):
        self.status.setText("Останавливаю процессы выбранной конфигурации…")
        self.begin("stop", lambda progress: self.controller.stop())

    def start_tests(self):
        if not self.repo:
            return
        self.live_results.clear()
        self.test_current = None
        self.log.clear()
        self.status.setText("Проверка всех конфигураций. Окно можно свернуть; закрытие доступно после восстановления состояния.")
        self.begin("tests", lambda progress: self.controller.run_tests(self.repo, progress))
        self.rebuild_table()

    def on_progress(self, line, snapshot):
        self.log.appendPlainText(line)
        if self.operation != "tests":
            self.status.setText(line)
            return
        self.live_results = {key: Result.from_dict(value) for key, value in snapshot["results"].items()}
        self.test_current = snapshot["current"]
        if snapshot["count"]:
            self.progress.setRange(0, snapshot["count"])
            self.progress.setValue(max(0, snapshot["index"] - 1))
            self.status.setText(f"Проверка {snapshot['index']}/{snapshot['count']} · {self.test_current or 'восстановление состояния'}")
        self.rebuild_table()

    def operation_succeeded(self, value):
        try:
            if isinstance(value, UpdateInfo):
                self.update_info = value
                self.update_label.setText(value.summary)
                self.update_label.setToolTip("\n".join(value.changed_files))
                self.update_note.setText("После обновления новый тестовый скрипт потребует обновления клиента. Импорт отчётов останется доступен.")
                self.update_note.setVisible(value.available and not value.compatible)
                self.status.setText("Проверка завершена. " + ("Доступна кнопка обновления." if value.available else "Установлена последняя ревизия main."))
            elif isinstance(value, UpdateOutcome):
                self.update_info = None
                self.update_label.setText(f"zapret {self.repo.version} · main обновлена ({value.info.target[:7]})")
                self.update_note.setText("Новый тестовый скрипт пока не поддерживается. Для результатов используй импорт отчёта service.bat.")
                self.update_note.setVisible(not value.info.compatible)
                self.status.setText(value.message)
                self.log.appendPlainText(value.message)
            elif isinstance(value, TestOutcome):
                self.last_log = value.log
                for result in value.parser.results.values():
                    if result.config.casefold() not in value.fingerprints:
                        continue
                    checkpoint = self.store.entries(self.repo.key).get(result.config.casefold(), {})
                    self.store.put(self.repo.key, result, value.fingerprints[result.config.casefold()],
                                   "live" if value.successful else "incomplete", str(value.log),
                                   tested_at=checkpoint.get("tested_at") if checkpoint.get("source") == "checkpoint" else None)
                self.store.save()
                self.status.setText(value.message)
                self.log.appendPlainText("[CLIENT] " + value.message)
                if not value.successful:
                    self.log.setVisible(True)
                    self.log_toggle.setText("Скрыть журнал")
            elif self.operation == "start":
                self.last_log = self.controller.last_log
                self.status.setText(f"Работает {value}. Процесс winws.exe запущен и проверен.")
            elif self.operation == "stop":
                self.status.setText("Конфигурация остановлена.")
        except Exception as exc:
            self.show_error(f"Результат получен, но не удалось сохранить кэш: {exc}")

    def operation_finished(self):
        worker = self.worker
        if self.operation == "apply_update":
            self.update_info = None
        self.worker = None
        self.operation = ""
        self.live_results.clear()
        self.test_current = None
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if self.repo:
            try:
                self.repo.refresh()
            except Exception as exc:
                self.show_error(str(exc))
        self.rebuild_table()
        if worker:
            worker.deleteLater()

    def import_text(self, text: str, source: Path | None = None) -> int:
        if not self.repo:
            return 0
        parsed = ReportParser.parse(text)
        names = {c.name.casefold() for c in self.repo.configs}
        matched = 0
        for key, result in parsed.results.items():
            if key in names:
                self.store.put(self.repo.key, result, "", "import", str(source or ""))
                matched += 1
        if not matched:
            raise ClientError("В отчёте нет стандартных результатов для батников из выбранной папки.")
        self.store.save()
        self.rebuild_table()
        self.status.setText(f"Импортировано конфигураций: {matched}. Соответствие текущим файлам не подтверждено.")
        return matched

    def import_report(self):
        if not self.repo:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Отчёт service.bat", str(self.repo.root / "utils" / "test results"),
                                            "Отчёты (*.txt *.log *.md);;Все файлы (*)")
        if path:
            try:
                source = Path(path)
                data = source.read_bytes()
                encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                self.import_text(data.decode(encoding, errors="replace"), source)
            except Exception as exc:
                self.show_error(str(exc))

    def show_error(self, text):
        self.status.setText(text.split("\n")[0])
        self.log.appendPlainText("[CLIENT ERROR] " + text)
        QMessageBox.warning(self, "Не удалось выполнить действие", text)

    def poll_runtime(self):
        if self.worker or not self.controller.active_name:
            return
        try:
            if not self.controller.running():
                self.status.setText("winws.exe завершился вне клиента. Завершаю оставшиеся процессы…")
                self.begin("lost", lambda progress: self.controller.stop())
        except Exception as exc:
            self.status.setText(f"Не удалось проверить процесс: {exc}")

    def toggle_log(self):
        visible = not self.log.isVisible()
        self.log.setVisible(visible)
        self.log_toggle.setText("Скрыть журнал" if visible else "Показать журнал")

    def open_log(self):
        if self.last_log and self.last_log.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_log)))

    def closeEvent(self, event):
        if self.worker:
            self.status.setText("Дождись завершения операции: клиент должен восстановить состояние и сохранить результаты.")
            event.ignore()
            return
        if self.controller.active_name:
            answer = QMessageBox.question(self, "Завершить работу?",
                                          "При закрытии клиента запущенная им конфигурация будет остановлена.",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            try:
                self.controller.stop()
            except Exception as exc:
                self.show_error(str(exc))
                event.ignore()
                return
        self.timer.stop()
        event.accept()


def create_application(argv=None):
    # A stable app identity keeps the Python host icon out of the taskbar.
    from .windows import shell
    import ctypes
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
    shell.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell.SetCurrentProcessExplicitAppUserModelID("ZapretClient.Desktop")
    app = QApplication(argv or sys.argv)
    app.setApplicationName("Zapret Client")
    app.setOrganizationName("ZapretClient")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets/zapret-client.ico")))
    return app
