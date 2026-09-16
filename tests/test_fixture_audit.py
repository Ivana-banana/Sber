"""The static fixture audit must stay green, and stay diagnosable.

It had been failing all eighteen extra packages for days — a stale `memory_mb` figure —
and printed `invalid manifest: ` with an empty reason, so nothing in the repository
noticed. Nothing ran it either. This test is what makes the next drift visible.
"""

from __future__ import annotations

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


def test_a_manifest_failure_says_which_value_broke(tmp_path: Path) -> None:
    """A failure with an empty reason is how the stale figure survived unread."""
    source = AUDIT.read_text()
    broken = source.replace("== 2048", "== 256")
    tools = tmp_path / "tools"
    tools.mkdir()
    probe = tools / "audit.py"
    probe.write_text(broken)
    (tmp_path / "tasks").symlink_to(AUDIT.parent.parent / "tasks", target_is_directory=True)

    result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)

    assert result.returncode == 1
    assert "memory_mb 2048" in result.stderr
