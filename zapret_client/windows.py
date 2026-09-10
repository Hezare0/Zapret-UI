"""Windows process ownership, elevation and read-only conflict detection."""

import ctypes as ct
from contextlib import ExitStack
from ctypes import wintypes as wt
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil

from .repository import ClientError


kernel = ct.WinDLL("kernel32", use_last_error=True)
shell = ct.WinDLL("shell32", use_last_error=True)
SIZE_T = ct.c_size_t


class STARTUPINFO(ct.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ct.POINTER(wt.BYTE)), ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ct.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class BASIC_LIMIT(ct.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ct.c_int64), ("PerJobUserTimeLimit", ct.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", SIZE_T),
                ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", SIZE_T), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ct.Structure):
    _fields_ = [(name, ct.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class EXTENDED_LIMIT(ct.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


def _signature(name, args, result):
    fn = getattr(kernel, name)
    fn.argtypes, fn.restype = args, result
    return fn


_signature("CreateJobObjectW", [ct.c_void_p, wt.LPCWSTR], wt.HANDLE)
_signature("SetInformationJobObject", [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD], wt.BOOL)
_signature("QueryInformationJobObject", [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p], wt.BOOL)
_signature("AssignProcessToJobObject", [wt.HANDLE, wt.HANDLE], wt.BOOL)
_signature("TerminateJobObject", [wt.HANDLE, wt.UINT], wt.BOOL)
_signature("CloseHandle", [wt.HANDLE], wt.BOOL)
_signature("ResumeThread", [wt.HANDLE], wt.DWORD)
_signature("TerminateProcess", [wt.HANDLE, wt.UINT], wt.BOOL)
_signature("GetExitCodeProcess", [wt.HANDLE, ct.POINTER(wt.DWORD)], wt.BOOL)
_signature("WaitForSingleObject", [wt.HANDLE, wt.DWORD], wt.DWORD)
_signature("CreateMutexW", [ct.c_void_p, wt.BOOL, wt.LPCWSTR], wt.HANDLE)
_signature("CreateProcessW", [wt.LPCWSTR, wt.LPWSTR, ct.c_void_p, ct.c_void_p, wt.BOOL,
                             wt.DWORD, ct.c_void_p, wt.LPCWSTR,
                             ct.POINTER(STARTUPINFO), ct.POINTER(PROCESS_INFORMATION)], wt.BOOL)


def is_admin() -> bool:
    return bool(shell.IsUserAnAdmin())


def elevate(argv: list[str]) -> None:
    shell.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ct.c_int]
    shell.ShellExecuteW.restype = ct.c_void_p
    executable = sys.executable
    args = list(argv)
    if not getattr(sys, "frozen", False):
        args.insert(0, str(Path(__file__).resolve().parents[1] / "main.py"))
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.is_file():
            executable = str(windowless)
    code = shell.ShellExecuteW(None, "runas", executable, subprocess.list2cmdline(args), None, 1)
    if not code or code <= 32:
        raise ClientError("Повышение прав отменено или Windows не смогла запустить клиент.")


class SingleInstance:
    def __init__(self):
        self.handle = kernel.CreateMutexW(None, False, "Local\\ZapretClient.Desktop.v1")
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        self.already_running = ct.get_last_error() == 183

    def close(self):
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessJob:
    """Assign a suspended root to a job before it can spawn detached children.

    Closing this job stops only its members, including children started by BAT
    START. The process ID alone is never used as ownership evidence.
    """
    def __init__(self):
        self.name = "Local\\ZapretClient.Job." + uuid.uuid4().hex
        self.handle = kernel.CreateJobObjectW(None, self.name)
        self.process = None
        self.root_pid = None
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ct.byref(limits), ct.sizeof(limits)):
            error = ct.WinError(ct.get_last_error())
            self.close()
            raise error

    def start(self, command: str | list[str], cwd: Path, log_path: Path,
              env: dict[str, str] | None = None, *, stderr_path: Path | None = None):
        if self.process:
            raise RuntimeError("A job can start only one root process")
        line = subprocess.list2cmdline(command) if isinstance(command, list) else command
        startup, info = STARTUPINFO(), PROCESS_INFORMATION()
        startup.cb = ct.sizeof(startup)
        startup.dwFlags = 0x100 | 1  # USESTDHANDLES | USESHOWWINDOW
        startup.wShowWindow = 0
        environment = os.environ.copy()
        environment.update(env or {})
        block = ct.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
        with ExitStack() as stack:
            output = stack.enter_context(log_path.open("ab", buffering=0))
            stdin = stack.enter_context(open(os.devnull, "rb"))
            errors = stack.enter_context(stderr_path.open("ab", buffering=0)) if stderr_path else output
            handles = [msvcrt.get_osfhandle(f.fileno()) for f in (stdin, output, errors)]
            try:
                for handle in set(handles):
                    os.set_handle_inheritable(handle, True)
                startup.hStdInput = handles[0]
                startup.hStdOutput, startup.hStdError = handles[1], handles[2]
                # CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
                if not kernel.CreateProcessW(None, ct.create_unicode_buffer(line), None, None, True,
                                             0x4 | 0x08000000 | 0x400, block, str(cwd),
                                             ct.byref(startup), ct.byref(info)):
                    raise ct.WinError(ct.get_last_error())
            finally:
                for handle in set(handles):
                    os.set_handle_inheritable(handle, False)
        try:
            if not kernel.AssignProcessToJobObject(self.handle, info.hProcess):
                raise ct.WinError(ct.get_last_error())
            if kernel.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ct.WinError(ct.get_last_error())
            self.process, self.root_pid = info.hProcess, info.dwProcessId
        except Exception:
            kernel.TerminateProcess(info.hProcess, 1)
            kernel.CloseHandle(info.hProcess)
            self.close()
            raise
        finally:
            kernel.CloseHandle(info.hThread)

    def poll(self) -> int | None:
        if not self.process:
            return None
        # An actual exit code 259 must not be mistaken for a running process.
        if kernel.WaitForSingleObject(self.process, 0) == 258:
            return None
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(self.process, ct.byref(code)):
            raise ct.WinError(ct.get_last_error())
        return code.value

    def pids(self) -> set[int]:
        if not self.handle:
            return set()
        capacity = 64
        while capacity <= 65536:
            buffer = ct.create_string_buffer(8 + capacity * ct.sizeof(SIZE_T))
            if kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None):
                count = wt.DWORD.from_buffer(buffer, 4).value
                return set((SIZE_T * count).from_buffer(buffer, 8))
            if ct.get_last_error() != 234:
                raise ct.WinError(ct.get_last_error())
            capacity *= 2
        raise ClientError("Слишком много дочерних процессов для контроля запуска.")

    def stop(self):
        if self.handle:
            if not kernel.TerminateJobObject(self.handle, 0):
                raise ct.WinError(ct.get_last_error())
            deadline = time.monotonic() + 5
            while self.pids() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.pids():
                raise ClientError("Windows ещё завершает процессы zapret. Повторите остановку.")
        self.close()

    def close(self):
        if self.process:
            kernel.CloseHandle(self.process)
            self.process = None
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle = None


def winws_processes() -> list[dict]:
    found = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if (process.info["name"] or "").casefold() == "winws.exe":
                found.append({"pid": process.pid, "exe": process.exe(), "created": process.create_time()})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            if (process.info.get("name") or "").casefold() == "winws.exe":
                raise ClientError("Недостаточно прав для проверки уже работающего winws.exe.") from exc
    return found


def check_conflicts(allowed: set[int] | None = None):
    if not is_admin():
        raise ClientError("Для запуска и диагностики нужны права администратора. Нажмите «Права администратора».")
    try:
        psutil.win_service_get("zapret").status()
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as exc:
        raise ClientError("Не удалось проверить службу zapret: недостаточно прав.") from exc
    else:
        raise ClientError("Установлена служба zapret. Управление её установкой в v0.1 не входит. "
                          "Для запуска батников сначала удалите её через service.bat → Remove Services.")
    foreign = [p for p in winws_processes() if p["pid"] not in (allowed or set())]
    if foreign:
        raise ClientError("Уже работает zapret, запущенный вне клиента (PID " +
                          ", ".join(str(p["pid"]) for p in foreign) +
                          "). Закройте его перед запуском или тестированием.")


def batch_command(path: Path) -> str:
    cmd = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "cmd.exe")
    return f'"{cmd}" /d /v:off /s /c ""{path}""'


def powershell_path() -> Path:
    path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not path.is_file():
        raise ClientError("Не найден Windows PowerShell.")
    return path
