"""Contract tests for the fixture guard.

The synthetic fixtures below carry no name, path or value of a real task. The one test
that does touch the repository is deliberate: the regression this guard exists for is
"an evidence file disappeared from a real fixture", and only a run against the real
tree can fail when that happens again.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts.eval.check_fixture_answers import expected_answers, main, reachable


def make_task(root: Path, name: str, evidence: str, expected: str = "") -> Path:
    task = root / name
    (task / "environment" / "case").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")
    (task / "environment" / "case" / "trail.log").write_text(evidence)
    if expected:
        (task / "tests" / "expected_report.txt").write_text(expected)
    return task


def warnings_of(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("warning:")]


def test_an_answer_absent_from_the_environment_is_reported(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "alpha",
        evidence="09:00 login ok from 10.0.0.1\n",
        expected="source_ip=203.0.113.7\nactor=roland\n",
    )

    assert sorted(a for a in expected_answers(task, []) if not reachable(a, "")) == [
        "203.0.113.7",
        "roland",
    ]


def test_an_answer_present_in_any_environment_file_is_not_reported(tmp_path: Path, capsys) -> None:
    make_task(
        tmp_path,
        "beta",
        evidence="09:00 login ok for roland from 203.0.113.7\n",
        expected="source_ip=203.0.113.7\nactor=roland\n",
    )

    assert main(["check_fixture_answers.py", str(tmp_path)]) == 0
    assert "1 state a verbatim answer" in capsys.readouterr().out


def test_a_checker_dict_under_any_answer_name_is_read(tmp_path: Path) -> None:
    """Three of the four forensics checkers here call it `exp`, not `expected`."""
    task = make_task(tmp_path, "gamma", evidence="nothing of interest\n")
    (task / "tests" / "check.py").write_text("exp = {'source_ip': '203.0.113.7'}\n")

    assert expected_answers(task, []) == {"203.0.113.7"}


def test_an_anonymous_dict_is_not_an_answer(tmp_path: Path) -> None:
    """Attack payloads are passed inline and are meant to be absent from the evidence."""
    task = make_task(tmp_path, "delta", evidence="nothing of interest\n")
    (task / "tests" / "check.py").write_text(
        "post('/login', json={'username': \"admin'--\", 'password': 'x'})\n"
    )

    assert expected_answers(task, []) == set()


def test_a_longer_token_does_not_satisfy_an_answer() -> None:
    assert not reachable("198.51.100.44", "denied 198.51.100.440 at the edge")
    assert not reachable("deploy", "run the deployment on the operations host")
    assert reachable("198.51.100.44", "from 198.51.100.44 port 51003")


def test_an_answer_split_across_two_files_is_not_reachable(tmp_path: Path) -> None:
    task = make_task(tmp_path, "epsilon", evidence="tail 198.51.\n", expected="ip=198.51.100.44\n")
    (task / "environment" / "case" / "second.log").write_text("100.44 head\n")

    answers = expected_answers(task, [])
    from scripts.eval.check_fixture_answers import evidence as evidence_text

    assert not reachable(next(iter(answers)), evidence_text(task, []))


def test_prose_containing_an_equals_sign_is_not_an_answer(tmp_path: Path) -> None:
    task = make_task(tmp_path, "zeta", evidence="quiet\n")
    (task / "tests" / "expected_report.md").write_text(
        "# Report =====\n<!-- e.g. key=value -->\nsource_ip=203.0.113.7\n"
    )

    assert expected_answers(task, []) == {"203.0.113.7"}


def test_a_json_expectation_is_read(tmp_path: Path) -> None:
    task = make_task(tmp_path, "eta", evidence="quiet\n")
    (task / "tests" / "expected.json").write_text('{"source_ip": "203.0.113.7"}')

    assert expected_answers(task, []) == {"203.0.113.7"}


def test_checking_nothing_is_not_a_pass(tmp_path: Path, capsys) -> None:
    assert main(["check_fixture_answers.py", str(tmp_path / "absent")]) == 2
    assert "nothing was checked" in capsys.readouterr().out


def test_a_task_directory_without_a_manifest_is_announced(tmp_path: Path, capsys) -> None:
    orphan = tmp_path / "theta"
    (orphan / "tests").mkdir(parents=True)
    make_task(tmp_path, "iota", evidence="203.0.113.7\n", expected="ip=203.0.113.7\n")

    main(["check_fixture_answers.py", str(tmp_path)])

    assert any("no task.toml" in line for line in warnings_of(capsys.readouterr().out))


def test_answers_are_masked_unless_asked_for(tmp_path: Path, capsys) -> None:
    make_task(tmp_path, "kappa", evidence="quiet\n", expected="ip=203.0.113.7\n")

    main(["check_fixture_answers.py", str(tmp_path)])
    assert "203.0.113.7" not in capsys.readouterr().out

    main(["check_fixture_answers.py", "--show-values", str(tmp_path)])
    assert "203.0.113.7" in capsys.readouterr().out


def test_a_fixture_file_git_does_not_track_is_reported(tmp_path: Path, capsys) -> None:
    if not shutil.which("git"):
        return
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    make_task(repo, "lambda", evidence="203.0.113.7\n", expected="ip=203.0.113.7\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    (repo / "lambda" / "environment" / "case" / "late.log").write_text("203.0.113.8\n")

    assert main(["check_fixture_answers.py", str(repo)]) == 1
    assert "UNTRACKED" in capsys.readouterr().out


def test_the_repository_fixtures_are_clean(capsys) -> None:
    """The regression itself: an evidence file lost from a real task must fail here."""
    code = main(["check_fixture_answers.py"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "checked 24 fixtures" in out
