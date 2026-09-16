"""The audit that would have caught two fixtures shipping without their evidence.

The defect it reproduces: `local_task_extra/tools/run_reference_suite.sh` runs
`solution/solve.sh` (which prints the expected answer) against `tests/test.sh`, so it
passes whether or not the answer is present in `environment/`. Two forensics packages
shipped with their logs missing and the suite still reported 18/18.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = ROOT / "local_task_extra" / "tools" / "fixture_solvability_audit.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("fixture_solvability_audit", AUDIT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_module = load_audit()


def make_task(root: Path, name: str, expected: dict[str, str], evidence: str) -> Path:
    """A minimal package shaped like the shipped ones: manifest, checker, environment."""
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(f'[task]\nname = "local/{name}"\n')
    lines = "".join(f"{k}={v}\n" for k, v in expected.items())
    (task / "tests" / "expected_incident_report.txt").write_text(lines)
    (task / "environment" / "evidence.log").write_text(evidence)
    return task


def test_missing_evidence_is_reported(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "forensics-lost-log",
        {"attacker_ip": "198.51.100.44", "compromised_user": "deploy"},
        "some unrelated line about deploy\n",
    )
    assert audit_module.missing_values(task) == ["198.51.100.44"]


def test_present_evidence_passes(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "forensics-complete",
        {"attacker_ip": "198.51.100.44", "compromised_user": "deploy"},
        "Failed password for deploy from 198.51.100.44 port 51002\n",
    )
    assert audit_module.missing_values(task) == []


def test_values_are_read_from_a_check_py_dict(tmp_path: Path) -> None:
    """Most shipped checkers keep the answer in a dict literal, not a separate file."""
    task = tmp_path / "forensics-inline-checker"
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text('[task]\nname = "local/forensics-inline-checker"\n')
    (task / "tests" / "check.py").write_text(
        "expected = {'attacker_ip': '203.0.113.9', 'user': 'guest'}\n"
    )
    (task / "environment" / "case.txt").write_text("guest logged in\n")
    assert audit_module.missing_values(task) == ["203.0.113.9"]


def test_behavioural_tasks_are_skipped_not_passed(tmp_path: Path) -> None:
    """A `fix-*` package states no literal answer; counting it as a pass would hide
    the fact that nothing was checked."""
    task = tmp_path / "fix-something"
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text('[task]\nname = "local/fix-something"\n')
    (task / "tests" / "check.py").write_text("import subprocess\nsubprocess.run(['pytest'])\n")
    (task / "environment" / "app.py").write_text("print('hi')\n")
    failures, skipped = audit_module.audit(tmp_path)
    assert failures == {}
    assert skipped == ["fix-something"]


@pytest.mark.skipif(
    not (ROOT / "local_task_extra" / "tasks").is_dir(), reason="extra set not present"
)
def test_the_shipped_extra_set_is_solvable() -> None:
    failures, skipped = audit_module.audit(ROOT / "local_task_extra" / "tasks")
    assert failures == {}, f"fixtures whose answer is not in environment/: {failures}"
    assert len(skipped) < 18, "no task declared a literal answer -- the audit found nothing to check"
