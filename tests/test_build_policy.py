from pathlib import Path

import pytest

from build_support.policy import clean_build_environment, validate_binary_origins
from build_support.vendor import MINGIT_VERSION, prepare_mingit


@pytest.mark.parametrize("root_key", ["SystemRoot", "SYSTEMROOT"])
def test_build_does_not_inherit_foreign_dll_or_qt_search_paths(root_key):
    environment = {root_key: "C:\\Windows", "PATH": "C:\\foreign-poppler",
                   "QT_PLUGIN_PATH": "C:\\foreign-qt", "PYTHONPATH": "C:\\foreign-python",
                   "QT_QPA_PLATFORM": "offscreen"}
    actual = clean_build_environment(environment)
    assert "foreign" not in actual["PATH"]
    assert "System32" in actual["PATH"]
    assert "QT_PLUGIN_PATH" not in actual
    assert "PYTHONPATH" not in actual
    assert "QT_QPA_PLATFORM" not in actual
    assert environment["PATH"] == "C:\\foreign-poppler"


def test_foreign_binary_origin_is_rejected(tmp_path):
    allowed = tmp_path / "venv"
    with pytest.raises(RuntimeError, match="Unexpected binary origin"):
        validate_binary_origins([("icuuc.dll", str(tmp_path / "poppler/icuuc.dll"), "BINARY")], (allowed,))


def test_shadowing_windows_icu_is_rejected_even_from_allowed_root(tmp_path):
    with pytest.raises(RuntimeError, match="must be resolved by Windows"):
        validate_binary_origins([("icuuc.dll", str(tmp_path / "icuuc.dll"), "BINARY")], (tmp_path,))


def test_expected_package_binary_is_audited(tmp_path):
    records = validate_binary_origins([("PySide6/Qt6Core.dll", str(tmp_path / "Qt6Core.dll"), "BINARY")], (tmp_path,))
    assert records[0]["destination"] == "PySide6/Qt6Core.dll"


def test_unverified_mingit_is_never_extracted(tmp_path):
    archive = tmp_path / f"MinGit-{MINGIT_VERSION}-64-bit.zip"
    archive.write_bytes(b"wrong download")
    target = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        prepare_mingit(tmp_path, target)
    assert not target.exists()
