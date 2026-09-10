from pathlib import Path

import pytest

from zapret_client.repository import ClientError, Repository, validate_shell_path
from zapret_client.results import ReportParser
from zapret_client.storage import Store


def test_discovery_and_invalidation(sample_repo):
    repo = Repository(sample_repo)
    assert [c.name for c in repo.configs] == ["general (ALT).bat", "general (ALT3).bat", "general (ALT11).bat", "general.bat"]
    before = {c.name: c.fingerprint for c in repo.configs}
    (sample_repo / "general.bat").write_text("echo changed")
    repo.refresh()
    assert repo.get("general.bat").fingerprint != before["general.bat"]
    assert repo.get("general (ALT).bat").fingerprint == before["general (ALT).bat"]
    after = repo.get("general.bat").fingerprint
    (sample_repo / "lists/ipset-all.txt").write_text("203.0.113.1")
    repo.refresh()
    assert repo.get("general.bat").fingerprint != after
    assert repo.get("general (ALT).bat").fingerprint != before["general (ALT).bat"]
    with pytest.raises(ClientError):
        repo.get("../other.bat")


@pytest.mark.parametrize("value", ["C:/foo%TEMP%/general.bat", "C:/foo&bar", "C:/foo!bar", "C:/foo^bar"])
def test_shell_unsafe_paths(value):
    with pytest.raises(ClientError):
        validate_shell_path(Path(value))


def test_spaces_are_allowed():
    validate_shell_path(Path("C:/some folder/general (ALT3).bat"))


def test_cache_roundtrip_and_repository_isolation(tmp_path):
    store = Store(tmp_path)
    result = ReportParser.parse("[1/1] general.bat\nDNS Ping: 12 ms").results["general.bat"]
    store.put("repo-a", result, "abc", "live")
    store.save()
    reread = Store(tmp_path)
    assert reread.entries("repo-a")["general.bat"]["result"]["targets"]["DNS"]["ping_ms"] == 12
    assert reread.entries("repo-b") == {}


def test_bad_cache_falls_back(tmp_path):
    (tmp_path / "state.json").write_text("{bad json", encoding="utf-8")
    assert Store(tmp_path).warning


def test_last_good_backup_recovers_results_and_preserves_corrupt_file(tmp_path):
    store = Store(tmp_path)
    result = ReportParser.parse("[1/1] general.bat\nDNS Ping: 17 ms").results["general.bat"]
    store.put("repo", result, "fingerprint", "live", tested_at="2026-09-10T21:47:14+05:00")
    store.save()
    store.save()  # The last good state is now also present in backup.
    store.path.write_text("{broken", encoding="utf-8")
    recovered = Store(tmp_path)
    assert recovered.warning
    assert recovered.entries("repo")["general.bat"]["tested_at"] == "2026-09-10T21:47:14+05:00"
    recovered.save()
    assert next(tmp_path.glob("state.recovery-*.json")).read_text() == "{broken"
    assert Store(tmp_path).entries("repo")["general.bat"]["result"]["targets"]["DNS"]["ping_ms"] == 17
