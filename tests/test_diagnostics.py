from pathlib import Path
import os
import subprocess
import time

import pytest

from zapret_client.diagnostics import JOB_GUARD, adapt_script
from zapret_client.repository import ClientError
from zapret_client.windows import ProcessJob, powershell_path


UPSTREAM = Path(os.environ.get("ZAPRET_TEST_UPSTREAM", str(Path(__file__).resolve().parents[2] / "zapret-discord-youtube/utils/test zapret.ps1")))


def test_unknown_version_is_rejected():
    with pytest.raises(ClientError, match="не поддерживается"):
        adapt_script(b"Write-Host 'different source'")


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
