"""The static fixture audit must stay green, and stay diagnosable.

It had been failing all eighteen extra packages for days — a stale `memory_mb` figure —
and printed `invalid manifest: ` with an empty reason, so nothing in the repository
noticed. Nothing ran it either. This test is what makes the next drift visible.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent / "local_task_extra" / "tools" / "static_fixture_audit.py"


def run_audit(root: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT)],
        cwd=root or AUDIT.parent,
        capture_output=True,
        text=True,
    )


def test_the_shipped_fixtures_pass_the_static_audit() -> None:
    result = run_audit()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATIC_PASS packages=18" in result.stdout


def copy_fixture_set(tmp_path: Path) -> Path:
    """A writable copy of the packages, so a probe can break one without touching git.

    A symbolic link back into the repository would have let any future version of the
    audit write through it into the real fixtures.
    """
    root = tmp_path / "set"
    (root / "tools").mkdir(parents=True)
    shutil.copy(AUDIT, root / "tools" / AUDIT.name)
    shutil.copytree(AUDIT.parent.parent / "tasks", root / "tasks")
    return root


def test_a_drifted_manifest_fails_the_audit(tmp_path: Path) -> None:
    """The direction that actually happened: a package leaves the declared envelope."""
    root = copy_fixture_set(tmp_path)
    manifest = root / "tasks" / "mut-greeting-farewell" / "task.toml"
    manifest.write_text(manifest.read_text().replace("memory_mb = 2048", "memory_mb = 512"))

    result = subprocess.run([sys.executable, str(root / "tools" / AUDIT.name)], capture_output=True, text=True)

    assert result.returncode == 1
    assert "mut-greeting-farewell: invalid manifest: memory_mb 512" in result.stderr


def test_a_package_that_lost_a_file_is_reported_not_fatal(tmp_path: Path) -> None:
    """The incident itself: the audit must name the loss, not die on it."""
    root = copy_fixture_set(tmp_path)
    (root / "tasks" / "ctf-header-override" / "tests" / "test.sh").unlink()

    result = subprocess.run([sys.executable, str(root / "tools" / AUDIT.name)], capture_output=True, text=True)

    assert result.returncode == 1
    assert "ctf-header-override: missing tests/test.sh" in result.stderr
    assert "Traceback" not in result.stderr


def test_every_manifest_assertion_names_the_value_that_broke(tmp_path: Path) -> None:
    """A failure with an empty reason is how the stale figure survived unread."""
    root = copy_fixture_set(tmp_path)
    manifest = root / "tasks" / "ctf-signed-cookie" / "task.toml"
    manifest.write_text(manifest.read_text().replace("cpus = 1", "cpus = 4"))

    result = subprocess.run([sys.executable, str(root / "tools" / AUDIT.name)], capture_output=True, text=True)

    assert result.returncode == 1
    assert "invalid manifest: cpus 4" in result.stderr


def test_a_missing_package_is_named(tmp_path: Path) -> None:
    root = copy_fixture_set(tmp_path)
    shutil.rmtree(root / "tasks" / "ctf-duplicate-parameter")

    result = subprocess.run([sys.executable, str(root / "tools" / AUDIT.name)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "ctf-duplicate-parameter" in result.stderr
