"""Per-repository cache, stored atomically outside the upstream distribution."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from .results import Result


def data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZapretClient"


class Store:
    def __init__(self, folder: Path | None = None):
        self.folder = folder or data_directory()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state.json"
        self.backup_path = self.folder / "state.backup.json"
        self.warning = ""
        self.valid_primary = False
        try:
            self.data = self.read_state(self.path)
            self.valid_primary = True
        except FileNotFoundError:
            self.data = {"schema": 1, "repositories": {}}
        except (ValueError, OSError, TypeError, KeyError):
            try:
                self.data = self.read_state(self.backup_path)
                self.warning = "Файл результатов повреждён. Восстановлена последняя резервная копия."
            except (ValueError, OSError, TypeError, KeyError):
                self.warning = "Файл результатов не прочитан. Его копия будет сохранена перед записью новых данных."
                self.data = {"schema": 1, "repositories": {}}

    @staticmethod
    def read_state(path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("repositories"), dict):
            raise ValueError("Unsupported cache schema")
        for entries in data["repositories"].values():
            if not isinstance(entries, dict):
                raise ValueError("Invalid repository cache")
            for entry in entries.values():
                Result.from_dict(entry["result"])
        return data

    def save(self) -> None:
        pending = self.path.with_suffix(".pending")
        with pending.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(self.data, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        if self.path.exists():
            if self.valid_primary:
                shutil.copy2(self.path, self.backup_path)
            else:
                recovery = self.folder / ("state.recovery-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
                shutil.copy2(self.path, recovery)
        pending.replace(self.path)
        self.valid_primary = True

    def entries(self, repo_key: str) -> dict:
        return self.data.setdefault("repositories", {}).setdefault(repo_key, {})

    def reload(self) -> None:
        latest = Store(self.folder)
        self.data, self.warning, self.valid_primary = latest.data, latest.warning, latest.valid_primary

    def put(self, repo_key: str, result: Result, fingerprint: str, source: str, log: str = "", *, tested_at: str | None = None) -> None:
        self.entries(repo_key)[result.config.casefold()] = {
            "result": result.to_dict(), "fingerprint": fingerprint,
            "tested_at": tested_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source, "log": log,
        }

    def new_run_folder(self) -> Path:
        path = self.folder / "logs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path.mkdir(parents=True)
        return path
