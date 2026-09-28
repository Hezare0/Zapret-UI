import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import zapret_client.probe_engine as engine
from zapret_client.repository import ClientError


def test_target_file_and_old_default_set(tmp_path):
    assert len(engine.load_targets(tmp_path / "missing.txt")) == 17
    source = tmp_path / "targets.txt"
    source.write_text('Web = "https://example.test/path"\nDNS = "PING:1.1.1.1"\nWeb = "https://new.test"\n')
    targets = engine.load_targets(source)
    assert [(t.name, t.url, t.ping_host) for t in targets] == [
        ("Web", "https://new.test", "new.test"), ("DNS", None, "1.1.1.1")]
    source.write_text('Bad = "file:///private/data"\n')
    with pytest.raises(ClientError, match="URL"):
        engine.load_targets(source)


def test_curl_status_matches_upstream_semantics(monkeypatch):
    target = engine.ProbeTarget("Web", "https://example.test", "example.test")
    commands = []
    def execute(command, *_):
        commands.append(command)
        return 0, b"403", b""
    monkeypatch.setattr(engine, "_execute", execute)
    cancel = threading.Event()
    assert engine.curl_probe("curl", target, "HTTP", cancel) == "OK"
    assert "--http1.1" in commands[-1] and "-I" in commands[-1]
    assert engine.curl_probe("curl", target, "TLS1.2", cancel) == "OK"
    assert commands[-1][-4:-1] == ["--tlsv1.2", "--tls-max", "1.2"]
    monkeypatch.setattr(engine, "_execute", lambda *a, **kw: (60, b"000", b"SSL certificate problem"))
    assert engine.curl_probe("curl", target, "HTTP", cancel) == "SSL"
    monkeypatch.setattr(engine, "_execute", lambda *a, **kw: (35, b"000", b"handshake failed"))
    assert engine.curl_probe("curl", target, "TLS1.3", cancel) == "UNSUP"
    monkeypatch.setattr(engine, "_execute", lambda *a, **kw: (28, b"000", b"Operation timed out"))
    assert engine.curl_probe("curl", target, "HTTP", cancel) == "ERROR"


def test_ping_numeric_and_timeout(monkeypatch):
    target = engine.ProbeTarget("DNS", None, "127.0.0.1")
    monkeypatch.setattr(engine, "_execute", lambda *a, **kw: (0, b"Reply: time<1ms TTL=128", b""))
    assert engine.ping_probe("ping", target, threading.Event()) == (1.0, "1 ms")
    monkeypatch.setattr(engine, "_execute", lambda *a, **kw: (1, b"Request timed out", b""))
    assert engine.ping_probe("ping", target, threading.Event()) == (None, "Timeout")


def test_protocols_and_targets_are_bounded_and_parallel(monkeypatch):
    targets = [engine.ProbeTarget(f"Web{i}", "https://example.test", "example.test") for i in range(4)]
    targets.append(engine.ProbeTarget("DNS", None, "1.1.1.1"))
    active = 0
    peak = 0
    lock = threading.Lock()
    def enter():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
    def curl(*args):
        enter()
        return "OK"
    def ping(*args):
        enter()
        return 45.0, "45 ms"
    monkeypatch.setattr(engine, "curl_probe", curl)
    monkeypatch.setattr(engine, "ping_probe", ping)
    received = []
    started = time.monotonic()
    results = engine.run_probes(targets, "curl", "ping", threading.Event(), received.append, max_workers=8)
    assert time.monotonic() - started < 0.5
    assert 2 <= peak <= 8
    assert len(received) == len(results) == 5
    assert all(result.ok and result.ping_ms == 45 for result in results)
    assert engine.format_target(results[0]).count(":OK") == 3


def test_cancel_kills_own_probe_process():
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(engine.ProbeCancelled):
        engine._execute([sys.executable, "-c", "import time; time.sleep(30)"], cancel, 10)
    timer.join()
    assert time.monotonic() - started < 2


def test_parallel_limit_validation(monkeypatch):
    monkeypatch.setenv("ZAPRET_PROBE_PARALLEL", "49")
    with pytest.raises(ClientError, match="1 до 48"):
        engine.probe_parallelism()


def test_real_windows_curl_and_ping_on_loopback():
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(403)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        targets = [engine.ProbeTarget("LocalWeb", f"http://127.0.0.1:{server.server_port}", "127.0.0.1"),
                   engine.ProbeTarget("LocalPing", None, "127.0.0.1")]
        results = engine.run_probes(targets, engine.system_tool("curl.exe"), engine.system_tool("ping.exe"),
                                    threading.Event(), lambda _: None, max_workers=8)
        assert len(results) == 2
        assert all(t.ok and t.ping_ms is not None for t in results)
        assert results[0].protocols == {"HTTP": "OK", "TLS1.2": "OK", "TLS1.3": "OK"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
