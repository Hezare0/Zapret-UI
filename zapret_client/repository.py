"""Discover configurations and invalidate saved metrics when inputs change."""

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path


class ClientError(Exception):
    """An actionable error suitable for display to the user."""


def natural_key(value: str) -> str:
    return re.sub(r"\d+", lambda match: match[0].zfill(8), value.casefold())


def validate_shell_path(path: Path) -> None:
    # Upstream batches use CALL, delayed expansion and unquoted path fragments.
    # Reject characters their own implementation cannot safely round-trip.
    if any(c in str(path) for c in '\r\n"% !&|<>^'.replace(" ", "")):
        raise ClientError("Путь содержит символы, небезопасные для upstream .bat (% ! & | < > ^). "
                          "Перенесите сборку, например, в C:\\zapret. Пробелы допустимы.")


@dataclass(frozen=True)
class Config:
    path: Path
    fingerprint: str

    @property
    def name(self) -> str:
        return self.path.name


class Repository:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir() or not (self.root / "service.bat").is_file():
            raise ClientError("Выберите распакованную папку zapret с service.bat и general*.bat.")
        self.service = self.root / "service.bat"
        self.test_script = self.root / "utils" / "test zapret.ps1"
        self.executable = self.root / "bin" / "winws.exe"
        self.configs: list[Config] = []
        self.refresh()

    @property
    def key(self) -> str:
        return hashlib.sha256(str(self.root).casefold().encode()).hexdigest()

    @property
    def version(self) -> str:
        match = re.search(r'LOCAL_VERSION=([^"\r\n]+)', self.service.read_text(encoding="utf-8-sig", errors="replace"))
        return match[1] if match else "неизвестна"

    def refresh(self) -> None:
        paths = [p for p in self.root.iterdir() if p.is_file()
                 and p.name.lower().startswith("general") and p.suffix.lower() == ".bat"]
        if not paths:
            raise ClientError("В этой папке нет general*.bat.")
        shared = hashlib.sha256()
        inputs = [self.service]
        for folder in ("lists", "bin", "utils"):
            base = self.root / folder
            if base.exists():
                inputs.extend(p for p in base.rglob("*") if p.is_file()
                              and "test results" not in p.relative_to(base).parts
                              and p.suffix.lower() in (".txt", ".bin", ".enabled", ".disabled", ".exe", ".dll", ".sys"))
        for path in sorted(set(inputs)):
            if path.is_file():
                shared.update(str(path.relative_to(self.root)).encode())
                if path.suffix.lower() in (".exe", ".dll", ".sys"):
                    info = path.stat()
                    shared.update(f"{info.st_size}:{info.st_mtime_ns}".encode())
                else:
                    shared.update(path.read_bytes())
        self.configs = [Config(path, hashlib.sha256(shared.digest() + path.read_bytes()).hexdigest())
                        for path in sorted(paths, key=lambda p: natural_key(p.name))]

    def get(self, name: str) -> Config:
        for config in self.configs:
            if config.name.casefold() == name.casefold():
                if config.path.resolve().parent != self.root:
                    raise ClientError("Конфигурация ссылается за пределы выбранной папки.")
                return config
        raise ClientError("Выбранный батник больше не существует. Обновите список.")

    def validate_run(self, name: str | None = None) -> None:
        validate_shell_path(self.root)
        if not self.executable.is_file():
            raise ClientError("Не найден bin\\winws.exe. Нужна полностью распакованная сборка zapret.")
        if not (self.root / "lists").is_dir():
            raise ClientError("Не найдена папка lists.")
        configs = [self.get(name)] if name else self.configs
        for config in configs:
            validate_shell_path(config.path)
            self.get(config.name)
