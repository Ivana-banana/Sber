"""Contract tests for the fixture-answer guard.

The shapes below are synthetic on purpose: the guard must not depend on the names,
paths or values of any real task.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval.check_fixture_answers import main, unreachable


def make_task(root: Path, name: str, evidence: str, expected: str) -> Path:
    task = root / name
    (task / "environment" / "case").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")
    (task / "environment" / "case" / "trail.log").write_text(evidence)
    (task / "tests" / "expected_report.txt").write_text(expected)
    return task


def test_an_answer_absent_from_the_environment_is_reported(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "alpha",
        evidence="09:00 login ok from 10.0.0.1\n",
        expected="source_ip=203.0.113.7\nactor=roland\n",
    )

    assert unreachable(task) == ["203.0.113.7", "roland"]


def test_an_answer_present_in_any_environment_file_is_not_reported(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "beta",
        evidence="09:00 login ok for roland from 203.0.113.7\n",
        expected="source_ip=203.0.113.7\nactor=roland\n",
    )

    assert unreachable(task) == []


def test_answers_stated_only_in_the_checker_are_covered(tmp_path: Path) -> None:
    task = make_task(tmp_path, "gamma", evidence="nothing of interest\n", expected="")
    (task / "tests" / "check.py").write_text(
        "expected = {'source_ip': '203.0.113.7'}\nother = {'source_ip': '198.51.100.9'}\n"
    )

    assert unreachable(task) == ["203.0.113.7"]


def test_a_task_without_a_tests_directory_is_silent(tmp_path: Path) -> None:
    task = tmp_path / "delta"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")

    assert unreachable(task) == []


def test_the_exit_code_is_the_gate(tmp_path: Path, capsys) -> None:
    make_task(
        tmp_path,
        "alpha",
        evidence="09:00 login ok from 10.0.0.1\n",
        expected="source_ip=203.0.113.7\n",
    )

    assert main(["check_fixture_answers.py", str(tmp_path)]) == 1
    assert "UNREACHABLE alpha" in capsys.readouterr().out

    make_task(
        tmp_path / "clean",
        "beta",
        evidence="09:00 login ok from 203.0.113.7\n",
        expected="source_ip=203.0.113.7\n",
    )

    assert main(["check_fixture_answers.py", str(tmp_path / "clean")]) == 0
