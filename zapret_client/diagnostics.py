"""Structural adapter for the upstream standard-test implementation.

Only a temporary copy is adapted. Network probes and upstream cleanup remain
in the upstream script; menu input, paths and process ownership are adapted.
"""

import re
from pathlib import Path

from .repository import ClientError


JOB_GUARD = r'''
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$ErrorActionPreference = 'Continue'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ZapretClientJob {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    public static bool Owns(int pid) {
        IntPtr job = OpenJobObject(4, false, Environment.GetEnvironmentVariable("ZAPRET_CLIENT_JOB"));
        if (job == IntPtr.Zero) throw new InvalidOperationException("Cannot query client job");
        IntPtr process = OpenProcess(0x1000, false, pid);
        try {
            if (process == IntPtr.Zero) return false;
            bool owned;
            if (!IsProcessInJob(process, job, out owned))
                throw new InvalidOperationException("Cannot verify process ownership");
            return owned;
        } finally {
            if (process != IntPtr.Zero) CloseHandle(process);
            CloseHandle(job);
        }
    }
}
'@ -ErrorAction Stop
if (-not [ZapretClientJob]::Owns($PID)) { throw 'Client job is not attached' }
'''


def adapt_script(source: bytes) -> str:
    try:
        text = source.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ClientError("Тестовый скрипт имеет неизвестную кодировку.") from exc

    def replace_function(name: str, body: str, required: tuple[str, ...]) -> None:
        nonlocal text
        pattern = re.compile(rf"(?ms)^function {re.escape(name)}\s*\{{.*?^\}}")
        matches = list(pattern.finditer(text))
        if len(matches) != 1 or any(fragment not in matches[0][0] for fragment in required):
            raise ClientError(f"Изменилась процедура {name} в тестовом скрипте; автоматическая проверка остановлена.")
        text = text[:matches[0].start()] + body + text[matches[0].end():]

    replace_function("Read-TestType", "function Read-TestType { return 'standard' }",
                     ("'standard'", "'dpi'", "Read-Host"))
    replace_function("Read-ModeSelection", "function Read-ModeSelection { return 'all' }",
                     ("'all'", "'select'", "Read-Host"))

    # Older versions pause directly; newer versions use Wait-AnyKey. Replace
    # the whole helper because Console.KeyAvailable may throw without a console
    # and its catch block would then block on Read-Host.
    if re.search(r"(?m)^function Wait-AnyKey\s*\{", text):
        replace_function("Wait-AnyKey", "function Wait-AnyKey { param([string]$message = '') }",
                         ("ReadKey", "Read-Host"))
    else:
        old_pause = '[void][System.Console]::ReadKey($true)'
        if text.count(old_pause) < 1:
            raise ClientError("Способ ожидания ввода в тестовом скрипте неизвестен.")
        text = text.replace(old_pause, '# Console wait omitted by Zapret Client')

    if any(marker not in text for marker in
           ("[$configNum/$($batFiles.Count)]", "All tests finished.",
            "Results saved to $resultFile", "HTTP", "TLS1.2", "TLS1.3", "PingResult")):
        raise ClientError("Формат Standard-тестов или их результатов изменился; автоматическая проверка остановлена.")
    replacements = [
        ('$rootDir = Split-Path $PSScriptRoot', '$rootDir = $env:ZAPRET_CLIENT_ROOT', 1),
        ('Where-Object { $_.Name -notlike "service*" }',
         'Where-Object { $_.Name -like "general*.bat" }', 1),
        ('Get-Process -Name "winws" -ErrorAction SilentlyContinue',
         'Get-Process -Name "winws" -ErrorAction SilentlyContinue | Where-Object { [ZapretClientJob]::Owns($_.Id) }', 2),
        ('return Get-CimInstance Win32_Process -Filter "Name=\'winws.exe\'" |',
         'return Get-CimInstance Win32_Process -Filter "Name=\'winws.exe\'" | Where-Object { [ZapretClientJob]::Owns($_.ProcessId) } |', 1),
    ]
    for old, new, count in replacements:
        if text.count(old) != count:
            raise ClientError(f"Изменилась часть тестового скрипта, отвечающая за пути или контроль процессов: {old[:80]}")
        text = text.replace(old, new)
    if re.search(r"(?i)(taskkill\s+[^\n]*winws|stop-process\s+-name\s+winws)", text):
        raise ClientError("Тестовый скрипт содержит новый способ остановки winws; проверка остановлена для защиты других процессов.")
    return JOB_GUARD + "\n" + text


def script_compatibility(source: bytes) -> tuple[bool, str]:
    try:
        adapt_script(source)
    except ClientError as exc:
        return False, str(exc)
    return True, "Standard-тесты поддерживаются"


def prepare_script(source: Path, folder: Path) -> Path:
    if not source.is_file():
        raise ClientError("Не найден utils\\test zapret.ps1. Обновите распакованную сборку.")
    adapted = adapt_script(source.read_bytes())
    path = folder / "standard-tests.ps1"
    # Windows PowerShell 5.1 needs a BOM for scripts containing non-ASCII text.
    path.write_text(adapted, encoding="utf-8-sig")
    return path
