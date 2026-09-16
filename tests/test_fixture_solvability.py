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


def test_zero_taxonomy_defaults_to_the_dev_side() -> None:
    """The dev/holdout discipline should not rest on remembering `--only`.

    An opened holdout log cannot be un-opened by deciding to ignore it, so a run made
    entirely of extra tasks is narrowed to dev unless `--all` says otherwise.
    """
    spec = importlib.util.spec_from_file_location(
        "zero_taxonomy", ROOT / "scripts" / "eval" / "zero_taxonomy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    extra = {p.name for p in (ROOT / "local_task_extra" / "tasks").iterdir() if p.is_dir()}
    dev = module.dev_side_default(extra)
    assert dev is not None and dev, "an all-extra run must be narrowed"
    assert "forensics-ssh-bruteforce" not in dev, "a holdout task leaked into the default scope"
    assert "forensics-webshell-chain" in dev

    # A public run has no holdout to protect and must be left alone.
    assert module.dev_side_default({"hello-file", "bye-file", "find-sqli-login"}) is None
    # So must a mixed run: narrowing it would silently drop the public half.
    assert module.dev_side_default({"hello-file", "fix-cache-key-scope"}) is None


def load_vocabulary_check():
    spec = importlib.util.spec_from_file_location(
        "check_fixture_vocabulary", ROOT / "scripts" / "check_fixture_vocabulary.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture_package(root: Path, name: str, checker: str, instruction: str = "") -> None:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(f'[task]\nname = "local/{name}"\n')
    (task / "tests" / "check.py").write_text(checker)
    if instruction:
        (task / "instruction.md").write_text(instruction)


def test_a_playbook_line_repeating_a_checker_literal_is_caught(tmp_path: Path) -> None:
    """The C7 rule: a prompt may repeat the task's own words, never the checker's."""
    check = load_vocabulary_check()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fixture_package(tasks, "audit-made-up", "expected = {'location': 'app/views.py:118'}\n")
    vocab = check.vocabulary((tasks,))
    assert "app/views.py:118" in vocab

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "playbooks.py").write_text(
        'AUDIT = "report the finding as app/views.py:118"\n'
    )
    hits = check.scan(vocab, agent_dir)
    assert hits and "app/views.py:118" in hits[0]


def test_the_honest_form_of_the_same_line_passes(tmp_path: Path) -> None:
    check = load_vocabulary_check()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fixture_package(tasks, "audit-made-up", "expected = {'location': 'app/views.py:118'}\n")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "playbooks.py").write_text(
        'AUDIT = "repeat the location format the task asks for"\n'
    )
    assert check.scan(check.vocabulary((tasks,)), agent_dir) == []


def test_an_experiment_note_in_a_docstring_is_not_a_hit(tmp_path: Path) -> None:
    """Attribute docstrings are where fixture names legitimately live (PEP 258)."""
    check = load_vocabulary_check()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fixture_package(tasks, "audit-made-up", "expected = {'location': 'app/views.py:118'}\n")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "limits.py").write_text(
        'BRIEF_MAX = 8\n"""Chosen after app/views.py:118 in audit-made-up scored zero."""\n'
    )
    assert check.scan(check.vocabulary((tasks,)), agent_dir) == []
