from pathlib import Path
import textwrap

import pytest

import zapret_client.controller as runtime
from zapret_client.controller import Controller
from zapret_client.repository import ClientError, Repository
from zapret_client.storage import Store


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
