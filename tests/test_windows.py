import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from zapret_client.windows import ProcessJob, batch_command


def test_job_tracks_detached_batch_child_and_stops_only_owned_processes(tmp_path):
    script = tmp_path / "general (SAFE FIXTURE).bat"
    child = tmp_path / "child.py"
    ready = tmp_path / "ready.txt"
    child.write_text("import os, pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n")
    script.write_text(f'@echo off\nstart "" /b "{sys.executable}" "{child}" "{ready}"\nexit /b 0\n')
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
    job = ProcessJob()
    try:
        job.start(batch_command(script), tmp_path, tmp_path / "process.log")
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), (tmp_path / "process.log").read_bytes()
        child_pid = int(ready.read_text())
        deadline = time.monotonic() + 3
        while job.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job.poll() == 0
        assert child_pid in job.pids()
        child_process = psutil.Process(child_pid)
        job.stop()
        assert child_process.wait(timeout=3) is not None
        assert outsider.poll() is None
    finally:
        job.stop()
        outsider.terminate()
        outsider.wait(timeout=5)


def test_nonzero_exit_is_observable(tmp_path):
    job = ProcessJob()
    try:
        job.start([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, tmp_path / "exit.log")
        deadline = time.monotonic() + 5
        while job.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert job.poll() == 7
    finally:
        job.stop()
