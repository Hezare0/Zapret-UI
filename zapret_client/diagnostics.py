"""Versioned adapter for the exact upstream standard-test implementation.

Only a temporary copy is adapted. Network probes and upstream cleanup remain
in the upstream script; menu input, paths and process ownership are adapted.
"""

import hashlib
from pathlib import Path

from .repository import ClientError


SUPPORTED_HASHES = {"8fb0671fbf7fb23582afd1738a93baacd33988879ab5c1b7dc36bd381c493ef3"}

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
    text = source.decode("utf-8-sig").replace("\r\n", "\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest not in SUPPORTED_HASHES:
        raise ClientError("Версия utils\\test zapret.ps1 пока не поддерживается клиентом. "
                          "Автоматический прогон остановлен. Можно импортировать отчёт service.bat. "
                          f"SHA-256: {digest}")
    replacements = [
        ('$rootDir = Split-Path $PSScriptRoot', '$rootDir = $env:ZAPRET_CLIENT_ROOT', 1),
        ('Where-Object { $_.Name -notlike "service*" }',
         'Where-Object { $_.Name -like "general*.bat" }', 1),
        ('Read-Host "Enter 1 or 2"', '"1"', 2),
        ('[void][System.Console]::ReadKey($true)', '# Console wait omitted by Zapret Client', 3),
        ('Get-Process -Name "winws" -ErrorAction SilentlyContinue',
         'Get-Process -Name "winws" -ErrorAction SilentlyContinue | Where-Object { [ZapretClientJob]::Owns($_.Id) }', 2),
        ('return Get-CimInstance Win32_Process -Filter "Name=\'winws.exe\'" |',
         'return Get-CimInstance Win32_Process -Filter "Name=\'winws.exe\'" | Where-Object { [ZapretClientJob]::Owns($_.ProcessId) } |', 1),
    ]
    for old, new, count in replacements:
        if text.count(old) != count:
            raise ClientError("Структура тестового скрипта изменилась; автоматический запуск остановлен.")
        text = text.replace(old, new)
    return JOB_GUARD + "\n" + text


def prepare_script(source: Path, folder: Path) -> Path:
    if not source.is_file():
        raise ClientError("Не найден utils\\test zapret.ps1. Обновите распакованную сборку.")
    adapted = adapt_script(source.read_bytes())
    path = folder / "standard-tests.ps1"
    # Windows PowerShell 5.1 needs a BOM for scripts containing non-ASCII text.
    path.write_text(adapted, encoding="utf-8-sig")
    return path
