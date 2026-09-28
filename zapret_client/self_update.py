"""GitHub Release checks and verified, restart-safe Windows self-update."""

from dataclasses import dataclass
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from . import __version__
from .repository import ClientError
from .storage import Store
from .updates import _SPAWN_LOCK, find_git
from .windows import powershell_path


API_RELEASE = "https://api.github.com/repos/Hezare0/Zapret-UI/releases/latest"
EXPECTED_REPO = "Hezare0/Zapret-UI"


@dataclass(frozen=True)
class UiUpdateInfo:
    current: str
    available: str
    release_url: str
    asset_url: str
    asset_size: int
    asset_digest: str

    @property
    def has_update(self) -> bool:
        return tuple(map(int, self.available.split("."))) > tuple(map(int, self.current.split(".")))


@dataclass(frozen=True)
class UiUpdateReady:
    version: str
    staged_exe: Path
    expected_sha256: str
    message: str


@dataclass(frozen=True)
class UiDownloadCancelled:
    message: str = "Скачивание обновления UI отменено."


def _github_token() -> str:
    env = os.environ.copy()
    env.update({"GCM_INTERACTIVE": "Never", "GIT_TERMINAL_PROMPT": "0"})
    try:
        with _SPAWN_LOCK:
            bundled = getattr(sys, "_MEIPASS", None)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            if bundled and not kernel.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                result = subprocess.run([find_git(), "credential-manager", "get"],
                                        input="protocol=https\nhost=github.com\nusername=Hezare0\n\n",
                                        text=True, capture_output=True, env=env, timeout=20,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                if bundled:
                    kernel.SetDllDirectoryW(str(bundled))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode:
        return ""
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return fields.get("password", "")


def _request(url: str, token: str = ""):
    if urllib.parse.urlsplit(url).hostname != "api.github.com":
        raise ClientError("Некорректный адрес GitHub API.")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Zapret-UI-updater",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def check_ui_update() -> UiUpdateInfo:
    token = _github_token()
    try:
        release = _request(API_RELEASE, token)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ClientError("Нет доступа к приватному GitHub Release. Подключи свой аккаунт через Git Credential Manager.") from exc
        raise ClientError(f"GitHub вернул ошибку {exc.code} при проверке UI.") from exc
    except (OSError, TimeoutError) as exc:
        raise ClientError(f"Не удалось проверить релиз UI: {exc}") from exc
    version = str(release.get("tag_name", "")).removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ClientError("GitHub вернул неизвестный формат версии UI.")
    name = f"ZapretClient-v{version}-windows-x64.zip"
    assets = [item for item in release.get("assets", []) if item.get("name") == name and item.get("state") == "uploaded"]
    if not assets:
        if tuple(map(int, version.split("."))) <= tuple(map(int, __version__.split("."))):
            return UiUpdateInfo(__version__, version, str(release["html_url"]), "", 0, "")
        raise ClientError("Новый релиз найден, но автономный ZIP для Windows пока отсутствует.")
    asset = assets[0]
    digest = str(asset.get("digest", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ClientError("GitHub не сообщил SHA-256 нового релиза. Автоматическая установка остановлена.")
    asset_id = asset.get("id")
    if not isinstance(asset_id, int) or asset_id <= 0:
        raise ClientError("У ZIP релиза нет корректного идентификатора GitHub Asset.")
    url = f"https://api.github.com/repos/{EXPECTED_REPO}/releases/assets/{asset_id}"
    return UiUpdateInfo(__version__, version, str(release["html_url"]), url,
                        int(asset.get("size", 0)), digest[7:])


def download_ui_update(info: UiUpdateInfo, store: Store, progress=lambda line: None,
                       cancel: threading.Event | None = None) -> UiUpdateReady | UiDownloadCancelled:
    if not info.has_update:
        raise ClientError("Установлена актуальная версия UI.")
    target = Path(sys.executable).resolve()
    if not getattr(sys, "frozen", False) or target.name.casefold() != "zapretclient.exe":
        raise ClientError("Автоматическое обновление UI доступно в автономном EXE.")
    if not re.fullmatch(r"[0-9a-f]{64}", info.asset_digest) or info.asset_size <= 0:
        raise ClientError("Нет проверенной контрольной суммы релиза.")
    if not re.fullmatch(r"https://api\.github\.com/repos/Hezare0/Zapret-UI/releases/assets/[1-9][0-9]*",
                        info.asset_url):
        raise ClientError("Некорректный адрес релизного файла UI.")
    if cancel and cancel.is_set():
        return UiDownloadCancelled()
    token = _github_token()
    headers = {"User-Agent": "Zapret-UI-updater", "Accept": "application/octet-stream",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    directory = store.folder / "self-update"
    directory.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".ZapretClient-{info.available}-{uuid.uuid4().hex}.new.exe"
    class StripCrossHostAuth(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, response_headers, new_url):
            redirected = super().redirect_request(request, fp, code, message, response_headers, new_url)
            if redirected and urllib.parse.urlsplit(new_url).hostname != urllib.parse.urlsplit(request.full_url).hostname:
                redirected.remove_header("Authorization")
            return redirected

    opener = urllib.request.build_opener(StripCrossHostAuth)
    with tempfile.TemporaryDirectory(prefix="ui-download-", dir=directory) as temporary:
        archive = Path(temporary) / "release.zip"
        progress(f"Скачиваю UI {info.available}…")
        try:
            request = urllib.request.Request(info.asset_url, headers=headers)
            with opener.open(request, timeout=15) as response, archive.open("wb") as output:
                digest = hashlib.sha256()
                size = 0
                while chunk := response.read(1024 * 1024):
                    if cancel and cancel.is_set():
                        return UiDownloadCancelled()
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except (OSError, urllib.error.HTTPError) as exc:
            raise ClientError(f"Не удалось скачать обновление UI: {type(exc).__name__}") from exc
        if size != info.asset_size or digest.hexdigest() != info.asset_digest:
            raise ClientError("SHA-256 или размер скачанного ZIP не совпали с GitHub.")
        if cancel and cancel.is_set():
            return UiDownloadCancelled()
        progress("Проверяю и распаковываю автономный EXE…")
        try:
            with zipfile.ZipFile(archive) as bundle:
                expected = "ZapretClient/ZapretClient.exe"
                if expected not in bundle.namelist():
                    raise ClientError("В проверенном ZIP не найден ZapretClient.exe.")
                exe_hash = hashlib.sha256()
                with bundle.open(expected) as source, staged.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        if cancel and cancel.is_set():
                            break
                        output.write(chunk)
                        exe_hash.update(chunk)
                if cancel and cancel.is_set():
                    staged.unlink(missing_ok=True)
                    return UiDownloadCancelled()
                expected_digest = exe_hash.hexdigest()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            staged.unlink(missing_ok=True)
            raise ClientError(f"Не удалось распаковать EXE из релиза: {type(exc).__name__}") from exc
        except Exception:
            staged.unlink(missing_ok=True)
            raise
    return UiUpdateReady(info.available, staged, expected_digest,
                         f"UI {info.available} скачан и проверен. Клиент будет перезапущен для установки.")


def _install_script(payload: str) -> str:
    return rf'''
$ErrorActionPreference = 'Stop'
$plan = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$current = [string]$plan.current
$staged = [string]$plan.staged
$marker = [string]$plan.marker
$previous = $current + '.previous.exe'
$failure = [IO.Path]::ChangeExtension($marker, '.error.txt')
$sha = [Security.Cryptography.SHA256]::Create()
$stream = [IO.File]::OpenRead($staged)
try {{ $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }}
finally {{ $stream.Dispose(); $sha.Dispose() }}
if ($actual -ne [string]$plan.hash) {{ exit 10 }}
$swapped = $false
for ($i = 0; $i -lt 240; $i++) {{
    try {{
        [IO.File]::Move($current, $previous)
        try {{ [IO.File]::Move($staged, $current); $swapped = $true; break }}
        catch {{ [IO.File]::Move($previous, $current); throw }}
    }} catch {{ Start-Sleep -Milliseconds 500 }}
}}
if (-not $swapped) {{ [IO.File]::WriteAllText($failure, 'Could not replace the running executable.'); exit 11 }}
$launched = $null
try {{
    $launched = Start-Process -FilePath $current -ArgumentList @('--post-update-marker', ('"' + $marker + '"')) -PassThru
    for ($i = 0; $i -lt 120; $i++) {{
        if (Test-Path -LiteralPath $marker) {{
            try {{
                Add-Type -AssemblyName Microsoft.VisualBasic
                [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($previous,[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
            }} catch {{ [IO.File]::WriteAllText($failure, 'Updated UI started, but old EXE could not be recycled: ' + $_.Exception.Message) }}
            exit 0
        }}
        if ($launched.HasExited) {{ break }}
        Start-Sleep -Milliseconds 500
    }}
    throw 'Updated UI did not confirm startup within 60 seconds.'
}} catch {{
    $problem = $_.Exception.Message
    try {{
        if ($launched -and -not $launched.HasExited) {{ Stop-Process -Id $launched.Id -Force; $launched.WaitForExit(15000) | Out-Null }}
        for ($i = 0; $i -lt 30; $i++) {{
            try {{ [IO.File]::Move($current, $staged); [IO.File]::Move($previous, $current); break }}
            catch {{ Start-Sleep -Milliseconds 500 }}
        }}
        if (Test-Path -LiteralPath $previous) {{ throw 'Previous EXE could not be restored.' }}
        [IO.File]::WriteAllText($failure, 'Update rolled back: ' + $problem)
    }} catch {{ [IO.File]::WriteAllText($failure, 'Update failed; manual recovery required: ' + $problem + ' / ' + $_.Exception.Message) }}
    exit 12
}}
'''


def launch_install_helper(ready: UiUpdateReady, store: Store) -> None:
    target = Path(sys.executable).resolve()
    if not ready.staged_exe.is_file() or ready.staged_exe.parent != target.parent:
        raise ClientError("Проверенный EXE для установки не найден рядом с приложением.")
    if hashlib.sha256(ready.staged_exe.read_bytes()).hexdigest() != ready.expected_sha256:
        raise ClientError("EXE изменился после проверки. Установка отменена.")
    if target.with_name(target.name + ".previous.exe").exists():
        raise ClientError("Остался EXE от предыдущей неоконченной установки. Обновление остановлено; проверь файл .previous.exe.")
    folder = store.folder / "self-update"
    marker = folder / ("started-" + uuid.uuid4().hex + ".txt")
    payload = base64.b64encode(json.dumps({"current": str(target), "staged": str(ready.staged_exe),
                                            "hash": ready.expected_sha256, "marker": str(marker)},
                                           ensure_ascii=False).encode()).decode()
    script = _install_script(payload)
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    subprocess.Popen([str(powershell_path()), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-EncodedCommand", encoded], cwd=target.parent, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
