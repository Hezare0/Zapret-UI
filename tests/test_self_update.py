"""Self-update must verify the GitHub asset before any executable handoff."""

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

from zapret_client.repository import ClientError
from zapret_client.storage import Store
import zapret_client.self_update as updater


def release_zip(exe=b"dummy standalone executable"):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ZapretClient/ZapretClient.exe", exe)
    return output.getvalue()


class Download:
    def __init__(self, data):
        self.data = BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.data.close()

    def read(self, size):
        return self.data.read(size)


def setup_download(monkeypatch, tmp_path, archive):
    target = tmp_path / "ZapretClient.exe"
    target.write_bytes(b"original executable")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(target))
    monkeypatch.setattr(updater, "_github_token", lambda: "dummy-secret")
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Download(archive)
    monkeypatch.setattr(updater.urllib.request, "build_opener", lambda *_: Opener())
    info = updater.UiUpdateInfo("0.3.0", "0.4.0", "https://github.com/Hezare0/Zapret-UI/releases/tag/v0.4.0",
                                "https://api.github.com/repos/Hezare0/Zapret-UI/releases/assets/123",
                                len(archive), hashlib.sha256(archive).hexdigest())
    return target, info, requests


def test_download_verifies_and_leaves_no_archive(monkeypatch, tmp_path):
    target, info, requests = setup_download(monkeypatch, tmp_path, release_zip())
    store = Store(tmp_path / "state")
    ready = updater.download_ui_update(info, store)
    assert isinstance(ready, updater.UiUpdateReady)
    assert ready.staged_exe.parent == target.parent
    assert hashlib.sha256(ready.staged_exe.read_bytes()).hexdigest() == ready.expected_sha256
    assert target.read_bytes() == b"original executable"
    assert not list((store.folder / "self-update").glob("ui-download-*"))
    assert requests[0].get_header("Authorization") == "Bearer dummy-secret"


def test_digest_mismatch_cannot_stage_executable(monkeypatch, tmp_path):
    target, info, _ = setup_download(monkeypatch, tmp_path, release_zip())
    wrong = updater.UiUpdateInfo(info.current, info.available, info.release_url,
                                 info.asset_url, info.asset_size, "0" * 64)
    with pytest.raises(ClientError, match="SHA-256"):
        updater.download_ui_update(wrong, Store(tmp_path / "state"))
    assert target.read_bytes() == b"original executable"
    assert not list(tmp_path.glob("*.new.exe"))


def test_cancelled_extraction_cleans_staging(monkeypatch, tmp_path):
    archive = release_zip(b"x" * (2 * 1024 * 1024))
    _, info, _ = setup_download(monkeypatch, tmp_path, archive)
    cancel = threading.Event()
    original_open = zipfile.ZipFile.open
    class CancelAfterRead:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def read(self, size):
            result = self.source.read(size)
            cancel.set()
            return result

    def cancelling_open(bundle, name, *args, **kwargs):
        source = original_open(bundle, name, *args, **kwargs)
        source.__enter__()
        return CancelAfterRead(source)
    monkeypatch.setattr(zipfile.ZipFile, "open", cancelling_open)
    result = updater.download_ui_update(info, Store(tmp_path / "state"), cancel=cancel)
    assert isinstance(result, updater.UiDownloadCancelled)
    assert not list(tmp_path.glob("*.new.exe"))


def test_release_check_selects_verified_asset(monkeypatch):
    release = {"tag_name": "v0.5.0", "html_url": "https://github.com/Hezare0/Zapret-UI/releases/tag/v0.5.0",
               "assets": [{"id": 123, "name": "ZapretClient-v0.5.0-windows-x64.zip", "state": "uploaded",
                           "size": 512, "digest": "sha256:" + "a" * 64}]}
    monkeypatch.setattr(updater, "_github_token", lambda: "")
    monkeypatch.setattr(updater, "_request", lambda url, token: release)
    info = updater.check_ui_update()
    assert info.available == "0.5.0" and info.has_update
    assert info.asset_url.endswith("/123")
    assert info.asset_digest == "a" * 64


def test_helper_script_parses_and_contains_rollback():
    payload = base64.b64encode(json.dumps({"current": "C:\\A\\ZapretClient.exe", "staged": "C:\\A\\next.exe",
                                           "marker": "C:\\A\\ready.txt", "hash": "a" * 64}).encode()).decode()
    script = updater._install_script(payload)
    parse = "\n".join((
        "$s=[Console]::In.ReadToEnd()",
        "$tokens=$null; $errors=$null",
        "[System.Management.Automation.Language.Parser]::ParseInput($s,[ref]$tokens,[ref]$errors) | Out-Null",
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }",
    ))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", parse], input=script,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "[IO.File]::Move($previous, $current)" in script
    assert "Update rolled back" in script


def test_helper_rolls_back_when_new_exe_exits_without_marker(tmp_path):
    system = Path(__import__("os").environ["SystemRoot"]) / "System32"
    current = tmp_path / "ZapretClient.exe"
    staged = tmp_path / "candidate.new.exe"
    shutil.copyfile(system / "cmd.exe", current)
    shutil.copyfile(system / "where.exe", staged)
    old_hash = hashlib.sha256(current.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    marker = tmp_path / "ready.txt"
    payload = base64.b64encode(json.dumps({"current": str(current), "staged": str(staged),
                                           "marker": str(marker), "hash": new_hash}).encode()).decode()
    script = updater._install_script(payload)
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    result = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
                            capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert result.returncode == 12, result.stderr
    assert hashlib.sha256(current.read_bytes()).hexdigest() == old_hash
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == new_hash
    assert not (tmp_path / "ZapretClient.exe.previous.exe").exists()
    assert "rolled back" in (tmp_path / "ready.error.txt").read_text(encoding="utf-8")
