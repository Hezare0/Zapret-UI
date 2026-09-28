"""Own Standard-test probes; no upstream PowerShell script is executed."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .repository import ClientError
from .results import Target
from .updates import _SPAWN_LOCK


DEFAULT_TARGETS = (
    ("DiscordMain", "https://discord.com"),
    ("DiscordGateway", "https://gateway.discord.gg"),
    ("DiscordCDN", "https://cdn.discordapp.com"),
    ("DiscordUpdates", "https://updates.discord.com"),
    ("YouTubeWeb", "https://www.youtube.com"),
    ("YouTubeShort", "https://youtu.be"),
    ("YouTubeImage", "https://i.ytimg.com"),
    ("YouTubeVideoRedirect", "https://redirector.googlevideo.com"),
    ("GoogleMain", "https://www.google.com"),
    ("GoogleGstatic", "https://www.gstatic.com"),
    ("CloudflareWeb", "https://www.cloudflare.com"),
    ("CloudflareCDN", "https://cdnjs.cloudflare.com"),
    ("CloudflareDNS1111", "PING:1.1.1.1"),
    ("CloudflareDNS1001", "PING:1.0.0.1"),
    ("GoogleDNS8888", "PING:8.8.8.8"),
    ("GoogleDNS8844", "PING:8.8.4.4"),
    ("Quad9DNS9999", "PING:9.9.9.9"),
)
PROTOCOL_ARGS = {
    "HTTP": ("--http1.1",),
    "TLS1.2": ("--tlsv1.2", "--tls-max", "1.2"),
    "TLS1.3": ("--tlsv1.3", "--tls-max", "1.3"),
}
TARGET_LINE = re.compile(r'^\s*(\w+)\s*=\s*"(.+)"\s*$', re.UNICODE)
PING_TIME = re.compile(r"(?i)(?:time|время)\s*[=<]\s*(\d+)")
SSL_ERROR = re.compile(r"could not resolve host|certificate|self[- ]?signed|unable to get local issuer", re.I)
UNSUPPORTED = re.compile(r"does not support|not supported|unsupported|unrecognized option|unknown option|schannel", re.I)


@dataclass(frozen=True)
class ProbeTarget:
    name: str
    url: str | None
    ping_host: str


class ProbeCancelled(Exception):
    pass


def load_targets(path: Path) -> list[ProbeTarget]:
    raw = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = TARGET_LINE.match(line)
            if match:
                raw[match[1]] = match[2]
    if not raw:
        raw = dict(DEFAULT_TARGETS)
    targets = []
    for name, value in raw.items():
        if value.upper().startswith("PING:"):
            host = value[5:].strip()
            if not host:
                raise ClientError(f"В targets.txt пустой PING-адрес у {name}.")
            targets.append(ProbeTarget(name, None, host))
        else:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ClientError(f"В targets.txt некорректный URL у {name}: {value}")
            targets.append(ProbeTarget(name, value, url.hostname))
    return targets


def system_tool(name: str) -> str:
    system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / name
    if system.is_file():
        return str(system)
    found = shutil.which(name)
    if found:
        return found
    raise ClientError(f"Не найден {name}; Standard-тесты требуют его в Windows.")


def probe_parallelism() -> int:
    raw = os.environ.get("ZAPRET_PROBE_PARALLEL", "32")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ClientError("ZAPRET_PROBE_PARALLEL должен быть числом от 1 до 48.") from exc
    if not 1 <= value <= 48:
        raise ClientError("ZAPRET_PROBE_PARALLEL должен быть числом от 1 до 48.")
    return value


def _execute(command: list[str], cancel: threading.Event, timeout: float) -> tuple[int, bytes, bytes]:
    if cancel.is_set():
        raise ProbeCancelled()
    process = None
    try:
        # PyInstaller's Qt DLL directory can poison system curl/ping children.
        with _SPAWN_LOCK:
            bundled = getattr(sys, "_MEIPASS", None)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            if bundled and not kernel.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                if bundled:
                    kernel.SetDllDirectoryW(str(bundled))
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set():
                raise ProbeCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return -1, b"", b"probe timed out"
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process and process.poll() is None:
            process.kill()
            process.communicate()


def curl_probe(curl: str, target: ProbeTarget, label: str, cancel: threading.Event,
               timeout: float = 4.0) -> str:
    command = [curl, "-I", "-s", "-m", str(timeout), "--connect-timeout", str(min(2, timeout)),
               "-o", "NUL", "-w", "%{http_code}", "--show-error", *PROTOCOL_ARGS[label], target.url]
    try:
        code, _, error_bytes = _execute(command, cancel, timeout + 1)
    except ProbeCancelled:
        raise
    except OSError:
        return "ERROR"
    error = error_bytes.decode("utf-8", errors="replace")
    if SSL_ERROR.search(error):
        return "SSL"
    if code == 35 or UNSUPPORTED.search(error):
        return "UNSUP"
    return "OK" if code == 0 else "ERROR"


def ping_probe(ping: str, target: ProbeTarget, cancel: threading.Event) -> tuple[float | None, str]:
    try:
        code, output, _ = _execute([ping, "-n", "1", "-w", "1000", target.ping_host], cancel, 3)
    except ProbeCancelled:
        raise
    except OSError:
        return None, "Timeout"
    if code != 0:
        return None, "Timeout"
    value = PING_TIME.search(output.decode("oem", errors="replace"))
    if value:
        number = max(1, int(value[1]))
        return float(number), f"{number} ms"
    return None, "Timeout"


def run_probes(targets: list[ProbeTarget], curl: str, ping: str, cancel: threading.Event,
               on_target, *, max_workers: int | None = None) -> list[Target]:
    """Probe every protocol and ping concurrently; emit each complete target."""
    workers = max_workers or probe_parallelism()
    slots = [{"protocols": {}, "ping": None, "left": 4 if t.url else 1} for t in targets]
    results = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zapret-probe") as pool:
        futures = {}
        for index, target in enumerate(targets):
            if target.url:
                for label in PROTOCOL_ARGS:
                    futures[pool.submit(curl_probe, curl, target, label, cancel)] = (index, label)
            futures[pool.submit(ping_probe, ping, target, cancel)] = (index, "ping")
        for future in as_completed(futures):
            if cancel.is_set():
                raise ProbeCancelled()
            index, label = futures[future]
            value = future.result()
            slot = slots[index]
            if label == "ping":
                slot["ping"] = value
            else:
                slot["protocols"][label] = value
            slot["left"] -= 1
            if slot["left"] == 0:
                ping_ms, ping_text = slot["ping"]
                target = Target(targets[index].name, slot["protocols"], ping_ms, ping_text)
                results[index] = target
                on_target(target)
    return results


def format_target(target: Target) -> str:
    if not target.protocols:
        return f"{target.name:<25} Ping: {target.ping_text}"
    return (f"{target.name:<25} HTTP:{target.protocols['HTTP']:<5} "
            f"TLS1.2:{target.protocols['TLS1.2']:<5} TLS1.3:{target.protocols['TLS1.3']:<5} "
            f"| Ping: {target.ping_text}")
