"""Blocking runtime operations; the GUI runs these in a worker thread."""

import codecs
from dataclasses import dataclass
import json
from pathlib import Path
import time
import threading
from typing import Callable

from .diagnostics import prepare_script
from .repository import ClientError, Repository
from .results import ReportParser
from .storage import Store
from .windows import ProcessJob, batch_command, check_conflicts, powershell_path, winws_processes


class OperationCancelled(ClientError):
    pass


class TestControl:
    __test__ = False

    def __init__(self):
        self.cancel = threading.Event()
        self.closing = threading.Event()

    def request(self, *, closing=False):
        if closing:
            self.closing.set()
        self.cancel.set()


@dataclass
class TestOutcome:
    __test__ = False
    parser: ReportParser
    log: Path
    fingerprints: dict[str, str]
    successful: bool
    message: str
    cancelled: bool = False
    cleanup_ok: bool = True


class Controller:
    def __init__(self, store: Store):
        self.store = store
        self.job: ProcessJob | None = None
        self.active_name: str | None = None
        self.active_repo: Repository | None = None
        self.last_log: Path | None = None
        self.test_job: ProcessJob | None = None

    def owned_pids(self) -> set[int]:
        return set().union(*(job.pids() for job in (self.job, self.test_job) if job))

    @property
    def cleanup_pending(self) -> bool:
        return self.test_job is not None

    def running(self) -> bool:
        if not self.job or not self.active_repo:
            return False
        pids = self.owned_pids()
        expected = self.active_repo.executable.resolve()
        return any(p["pid"] in pids and Path(p["exe"]).resolve() == expected for p in winws_processes())

    def stop(self):
        if self.test_job:
            self.test_job.stop()
            self.test_job = None
        if self.job:
            self.job.stop()
        self.job = None
        self.active_name = None
        self.active_repo = None

    def start(self, repo: Repository, name: str, *, cancel: threading.Event | None = None) -> str:
        if cancel and cancel.is_set():
            raise OperationCancelled("Запуск отменён.")
        repo.refresh()
        repo.validate_run(name)
        check_conflicts(self.owned_pids())
        if self.active_name == name and self.active_repo and self.active_repo.root == repo.root and self.running():
            return name
        self.stop()
        # Recheck after stopping to catch a competing process before starting.
        check_conflicts()
        folder = self.store.new_run_folder()
        self.last_log = folder / "launch.log"
        job = ProcessJob()
        try:
            job.start(batch_command(repo.get(name).path), repo.root, self.last_log, {"NO_UPDATE_CHECK": "1"})
            deadline = time.monotonic() + 10
            stable_since = None
            while time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    raise OperationCancelled("Запуск отменён.")
                pids = job.pids()
                matches = [p for p in winws_processes()
                           if p["pid"] in pids and Path(p["exe"]).resolve() == repo.executable.resolve()]
                if matches:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 1:
                        self.job, self.active_name, self.active_repo = job, name, repo
                        return name
                else:
                    stable_since = None
                if job.poll() not in (None, 0) or (job.poll() is not None and not pids):
                    break
                time.sleep(0.1)
            output = self.last_log.read_text(encoding="utf-8-sig", errors="replace")[-2500:]
            raise ClientError(f"{name}: winws.exe не запустился или сразу завершился.\n\n{output}\nЛог: {self.last_log}")
        except Exception:
            job.stop()
            raise

    def run_tests(self, repo: Repository, on_line: Callable[[str, ReportParser], None],
                  control: TestControl | None = None) -> TestOutcome:
        control = control or TestControl()
        repo.refresh()
        repo.validate_run()
        check_conflicts(self.owned_pids())
        ipset = repo.root / "lists" / "ipset-all.txt"
        if not ipset.is_file():
            raise ClientError("Не найден lists\\ipset-all.txt. Автоматические тесты требуют полного комплекта файлов.")
        if (repo.root / "ipset_switched.flag").exists():
            raise ClientError("Обнаружен ipset_switched.flag от незавершённого upstream-теста. "
                              "Сначала восстановите ipset штатным service.bat → Run Tests.")
        folder = self.store.new_run_folder()
        script = prepare_script(repo.test_script, folder)
        original_ipset = ipset.read_bytes()
        (folder / "ipset-all.before.txt").write_bytes(original_ipset)
        previous = self.active_name if self.active_repo and self.active_repo.root == repo.root else None
        fingerprints = {c.name.casefold(): c.fingerprint for c in repo.configs}
        (folder / "snapshot.json").write_text(json.dumps({
            "repository": str(repo.root), "version": repo.version, "active_config": previous,
            "fingerprints": fingerprints,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log = folder / "diagnostics.log"
        parser = ReportParser()
        failures = []
        job = None
        code = None
        stopped = False
        cancelled = False
        cleanup_ok = True
        try:
            if control.cancel.is_set():
                cancelled = True
                raise OperationCancelled("Проверка отменена до запуска.")
            self.stop()
            stopped = True
            check_conflicts()
            job = ProcessJob()
            self.test_job = job
            job.start([str(powershell_path()), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", str(script)], repo.root, log,
                      {"ZAPRET_CLIENT_ROOT": str(repo.root), "ZAPRET_CLIENT_JOB": job.name,
                       "NO_UPDATE_CHECK": "1"})
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending = ""
            checkpointed = set()

            def emit(line):
                parser.feed(line)
                if "Running tests..." in line and parser.current:
                    repo.refresh()
                    fingerprints[parser.current.config.casefold()] = repo.get(parser.current.config).fingerprint
                # Commit each completed configuration before waiting for the
                # remaining sweep. A later crash must not discard its results.
                for key, result in parser.results.items():
                    if result.complete and key not in checkpointed and key in fingerprints:
                        self.store.put(repo.key, result, fingerprints[key], "checkpoint", str(log))
                        self.store.save()
                        checkpointed.add(key)
                on_line(line, parser)

            with log.open("rb") as stream:
                while True:
                    if control.cancel.is_set() and not cancelled:
                        cancelled = True
                        on_line("[CLIENT] Останавливаю тестовые процессы и сохраняю полученные результаты…", parser)
                        job.stop()
                        self.test_job = None
                    chunk = stream.read(65536)
                    if chunk:
                        pending += decoder.decode(chunk)
                        lines = pending.split("\n")
                        pending = lines.pop()
                        for line in lines:
                            emit(line.rstrip("\r"))
                    elif cancelled or job.poll() is not None:
                        # Root exited: no further host output is expected.
                        pending += decoder.decode(b"", final=True)
                        if pending:
                            emit(pending)
                        code = -1 if cancelled else job.poll()
                        break
                    else:
                        time.sleep(0.1)
            parser.finish()
        except OperationCancelled:
            cancelled = True
        except Exception as exc:
            failures.append(str(exc))
        finally:
            if job:
                try:
                    job.stop()
                    self.test_job = None
                except Exception as exc:
                    cleanup_ok = False
                    failures.append(f"Остановка тестовых процессов: {exc}")
            # Exact bytes are an extra recovery layer on top of upstream finally.
            try:
                if not ipset.exists() or ipset.read_bytes() != original_ipset:
                    ipset.write_bytes(original_ipset)
                    on_line("[CLIENT] Исходное содержимое ipset восстановлено из слепка.", parser)
            except OSError as exc:
                cleanup_ok = False
                failures.append(f"Не удалось восстановить ipset. Слепок: {folder}: {exc}")
            if previous and stopped and not control.closing.is_set() and cleanup_ok:
                try:
                    self.start(repo, previous, cancel=control.closing)
                    on_line(f"[CLIENT] Снова включён {previous}.", parser)
                except OperationCancelled:
                    pass
                except Exception as exc:
                    failures.append(f"Не удалось снова включить {previous}: {exc}")
        expected_names = {c.name.casefold() for c in repo.configs}
        measured = set(parser.results)
        successful = (code == 0 and parser.finished and parser.saved and not parser.has_errors
                      and measured == expected_names and not failures and not cancelled)
        message = "Диагностика завершена. Результаты сохранены." if successful else (
            "Диагностика завершилась не полностью. " + " ".join(failures or [f"Код процесса: {code}. См. журнал."]))
        if cancelled:
            completed = sum(result.complete for result in parser.results.values())
            message = f"Проверка остановлена. Завершённых конфигураций: {completed}. Полученные результаты сохранены."
            if failures:
                message += " " + " ".join(failures)
        # Persist partial output as well, without replacing an earlier useful
        # result with an empty config that had only just started.
        for key, result in parser.results.items():
            if key not in fingerprints or (not result.targets and not result.launch_failed):
                continue
            checkpoint = self.store.entries(repo.key).get(key, {})
            self.store.put(repo.key, result, fingerprints[key],
                           "cancelled" if cancelled else ("live" if successful else "incomplete"), str(log),
                           tested_at=checkpoint.get("tested_at") if checkpoint.get("source") == "checkpoint" else None)
        self.store.save()
        return TestOutcome(parser, log, fingerprints, successful, message, cancelled, cleanup_ok)
