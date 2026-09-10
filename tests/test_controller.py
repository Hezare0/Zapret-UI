from pathlib import Path
import textwrap
import time

import pytest

import zapret_client.controller as runtime
from zapret_client.controller import Controller, TestControl
from zapret_client.repository import ClientError, Repository
from zapret_client.storage import Store
from zapret_client.results import ReportParser


@pytest.mark.parametrize("exit_code", [0, 9])
def test_diagnostics_streams_and_restores_exact_ipset(sample_repo, tmp_path, monkeypatch, exit_code):
    repo = Repository(sample_repo)
    store = Store(tmp_path / "state")
    before = (sample_repo / "lists/ipset-all.txt").read_bytes()
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)

    def fixture_script(source, folder):
        script = folder / "fixture.ps1"
        lines = ["[INFO] Targets loaded: 1"]
        for i, config in enumerate(repo.configs, 1):
            lines.extend([f"[{i}/{len(repo.configs)}] {config.name}", "Running tests...", "DNS Ping: 12 ms"])
        if exit_code == 0:
            lines.extend(["All tests finished.", "Results saved to fixture.txt"])
        body = '\n'.join("Write-Output '" + line + "'" for line in lines)
        script.write_text("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n" + body +
                          "\n[IO.File]::WriteAllText((Join-Path $env:ZAPRET_CLIENT_ROOT 'lists/ipset-all.txt'), 'changed by fixture')\n" +
                          f"exit {exit_code}\n", encoding="utf-8-sig")
        return script

    monkeypatch.setattr(runtime, "prepare_script", fixture_script)
    controller = Controller(store)
    observed = []
    checkpoints = []
    def progress(line, parser):
        observed.append(line)
        if parser.index > 1:
            checkpoints.append(len(Store(store.folder).entries(repo.key)))
    outcome = controller.run_tests(repo, progress)
    assert (sample_repo / "lists/ipset-all.txt").read_bytes() == before
    assert outcome.successful is (exit_code == 0)
    assert len(outcome.parser.results) == len(repo.configs)
    assert any("DNS Ping: 12 ms" in line for line in observed)
    assert outcome.log.is_file()
    assert (outcome.log.parent / "ipset-all.before.txt").read_bytes() == before
    assert controller.active_name is None
    assert checkpoints and checkpoints[0] == 1


def test_unknown_adapter_does_not_stop_active_config(sample_repo, tmp_path, monkeypatch):
    repo = Repository(sample_repo)
    (repo.test_script).write_text("Write-Host 'unknown'")
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)
    controller = Controller(Store(tmp_path / "state"))
    called = []
    monkeypatch.setattr(controller, "stop", lambda: called.append("stop"))
    with pytest.raises(ClientError, match="не поддерживается"):
        controller.run_tests(repo, lambda *args: None)
    assert called == []


def test_conflict_does_not_stop_or_start_anything(sample_repo, tmp_path, monkeypatch):
    controller = Controller(Store(tmp_path / "state"))
    repo = Repository(sample_repo)
    called = []

    def conflict(*args):
        raise ClientError("foreign winws")

    monkeypatch.setattr(runtime, "check_conflicts", conflict)
    monkeypatch.setattr(controller, "stop", lambda: called.append("stop"))
    with pytest.raises(ClientError, match="foreign"):
        controller.start(repo, "general.bat")
    assert called == []


@pytest.mark.parametrize("closing", [False, True])
def test_cancel_sweep_restores_ipset_and_preserves_completed_and_old_results(sample_repo, tmp_path, monkeypatch, closing):
    repo = Repository(sample_repo)
    store = Store(tmp_path / "state")
    control = TestControl()
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)
    before = (sample_repo / "lists/ipset-all.txt").read_bytes()
    first, second = repo.configs[:2]
    old = ReportParser.parse(f"[1/1] {second.name}\nDNS Ping: 99 ms").results[second.name.casefold()]
    store.put(repo.key, old, second.fingerprint, "live", tested_at="2026-09-10T10:00:00+05:00")
    store.save()

    def fixture_script(source, folder):
        script = folder / "cancel.ps1"
        script.write_text(
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "[IO.File]::WriteAllText((Join-Path $env:ZAPRET_CLIENT_ROOT 'lists/ipset-all.txt'), 'changed')\n"
            "Write-Output '[INFO] Targets loaded: 1'\n"
            f"Write-Output '[1/4] {first.name}'\nWrite-Output 'DNS Ping: 12 ms'\n"
            f"Write-Output '[2/4] {second.name}'\nWrite-Output 'WAIT_FOR_CANCEL'\nStart-Sleep -Seconds 60\n",
            encoding="utf-8-sig")
        return script

    monkeypatch.setattr(runtime, "prepare_script", fixture_script)
    controller = Controller(store)
    controller.active_name, controller.active_repo = "general.bat", repo
    restored = []
    def restore(repo, name, *, cancel=None):
        restored.append(name)
        controller.active_name, controller.active_repo = name, repo
        return name
    monkeypatch.setattr(controller, "start", restore)
    def progress(line, parser):
        if "WAIT_FOR_CANCEL" in line:
            control.request(closing=closing)
    started = time.monotonic()
    outcome = controller.run_tests(repo, progress, control)
    assert time.monotonic() - started < 10
    assert outcome.cancelled and not outcome.successful and outcome.cleanup_ok
    assert not controller.cleanup_pending
    assert (sample_repo / "lists/ipset-all.txt").read_bytes() == before
    saved = Store(store.folder).entries(repo.key)
    assert saved[first.name.casefold()]["result"]["targets"]["DNS"]["ping_ms"] == 12
    assert saved[first.name.casefold()]["source"] == "cancelled"
    assert saved[second.name.casefold()]["result"]["targets"]["DNS"]["ping_ms"] == 99
    assert saved[second.name.casefold()]["tested_at"] == "2026-09-10T10:00:00+05:00"
    assert restored == ([] if closing else ["general.bat"])
    controller.stop()


def test_cancel_before_launch_never_starts_test_process(sample_repo, tmp_path, monkeypatch):
    repo = Repository(sample_repo)
    control = TestControl()
    control.request(closing=True)
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)
    script = tmp_path / "must-not-run.ps1"
    script.write_text("throw 'Must not run'")
    monkeypatch.setattr(runtime, "prepare_script", lambda *args: script)
    def unexpected_job():
        pytest.fail("A cancelled operation must not create a process")
    monkeypatch.setattr(runtime, "ProcessJob", unexpected_job)
    controller = Controller(Store(tmp_path / "state"))
    outcome = controller.run_tests(repo, lambda *args: None, control)
    assert outcome.cancelled and outcome.cleanup_ok
    assert not outcome.parser.results
