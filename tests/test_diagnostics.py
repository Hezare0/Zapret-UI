from pathlib import Path
import os
import subprocess
import time

import pytest

from zapret_client.diagnostics import JOB_GUARD, adapt_script, script_compatibility
from zapret_client.repository import ClientError
from zapret_client.windows import ProcessJob, powershell_path


UPSTREAM = Path(os.environ.get("ZAPRET_TEST_UPSTREAM", str(Path(__file__).resolve().parents[2] / "zapret-discord-youtube/utils/test zapret.ps1")))


def test_unknown_version_is_rejected():
    with pytest.raises(ClientError, match="Read-TestType"):
        adapt_script(b"Write-Host 'different source'")


@pytest.mark.skipif(not UPSTREAM.exists(), reason="Local upstream checkout not present")
def test_adapter_accepts_old_current_and_harmless_future_edit(tmp_path):
    import subprocess
    original = UPSTREAM.read_bytes()
    current = adapt_script(original)
    assert script_compatibility(original)[0]
    changed = original + b"\n# A harmless change in a later upstream revision\n"
    assert script_compatibility(changed)[0]
    root = UPSTREAM.parents[1]
    old = subprocess.run(["git", "show", "6cec828910d0809863205702182a3557d9d0e8c3:utils/test zapret.ps1"],
                         cwd=root, capture_output=True, timeout=20)
    if old.returncode:
        pytest.skip("Earlier upstream commit is not present in this checkout")
    previous = adapt_script(old.stdout)
    for title, script in (("old", previous), ("current", current)):
        assert "[void][System.Console]::ReadKey($true)" not in script
        path = tmp_path / f"{title}.ps1"
        path.write_text(script, encoding="utf-8-sig")
        check = subprocess.run([str(powershell_path()), "-NoProfile", "-Command",
                                f'$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("{path}",[ref]$t,[ref]$e); if($e.Count) {{ exit 1 }}'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.skipif(not UPSTREAM.exists(), reason="Local upstream checkout not present")
def test_new_unconfined_process_stop_is_rejected():
    changed = UPSTREAM.read_bytes() + b"\ntaskkill /IM winws.exe /F\n"
    supported, reason = script_compatibility(changed)
    assert not supported and "новый способ остановки" in reason


@pytest.mark.skipif(not UPSTREAM.exists(), reason="Local upstream checkout not present")
def test_current_upstream_adapter_and_powershell_syntax(tmp_path):
    original = UPSTREAM.read_bytes()
    text = adapt_script(original)
    assert '[void][System.Console]::ReadKey($true)' not in text
    assert 'Read-Host "Enter 1 or 2"' not in text
    assert 'Where-Object { [ZapretClientJob]::Owns($_.Id) }' in text
    adapted = tmp_path / "adapter.ps1"
    adapted.write_text(text, encoding="utf-8-sig")
    check = tmp_path / "parse-only.ps1"
    check.write_text("param([string]$Path)\n$tokens=$null; $errors=$null\n"
                     "[void][System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$tokens,[ref]$errors)\n"
                     "if($errors.Count) { $errors | Format-List; exit 1 }\n", encoding="utf-8-sig")
    result = subprocess.run([str(powershell_path()), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(check), str(adapted)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert original == UPSTREAM.read_bytes()


def test_powershell_guard_detects_own_job_without_admin_or_network(tmp_path):
    script = tmp_path / "guard.ps1"
    script.write_text(JOB_GUARD + '\nWrite-Output "OWNERSHIP_OK"\n', encoding="utf-8-sig")
    log = tmp_path / "guard.log"
    job = ProcessJob()
    try:
        job.start([str(powershell_path()), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                  tmp_path, log, {"ZAPRET_CLIENT_JOB": job.name})
        deadline = time.monotonic() + 25
        while job.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert job.poll() == 0, log.read_text(encoding="utf-8", errors="replace")
        assert "OWNERSHIP_OK" in log.read_text(encoding="utf-8", errors="replace")
    finally:
        job.stop()
