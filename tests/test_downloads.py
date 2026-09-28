from pathlib import Path
import threading

import pytest

import zapret_client.downloads as downloads
import zapret_client.gui as gui
from zapret_client.downloads import download_zapret
from zapret_client.repository import ClientError
from zapret_client.storage import Store
from test_updates import git


@pytest.fixture
def local_remote(sample_repo):
    git(sample_repo, "init", "-b", "main")
    git(sample_repo, "add", ".")
    git(sample_repo, "commit", "-m", "initial fixture")
    return sample_repo


def test_downloads_clean_checkout_beside_application(local_remote, tmp_path, monkeypatch):
    application = tmp_path / "ZapretClient"
    application.mkdir()
    destination = application / "zapret-discord-youtube"
    monkeypatch.setattr(downloads, "download_directory", lambda: destination)
    monkeypatch.setattr(downloads, "REMOTE_URL", str(local_remote))
    store = Store(tmp_path / "state")
    outcome = download_zapret(destination, store)
    assert not outcome.cancelled and outcome.path == destination
    assert (destination / "service.bat").exists()
    assert git(destination, "rev-parse", "HEAD") == outcome.commit
    assert git(destination, "branch", "--show-current") == "main"
    assert not list(application.glob(".zapret-downloading-*"))
    with pytest.raises(ClientError, match="уже существует"):
        download_zapret(destination, store)


def test_cancelled_download_leaves_no_checkout(local_remote, tmp_path, monkeypatch):
    application = tmp_path / "ZapretClient"
    application.mkdir()
    destination = application / "zapret-discord-youtube"
    monkeypatch.setattr(downloads, "download_directory", lambda: destination)
    monkeypatch.setattr(downloads, "REMOTE_URL", str(local_remote))
    control = threading.Event()
    def cancelled_clone(*args, **kwargs):
        control.set()
        raise ClientError("cancelled")
    monkeypatch.setattr(downloads, "run_external", cancelled_clone)
    outcome = download_zapret(destination, Store(tmp_path / "state"), cancel=control)
    assert outcome.cancelled and not destination.exists()
    assert not list(application.glob(".zapret-downloading-*"))


def test_rejects_target_outside_application(local_remote, tmp_path, monkeypatch):
    application = tmp_path / "ZapretClient"
    application.mkdir()
    destination = application / "zapret-discord-youtube"
    monkeypatch.setattr(downloads, "download_directory", lambda: destination)
    with pytest.raises(ClientError, match="рядом с клиентом"):
        download_zapret(tmp_path / "elsewhere", Store(tmp_path / "state"))


def test_gui_download_button_loads_new_checkout(app, local_remote, tmp_path, monkeypatch):
    application = tmp_path / "ZapretClient"
    application.mkdir()
    destination = application / "zapret-discord-youtube"
    monkeypatch.setattr(downloads, "download_directory", lambda: destination)
    monkeypatch.setattr(downloads, "REMOTE_URL", str(local_remote))
    monkeypatch.setattr(gui, "download_directory", lambda: destination)
    window = gui.MainWindow(Store(tmp_path / "state"))
    assert window.download_button.text() == "Скачать zapret"
    window.download_button.click()
    from test_gui import drain_until
    drain_until(app, lambda: window.worker is None, seconds=6)
    assert window.repo and window.repo.root == destination.resolve()
    assert window.download_button.text() == "Открыть скачанный"
    assert window.config_table.rowCount() == 4
    window.close()
