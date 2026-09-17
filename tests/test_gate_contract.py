"""Contract tests for the archive gate's arguments.

The gate itself needs docker and the pinned image, so the run it performs cannot live in
this suite. What can live here is everything it decides *before* starting a container —
and that is where its two worst failure modes were: an empty archive argument silently
rebuilt this tree instead of gating the bytes it was handed, and a hash was printed but
never compared. Both are one `if` away from returning green on the wrong archive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

LIVE_RUN = Path(__file__).resolve().parent.parent / "scripts" / "live_run"
REPEATS = LIVE_RUN / "run_repeats.sh"
MATRIX = LIVE_RUN / "gate_archive.sh"


def sh(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if not shutil.which("sh"):
        pytest.skip("no POSIX shell here; the gate is a shell script")
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "some.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("run.sh", "#!/bin/sh\n")
    return path


def test_an_empty_archive_argument_is_refused(tmp_path: Path) -> None:
    """A caller whose variable did not expand must not get a rebuild of this tree."""
    result = sh(REPEATS, "script", "8999", "10", "contract", "1", "")

    assert result.returncode == 2
    assert "archive argument is empty" in result.stderr


def test_a_missing_archive_is_refused(tmp_path: Path) -> None:
    result = sh(REPEATS, "script", "8999", "10", "contract", "1", str(tmp_path / "absent.zip"))

    assert result.returncode == 2
    assert "no such archive" in result.stderr


def test_other_bytes_than_expected_are_refused(archive: Path) -> None:
    """The guard that caught the candidate being rebuilt under the same path."""
    result = sh(
        REPEATS,
        "script",
        "8999",
        "10",
        "contract",
        "1",
        str(archive),
        env={"EXPECT_SHA256": "0" * 64},
    )

    assert result.returncode == 2
    assert "expected sha256" in result.stderr
    assert "sha256:" in result.stdout  # it says what it actually saw


def test_the_matrix_without_an_archive_explains_itself() -> None:
    result = sh(MATRIX)

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_the_matrix_refuses_a_missing_archive(tmp_path: Path) -> None:
    result = sh(MATRIX, str(tmp_path / "absent.zip"))

    assert result.returncode == 2
    assert "no such archive" in result.stderr
