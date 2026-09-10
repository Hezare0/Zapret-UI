"""Build with an isolated process environment and fresh analysis directory."""

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support.policy import clean_build_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if (output / "ZapretClient").exists():
        parser.error("Output exists. Choose a new output directory; existing files are preserved.")
    run = project / "build" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run.mkdir(parents=True)
    env = clean_build_environment()
    env["ZAPRET_BUILD_AUDIT"] = str(run / "binary-origins.json")
    command = [sys.executable, "-m", "PyInstaller", "--workpath", str(run),
               "--distpath", str(output), str(project / "ZapretClient.spec")]
    print("Building with an isolated PATH. Binary audit:", env["ZAPRET_BUILD_AUDIT"], flush=True)
    return subprocess.run(command, cwd=project, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
