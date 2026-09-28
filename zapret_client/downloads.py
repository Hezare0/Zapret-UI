"""Download a clean official checkout next to the standalone application."""

from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid

from .repository import ClientError, Repository, validate_shell_path
from .storage import Store
from .updates import REMOTE_URL, find_git, run_external


@dataclass(frozen=True)
class DownloadOutcome:
    path: Path | None
    commit: str
    cancelled: bool
    message: str


def application_directory() -> Path:
    return (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])


def download_directory() -> Path:
    return application_directory() / "zapret-discord-youtube"


def download_zapret(destination: Path, store: Store, progress=lambda line: None,
                    cancel: threading.Event | None = None) -> DownloadOutcome:
    destination = destination.resolve()
    if destination != download_directory().resolve():
        raise ClientError("Скачивание разрешено в папку zapret-discord-youtube рядом с клиентом.")
    validate_shell_path(destination)
    if destination.exists():
        raise ClientError(f"Папка уже существует: {destination}. Выбери её через «Выбрать папку».")
    if cancel and cancel.is_set():
        return DownloadOutcome(None, "", True, "Скачивание отменено до запуска.")
    git = find_git()
    with tempfile.TemporaryDirectory(prefix=".zapret-downloading-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "repo"
        progress("Скачиваю официальный Flowseal / main…")
        try:
            result = run_external([git, "clone", "--depth", "1", "--no-tags", "--branch", "main",
                                   REMOTE_URL, str(staging)], cwd=destination.parent, timeout=300,
                                  log_directory=store.folder / "git-logs" / uuid.uuid4().hex, cancel=cancel)
        except ClientError:
            if cancel and cancel.is_set():
                return DownloadOutcome(None, "", True, "Скачивание отменено; незавершённая загрузка убрана.")
            raise
        if result.returncode:
            raise ClientError("Не удалось скачать официальный репозиторий: " +
                              (result.stderr or result.stdout).strip()[-1600:])
        if cancel and cancel.is_set():
            return DownloadOutcome(None, "", True, "Скачивание отменено; незавершённая загрузка убрана.")
        checkout = Repository(staging)
        checkout.validate_run()
        revision = run_external([git, "-C", str(staging), "rev-parse", "HEAD"],
                                cwd=destination.parent, timeout=30,
                                log_directory=store.folder / "git-logs" / uuid.uuid4().hex, cancel=cancel)
        if revision.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", revision.stdout.strip()):
            raise ClientError("Скачанная папка не содержит корректную ревизию Git.")
        if cancel and cancel.is_set():
            return DownloadOutcome(None, "", True, "Скачивание отменено; незавершённая загрузка убрана.")
        if destination.exists():
            raise ClientError(f"Во время скачивания появилась папка {destination}. Её содержимое сохранено.")
        staging.rename(destination)
    return DownloadOutcome(destination, revision.stdout.strip(), False,
                           f"zapret {Repository(destination).version} скачан в {destination} ({revision.stdout.strip()[:7]}).")
