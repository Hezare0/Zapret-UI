"""Check the requested upstream and apply only a reviewed fast-forward."""

from dataclasses import dataclass
from datetime import datetime
import ctypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid

import psutil

from .diagnostics import script_compatibility
from .repository import ClientError, Repository
from .storage import Store
from .windows import ProcessJob, winws_processes


_SPAWN_LOCK = threading.Lock()
REMOTE_URL = "https://github.com/flowseal/zapret-discord-youtube.git"


def is_official_remote(url: str) -> bool:
    return url.casefold().rstrip("/").removesuffix(".git") == REMOTE_URL.removesuffix(".git")


def find_git() -> str:
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "vendor/git/cmd/git.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("git.exe") or shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("LOCALAPPDATA", "")):
        for suffix in ("Git/cmd/git.exe", "Programs/Git/cmd/git.exe"):
            candidate = Path(base) / suffix
            if candidate.is_file():
                return str(candidate)
    raise ClientError("Для обновлений нужен Git for Windows. Запуск батников и сохранённые результаты доступны без Git.")


def run_external(command: list[str], *, cwd: Path, timeout: float = 60, log_directory: Path | None = None,
                 cancel: threading.Event | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GIT_MERGE_AUTOEDIT": "no"})
    # A frozen Qt application's SetDllDirectory is inherited by children. Git
    # must load its own libraries instead of our bundled Python/Qt libraries.
    folder = log_directory or Path(tempfile.mkdtemp(prefix="zapret-git-"))
    folder.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = folder / "stdout.log", folder / "stderr.log"
    job = ProcessJob()
    try:
        with _SPAWN_LOCK:
            bundled = getattr(sys, "_MEIPASS", None)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            if bundled and not kernel.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                job.start(command, cwd, stdout_path, env, stderr_path=stderr_path)
            finally:
                if bundled:
                    kernel.SetDllDirectoryW(str(bundled))
        deadline = time.monotonic() + timeout
        while job.poll() is None:
            if cancel and cancel.is_set():
                raise ClientError("Операция отменена. Дочерние процессы остановлены.")
            if time.monotonic() >= deadline:
                raise ClientError("Git не ответил за отведённое время. Проверь подключение к GitHub и повтори проверку.")
            time.sleep(0.05)
        return subprocess.CompletedProcess(command, job.poll(), stdout_path.read_text(encoding="utf-8", errors="replace"),
                                           stderr_path.read_text(encoding="utf-8", errors="replace"))
    finally:
        job.stop()


def assert_update_idle():
    if winws_processes():
        raise ClientError("Перед обновлением останови запущенный zapret. Обновлять исполняемые файлы во время работы нельзя.")
    try:
        status = psutil.win_service_get("zapret").status()
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise ClientError("Не удалось проверить состояние службы zapret.") from exc
    if status != "stopped":
        raise ClientError("Перед обновлением останови службу zapret.")


@dataclass(frozen=True)
class UpdateInfo:
    root: str
    current: str
    target: str
    local_version: str
    remote_version: str
    commits: int
    compatible: bool
    changed_files: tuple[str, ...]
    checked_at: str
    diagnostic_note: str = ""

    @property
    def available(self) -> bool:
        return self.current != self.target

    @property
    def summary(self) -> str:
        if not self.available:
            return f"zapret {self.local_version} · main актуальна ({self.current[:7]})"
        return f"Доступно: zapret {self.remote_version} · {self.commits} новых коммитов · {self.target[:7]}"


@dataclass(frozen=True)
class UpdateOutcome:
    info: UpdateInfo
    preserved_paths: tuple[str, ...]
    message: str


class Updater:
    def __init__(self, repo: Repository, store: Store):
        self.repo = repo
        self.store = store
        self.git = find_git()

    def command(self, *args, checked=True, timeout=60):
        result = run_external([self.git, "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
                               "-c", "maintenance.auto=false", "-c", "credential.helper=",
                               *args], cwd=self.repo.root, timeout=timeout,
                              log_directory=self.store.folder / "git-logs" / uuid.uuid4().hex)
        if checked and result.returncode:
            detail = (result.stderr or result.stdout).strip()[-2400:]
            raise ClientError("Git: " + detail)
        return result

    def validate_repository(self):
        if not (self.repo.root / ".git").is_dir():
            raise ClientError("Обновление доступно для обычной git-копии с папкой .git. Выбери папку, полученную через git clone.")
        root = self.command("rev-parse", "--show-toplevel").stdout.strip()
        if Path(root).resolve() != self.repo.root:
            raise ClientError("Выбранная папка не является корнем репозитория zapret.")
        origin = self.command("remote", "get-url", "origin").stdout.strip()
        if not is_official_remote(origin):
            raise ClientError("Обновления разрешены для origin https://github.com/flowseal/zapret-discord-youtube.git.")
        if self.command("branch", "--show-current").stdout.strip() != "main":
            raise ClientError("Автоматическое обновление поддерживается для ветки main. Текущая ветка не изменена.")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
            if (self.repo.root / ".git" / marker).exists():
                raise ClientError("В репозитории есть незавершённая операция Git. Сначала заверши её вручную.")

    def check(self, progress=lambda line: None) -> UpdateInfo:
        self.validate_repository()
        current = self.command("rev-parse", "HEAD").stdout.strip()
        progress("Проверяю изменения в GitHub / main…")
        self.command("fetch", "--no-tags", "--no-recurse-submodules", "origin", "refs/heads/main")
        target = self.command("rev-parse", "FETCH_HEAD").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", target):
            raise ClientError("Git вернул некорректный идентификатор версии.")
        ancestor = self.command("merge-base", "--is-ancestor", current, target, checked=False)
        if ancestor.returncode:
            raise ClientError("Локальная и удалённая история расходятся. Автоматическое обновление остановлено; файлы сохранены.")
        commits = int(self.command("rev-list", "--count", f"{current}..{target}").stdout.strip())
        service = self.command("show", f"{target}:service.bat").stdout
        version = re.search(r'LOCAL_VERSION=([^"\r\n]+)', service)
        script = self.command("show", f"{target}:utils/test zapret.ps1", checked=False)
        compatible, diagnostic_note = (script_compatibility(script.stdout.encode("utf-8"))
                                       if script.returncode == 0 else (False, "В новой версии нет тестового скрипта."))
        changed = tuple(filter(None, self.command("diff", "--name-only", "-z", current, target).stdout.split("\0")))
        return UpdateInfo(str(self.repo.root), current, target, self.repo.version,
                          version[1] if version else target[:7], commits, compatible, changed,
                          datetime.now().astimezone().isoformat(timespec="seconds"), diagnostic_note)

    def ensure_unchanged(self, info: UpdateInfo) -> tuple[str, ...]:
        self.validate_repository()
        if Path(info.root).resolve() != self.repo.root or not re.fullmatch(r"[0-9a-f]{40,64}", info.target):
            raise ClientError("Результат проверки относится к другому репозиторию.")
        if self.command("rev-parse", "HEAD").stdout.strip() != info.current:
            raise ClientError("Локальная версия изменилась после проверки. Проверь обновления ещё раз.")
        preserved, blocked = [], []
        for line in self.command("status", "--porcelain", "--untracked-files=all").stdout.splitlines():
            if line == " M lists/ipset-all.txt":
                preserved.append("lists/ipset-all.txt")
            else:
                blocked.append(line)
        if blocked:
            raise ClientError("Есть другие локальные изменения. Обновление остановлено, чтобы сохранить их.\n" +
                              "\n".join(blocked)[:1800])
        return tuple(preserved)

    def drop_owned_stash(self, revision: str) -> bool:
        top = self.command("rev-parse", "-q", "--verify", "refs/stash", checked=False)
        if top.returncode or top.stdout.strip() != revision:
            return False
        self.command("stash", "drop", "stash@{0}")
        return True

    def apply(self, info: UpdateInfo, progress=lambda line: None) -> UpdateOutcome:
        if not info.available:
            raise ClientError("Репозиторий уже актуален.")
        assert_update_idle()
        preserved = self.ensure_unchanged(info)
        assert_update_idle()
        self.ensure_unchanged(info)
        hooks = self.store.folder / "git-logs" / "disabled-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        original = {name: (self.repo.root / name).read_bytes() for name in preserved}
        stash_revision = None
        retained_stash = False
        if preserved:
            progress("Временно сохраняю изменённый IPSet в Git stash…")
            before = self.command("rev-parse", "-q", "--verify", "refs/stash", checked=False).stdout.strip()
            self.command("-c", "user.name=Zapret Client", "-c", "user.email=zapret-client@users.noreply.github.com",
                         "stash", "push", "-m", f"zapret-client-update-{info.current[:12]}-{info.target[:12]}",
                         "--", *preserved)
            stash_revision = self.command("rev-parse", "refs/stash").stdout.strip()
            if stash_revision == before or any((self.repo.root / name).read_bytes() == data
                                               for name, data in original.items()):
                raise ClientError("Git не сохранил изменённый IPSet. Обновление остановлено; проверь состояние stash.")
        progress(f"Обновляю main до {info.target[:7]}…")
        try:
            self.command("-c", f"core.hooksPath={hooks}", "merge", "--ff-only", "--no-edit", info.target)
            if self.command("rev-parse", "HEAD").stdout.strip() != info.target:
                raise ClientError("После обновления HEAD не совпадает с выбранной версией.")
            for name, data in original.items():
                (self.repo.root / name).write_bytes(data)
            if any((self.repo.root / name).read_bytes() != data for name, data in original.items()):
                raise ClientError("Не удалось восстановить исходные байты IPSet.")
            if stash_revision and not self.drop_owned_stash(stash_revision):
                progress("IPSet восстановлен, но временный stash остался: он перестал быть верхней записью.")
                retained_stash = True
            self.repo.refresh()
        except Exception as exc:
            if stash_revision and self.command("rev-parse", "HEAD").stdout.strip() == info.current:
                try:
                    self.command("stash", "apply", "--index", stash_revision)
                    if all((self.repo.root / name).read_bytes() == data for name, data in original.items()):
                        self.drop_owned_stash(stash_revision)
                except Exception:
                    pass  # Keep the stash: it contains the user's original data.
            raise ClientError(f"Обновление не завершено: {exc}\nПроверь IPSet и временный stash в .git.") from exc
        message = f"zapret обновлён до {self.repo.version} ({info.target[:7]})."
        if preserved:
            message += " Локальный IPSet сохранён."
            if "lists/ipset-all.txt" in info.changed_files:
                message += " Upstream тоже изменил IPSet; текущий пользовательский режим оставлен."
        if retained_stash:
            message += " Временный Git stash остался из-за параллельной операции Git; проверь его вручную."
        if not info.compatible:
            message += " " + info.diagnostic_note
        return UpdateOutcome(info, preserved, message)
