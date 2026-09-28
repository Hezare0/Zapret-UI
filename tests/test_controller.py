import time

import pytest

import zapret_client.controller as runtime
from zapret_client.controller import Controller, TestControl
from zapret_client.probe_engine import ProbeCancelled
from zapret_client.repository import ClientError, Repository
from zapret_client.storage import Store
from zapret_client.results import ReportParser, Target


@pytest.mark.parametrize("interrupt", [False, True])
def test_own_diagnostics_checkpoints_and_restores_exact_ipset(sample_repo, tmp_path, monkeypatch, interrupt):
    repo = Repository(sample_repo)
    store = Store(tmp_path / "state")
    before = (sample_repo / "lists/ipset-all.txt").read_bytes()
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)
    monkeypatch.setattr(runtime, "load_targets", lambda *_: [object()])
    monkeypatch.setattr(runtime, "system_tool", lambda name: name)
    controller = Controller(store)
    def start(repo, name, *, cancel=None, stable_for=1.0):
        controller.active_name, controller.active_repo = name, repo
        return name
    def stop():
        controller.active_name = controller.active_repo = None
    monkeypatch.setattr(controller, "start", start)
    monkeypatch.setattr(controller, "stop", stop)
    monkeypatch.setattr(controller, "running", lambda: bool(controller.active_name))
    checked = []
    def probes(targets, curl, ping, cancel, on_target, **kwargs):
        checked.append(controller.active_name)
        if interrupt and len(checked) == 2:
            raise RuntimeError("simulated probe failure")
        (sample_repo / "lists/ipset-all.txt").write_bytes(b"changed by fixture")
        on_target(Target("DNS", {}, 12.0, "12 ms"))
    monkeypatch.setattr(runtime, "run_probes", probes)
    observed = []
    checkpoints = []
    def progress(line, parser):
        observed.append(line)
        if parser.index > 1:
            checkpoints.append(len(Store(store.folder).entries(repo.key)))
    outcome = controller.run_tests(repo, progress)
    assert (sample_repo / "lists/ipset-all.txt").read_bytes() == before
    assert outcome.successful is (not interrupt)
    assert len(outcome.parser.results) == (2 if interrupt else len(repo.configs))
    assert any("DNS" in line and "Ping: 12 ms" in line for line in observed)
    assert outcome.log.is_file()
    assert (outcome.log.parent / "ipset-all.before.txt").read_bytes() == before
    assert controller.active_name is None
    assert checkpoints and checkpoints[0] == 1


def test_invalid_targets_do_not_stop_active_config(sample_repo, tmp_path, monkeypatch):
    repo = Repository(sample_repo)
    (sample_repo / "utils/targets.txt").write_text('Broken = "file:///private"\n')
    monkeypatch.setattr(runtime, "check_conflicts", lambda *args: None)
    controller = Controller(Store(tmp_path / "state"))
    called = []
    monkeypatch.setattr(controller, "stop", lambda: called.append("stop"))
    with pytest.raises(ClientError, match="URL"):
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
    monkeypatch.setattr(runtime, "load_targets", lambda *_: [object()])
    monkeypatch.setattr(runtime, "system_tool", lambda name: name)
    first, second = repo.configs[:2]
    old = ReportParser.parse(f"[1/1] {second.name}\nDNS Ping: 99 ms").results[second.name.casefold()]
    store.put(repo.key, old, second.fingerprint, "live", tested_at="2026-09-10T10:00:00+05:00")
    store.save()

    controller = Controller(store)
    controller.active_name, controller.active_repo = "general.bat", repo
    restored = []
    def restore(repo, name, *, cancel=None, stable_for=1.0):
        if name == "general.bat":
            restored.append(name)
        controller.active_name, controller.active_repo = name, repo
        return name
    monkeypatch.setattr(controller, "start", restore)
    def stop():
        controller.active_name = controller.active_repo = None
    monkeypatch.setattr(controller, "stop", stop)
    monkeypatch.setattr(controller, "running", lambda: bool(controller.active_name))
    def probes(targets, curl, ping, cancel, on_target, **kwargs):
        if controller.active_name == first.name:
            (sample_repo / "lists/ipset-all.txt").write_bytes(b"changed")
            on_target(Target("DNS", {}, 12.0, "12 ms"))
        else:
            control.request(closing=closing)
            raise ProbeCancelled()
    monkeypatch.setattr(runtime, "run_probes", probes)
    def progress(line, parser):
        pass
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
    monkeypatch.setattr(runtime, "system_tool", lambda name: name)
    controller = Controller(Store(tmp_path / "state"))
    monkeypatch.setattr(controller, "start", lambda *a, **kw: pytest.fail("Cancelled sweep started a config"))
    monkeypatch.setattr(controller, "stop", lambda: pytest.fail("Cancelled sweep stopped an active config"))
    outcome = controller.run_tests(repo, lambda *args: None, control)
    assert outcome.cancelled and outcome.cleanup_ok
    assert not outcome.parser.results
