"""Download and verify the official portable MinGit used by release builds."""

import hashlib
from pathlib import Path
import urllib.request
import zipfile

MINGIT_VERSION = "2.55.0.5"
MINGIT_SHA256 = "56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e"
MINGIT_URL = "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/MinGit-2.55.0.5-64-bit.zip"


def prepare_mingit(cache: Path, destination: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"MinGit-{MINGIT_VERSION}-64-bit.zip"
    if not archive.is_file():
        request = urllib.request.Request(MINGIT_URL, headers={"User-Agent": "Zapret-UI-build"})
        with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != MINGIT_SHA256:
        raise RuntimeError(f"MinGit SHA-256 mismatch: {archive}. Refusing to package this file.")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("MinGit archive contains an unsafe path")
        source.extractall(destination)
    if not (destination / "cmd/git.exe").is_file():
        raise RuntimeError("MinGit executable not found in verified archive")
    return destination
