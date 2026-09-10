"""Keep unrelated desktop tools out of the frozen application's DLL graph."""

import os
from pathlib import Path
import sys


def allowed_binary_roots() -> tuple[Path, ...]:
    return tuple(Path(value).resolve() for value in
                 (sys.prefix, sys.base_prefix, os.environ["SystemRoot"]))


def clean_build_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    # Windows environment keys are case-insensitive; a plain dict is not.
    env = {key.upper(): value for key, value in (os.environ if environment is None else environment).items()}
    windows = Path(env["SYSTEMROOT"])
    env["PATH"] = os.pathsep.join(str(path) for path in (
        Path(sys.prefix) / "Scripts", Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0",
    ))
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QML_IMPORT_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM"):
        env.pop(key, None)
    return env


def validate_binary_origins(binaries, roots: tuple[Path, ...] | None = None) -> list[dict[str, str]]:
    roots = roots or allowed_binary_roots()
    audit = []
    for destination, source, kind in binaries:
        origin = Path(source).resolve()
        if not any(origin.is_relative_to(root.resolve()) for root in roots):
            raise RuntimeError(f"Unexpected binary origin: {destination} <- {origin}")
        # Qt uses the unversioned ICU API provided by Windows 10/11. Poppler's
        # identically named library exports a different, version-suffixed ABI.
        if Path(destination).name.casefold() == "icuuc.dll":
            raise RuntimeError("icuuc.dll must be resolved by Windows, not bundled beside the application")
        audit.append({"destination": destination, "source": str(origin), "kind": kind})
    return audit
