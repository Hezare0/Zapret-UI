from pathlib import Path
import subprocess
import sys

import psutil
import pytest

import zapret_client.updates as updates
from zapret_client.repository import ClientError, Repository
from zapret_client.results import ReportParser
from zapret_client.storage import Store
from zapret_client.updates import Updater, find_git, run_external


def git(root, *args):
    result = subprocess.run([find_git(), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                             "-c", "commit.gpgsign=false", *args], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def update_pair(sample_repo, tmp_path, monkeypatch):
    # A local working repository acts as the remote. No network or git push.
    seed = sample_repo
    (seed / ".gitignore").write_text("lists/list-general-user.txt\n")
    git(seed, "init", "-b", "main")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "initial fixture")
    checkout = tmp_path / "checkout with spaces"
    git(tmp_path, "clone", "--no-hardlinks", str(seed), str(checkout))
    monkeypatch.setattr(updates, "is_official_remote", lambda url: Path(url).resolve() == seed.resolve())
    monkeypatch.setattr(updates, "assert_update_idle", lambda: None)
    repo = Repository(checkout)
    store = Store(tmp_path / "state")
    (seed / "service.bat").write_text('@echo off\nset "LOCAL_VERSION=2.0"\n')
    (seed / "general.bat").write_text("@echo changed fixture\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "new fixture version")
    return seed, repo, store


def test_check_update_without_persistent_archive_and_up_to_date(update_pair):
    seed, repo, store = update_pair
    updater = Updater(repo, store)
    original = (repo.root / "general.bat").read_bytes()
    ignored = repo.root / "lists/list-general-user.txt"
    ignored.write_text("custom.example.invalid\n")
    result = ReportParser.parse("[1/1] general.bat\nDNS Ping: 10 ms").results["general.bat"]
    old_fingerprint = repo.get("general.bat").fingerprint
    store.put(repo.key, result, old_fingerprint, "live")
    store.save()
    info = updater.check()
    assert info.available and info.commits == 1 and info.remote_version == "2.0"
    assert "general.bat" in info.changed_files
    assert not info.compatible
    assert (repo.root / "general.bat").read_bytes() == original  # Check does not apply.
    outcome = updater.apply(info)
    assert git(repo.root, "rev-parse", "HEAD") == info.target
    assert repo.version == "2.0"
    assert ignored.read_text() == "custom.example.invalid\n"
    assert outcome.preserved_paths == ()
    assert not (store.folder / "backups").exists()
    assert Store(store.folder).entries(repo.key)["general.bat"]["result"]["targets"]
    assert repo.get("general.bat").fingerprint != old_fingerprint
    assert not updater.check().available


@pytest.mark.parametrize("upstream_changes_ipset", [False, True])
def test_modified_ipset_survives_fast_forward_without_archive(update_pair, upstream_changes_ipset):
    seed, repo, store = update_pair
    updater = Updater(repo, store)
    if upstream_changes_ipset:
        (seed / "lists/ipset-all.txt").write_text("198.51.100.3/32\n")
        git(seed, "add", "lists/ipset-all.txt")
        git(seed, "commit", "-m", "upstream ipset")
    local_ipset = repo.root / "lists/ipset-all.txt"
    original = b"\xef\xbb\xbf203.0.113.113/32\r\ncustom-user-range\r\n"
    local_ipset.write_bytes(original)
    info = updater.check()
    outcome = updater.apply(info)
    assert git(repo.root, "rev-parse", "HEAD") == info.target
    assert local_ipset.read_bytes() == original
    assert outcome.preserved_paths == ("lists/ipset-all.txt",)
    assert not (store.folder / "backups").exists()
    assert git(repo.root, "status", "--porcelain").splitlines() == ["M lists/ipset-all.txt"]
    assert not git(repo.root, "stash", "list")
    if upstream_changes_ipset:
        assert "Upstream тоже изменил IPSet" in outcome.message


def test_unrelated_user_stash_is_not_removed(update_pair):
    _, repo, store = update_pair
    # Keep a real prior stash made by the user from another tracked file.
    (repo.root / "general (ALT).bat").write_text("personal change\n")
    git(repo.root, "stash", "push", "-m", "pre-existing user stash", "--", "general (ALT).bat")
    before = git(repo.root, "rev-parse", "refs/stash")
    (repo.root / "lists/ipset-all.txt").write_bytes(b"user mode\r\n")
    updater = Updater(repo, store)
    updater.apply(updater.check())
    assert git(repo.root, "rev-parse", "refs/stash") == before


def test_two_successive_updates_reuse_disabled_hooks_directory(update_pair):
    seed, repo, store = update_pair
    updater = Updater(repo, store)
    updater.apply(updater.check())
    (seed / "service.bat").write_text('@echo off\nset "LOCAL_VERSION=3.0"\n')
    git(seed, "add", "service.bat")
    git(seed, "commit", "-m", "another fixture version")
    info = updater.check()
    updater.apply(info)
    assert repo.version == "3.0"
    assert git(repo.root, "rev-parse", "HEAD") == info.target


def test_failed_merge_restores_ipset_and_leaves_no_archive(update_pair, monkeypatch):
    _, repo, store = update_pair
    ipset = repo.root / "lists/ipset-all.txt"
    ipset.write_bytes(b"user mode\r\n")
    updater = Updater(repo, store)
    info = updater.check()
    original_command = updater.command
    def fail_merge(*args, **kwargs):
        if "merge" in args:
            raise ClientError("simulated merge failure")
        return original_command(*args, **kwargs)
    monkeypatch.setattr(updater, "command", fail_merge)
    with pytest.raises(ClientError, match="Обновление не завершено"):
        updater.apply(info)
    assert ipset.read_bytes() == b"user mode\r\n"
    assert git(repo.root, "rev-parse", "HEAD") == info.current
    assert not (store.folder / "backups").exists()


def test_local_changes_are_preserved_and_block_update(update_pair):
    _, repo, store = update_pair
    updater = Updater(repo, store)
    info = updater.check()
    (repo.root / "general.bat").write_text("local custom change\n")
    with pytest.raises(ClientError, match="локальные изменения"):
        updater.apply(info)
    assert (repo.root / "general.bat").read_text() == "local custom change\n"
    assert git(repo.root, "rev-parse", "HEAD") == info.current
    assert not (store.folder / "backups").exists()


def test_recheck_required_when_local_head_changes(update_pair):
    _, repo, store = update_pair
    updater = Updater(repo, store)
    info = updater.check()
    (repo.root / "local.txt").write_text("local commit")
    git(repo.root, "add", ".")
    git(repo.root, "commit", "-m", "local")
    with pytest.raises(ClientError, match="изменилась после проверки"):
        updater.apply(info)
    with pytest.raises(ClientError, match="расходятся"):
        updater.check()


def test_unsupported_remote_is_rejected_before_fetch(update_pair, monkeypatch):
    _, repo, store = update_pair
    monkeypatch.setattr(updates, "is_official_remote", lambda url: False)
    with pytest.raises(ClientError, match="origin"):
        Updater(repo, store).check()


def test_no_git_checkout_has_clear_error(sample_repo, tmp_path):
    with pytest.raises(ClientError, match="git-копии"):
        Updater(Repository(sample_repo), Store(tmp_path / "state")).check()


def test_external_timeout_stops_descendants(tmp_path):
    ready = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(f"import os,pathlib,time\npathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,{str(child)!r}])\ntime.sleep(30)\n")
    with pytest.raises(ClientError, match="не ответил"):
        run_external([sys.executable, str(parent)], cwd=tmp_path, timeout=1, log_directory=tmp_path / "logs")
    assert ready.exists()
    try:
        process = psutil.Process(int(ready.read_text()))
        process.wait(timeout=2)
    except psutil.NoSuchProcess:
        pass


def test_bundled_git_is_used_before_system_git(tmp_path, monkeypatch):
    executable = tmp_path / "vendor/git/cmd/git.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert find_git() == str(executable)
