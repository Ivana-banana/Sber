"""Contract tests for the fixture guard.

The synthetic fixtures below carry no name, path or value of a real task. The one test
that does touch the repository is deliberate: the regression this guard exists for is
"an evidence file disappeared from a real fixture", and only a run against the real
tree can fail when that happens again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_an_answer_absent_from_the_environment_is_reported(tmp_path: Path, capsys) -> None:
    make_task(
        tmp_path,
        "alpha",
        evidence="09:00 login ok from 10.0.0.1\n",
        expected="source_ip=203.0.113.7\nactor=roland\n",
    )

    code = main(["check_fixture_answers.py", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "UNREACHABLE alpha" in out
    assert "'203.0.113.7'" in out and "'roland'" in out


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


def test_a_dict_named_like_an_answer_but_not_one_is_left_alone(tmp_path: Path) -> None:
    """`export`, `unexpected` and `experiment` all contain an answer word as a substring."""
    task = make_task(tmp_path, "delta-2", evidence="quiet\n")
    (task / "tests" / "check.py").write_text(
        "export = {'cmd': 'tar czf /tmp/x.tgz /app'}\n"
        "unexpected = {'payload': \"admin'--\"}\n"
        "experiment = {'note': 'see the README for the rationale'}\n"
    )

    assert expected_answers(task, []) == set()


def test_an_annotated_answer_dict_is_read(tmp_path: Path) -> None:
    task = make_task(tmp_path, "delta-3", evidence="quiet\n")
    (task / "tests" / "check.py").write_text("expected: dict = {'ip': '203.0.113.7'}\n")

    assert expected_answers(task, []) == {"203.0.113.7"}


def test_a_non_string_entry_does_not_switch_the_fixture_off(tmp_path: Path) -> None:
    """All-or-nothing extraction turned one integer field into a silent skip."""
    task = make_task(tmp_path, "delta-4", evidence="quiet\n")
    (task / "tests" / "check.py").write_text(
        "exp = {'ip': '203.0.113.7', 'attempts': 41}\n"
    )

    assert expected_answers(task, []) == {"203.0.113.7"}


def test_a_longer_token_does_not_satisfy_an_answer() -> None:
    assert not reachable("198.51.100.44", "denied 198.51.100.440 at the edge")
    assert not reachable("deploy", "run the deployment on the operations host")
    # A dot, dash, slash or colon continues the token only when a word character
    # follows it, so a value at the end of a sentence still counts.
    assert not reachable("deploy", "the deploy-key was rotated")
    assert not reachable("admin", "host admin.example.com answered")
    assert not reachable("100.44", "from 198.51.100.44 port 22")
    assert reachable("198.51.100.44", "from 198.51.100.44 port 51003")
    assert reachable("deploy", "accepted password for deploy.")
    assert reachable("/uploads/shell.php", '"path":"/uploads/shell.php",')


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


def test_an_unknown_option_stops_the_run(tmp_path: Path, capsys) -> None:
    """Silently dropping a flag lets a run pretend it understood what it was asked."""
    assert main(["check_fixture_answers.py", "--strict", str(tmp_path)]) == 2
    assert "unknown option" in capsys.readouterr().out


def test_a_checker_the_guard_cannot_parse_is_not_a_pass(tmp_path: Path, capsys) -> None:
    """A guard that could not look must not report clean."""
    task = make_task(tmp_path, "mu", evidence="quiet\n", expected="ip=203.0.113.7\n")
    (task / "environment" / "case" / "trail.log").write_text("203.0.113.7\n")
    (task / "tests" / "check.py").write_text("exp = {'ip': '203.0.113.7'\n")

    code = main(["check_fixture_answers.py", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1, out
    assert "BLIND: unparsable checker" in out


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Git with the developer's own configuration kept out of the test.

    A global `core.excludesFile`, `commit.gpgsign` or `init.templateDir` would otherwise
    decide the result of these tests on one machine and not on another.
    """
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
        "PATH": os.environ.get("PATH", ""),
    }
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def test_a_fixture_file_git_does_not_track_is_reported(tmp_path: Path, capsys) -> None:
    if not shutil.which("git"):
        pytest.skip("git is not installed; the tracking check cannot be exercised")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    make_task(repo, "lambda", evidence="203.0.113.7\n", expected="ip=203.0.113.7\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture")
    (repo / "lambda" / "environment" / "case" / "late.log").write_text("203.0.113.8\n")

    assert main(["check_fixture_answers.py", str(repo)]) == 1
    assert "UNTRACKED" in capsys.readouterr().out


def test_a_tasks_root_git_tracks_nothing_of_is_reported(tmp_path: Path, capsys) -> None:
    """The incident in full: nothing here would survive a clone."""
    if not shutil.which("git"):
        pytest.skip("git is not installed; the tracking check cannot be exercised")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    make_task(repo, "nu", evidence="203.0.113.7\n", expected="ip=203.0.113.7\n")

    assert main(["check_fixture_answers.py", str(repo)]) == 1
    assert "UNTRACKED" in capsys.readouterr().out


def is_ignored(repo: Path, path: str) -> bool:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
        "PATH": os.environ.get("PATH", ""),
    }
    return subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=repo, env=env
    ).returncode == 0


def test_the_project_ignore_rules_keep_fixture_evidence_and_drop_secrets(tmp_path: Path) -> None:
    """The rule that caused the incident, pinned from the outside.

    `*.log`, `logs/`, `dist/` and friends are legitimate rules for build output and an
    ordinary trap for a task fixture, where the same names are evidence. A credentials
    file must stay ignored anyway: the blanket re-inclusion lifts every earlier rule,
    including the one at the top of the file.
    """
    if not shutil.which("git"):
        pytest.skip("git is not installed; ignore rules cannot be exercised")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    shutil.copy(Path(__file__).resolve().parent.parent / ".gitignore", repo / ".gitignore")

    for kept in (
        "local_task/t/environment/case/logs/auth.log",
        "local_task/t/environment/app/dist/bundle.js",
        "local_task_extra/tasks/t/environment/evidence/edge.jsonl",
        "local_task_extra/tasks/t/solution/trace.log",
        "local_task_extra/tasks/t/tests/data/expected.log",
        "local_task_extra/tasks/t/environment/app/.env.example",
    ):
        assert not is_ignored(repo, kept), kept

    for dropped in (
        "local_task_extra/tasks/t/environment/.env",
        "local_task/t/__pycache__/check.cpython-312.pyc",
        "local_task_extra/tasks/t/environment/.pytest_cache/CACHEDIR.TAG",
        "local_task_extra/tasks/t/environment/.venv/pyvenv.cfg",
        "local_task_extra/tasks/t/environment/.DS_Store",
        # Run artefacts of the reference suite live beside the packages, not inside
        # them, and must stay ignored: the negation is scoped to the four package
        # directories exactly so that these do not start polluting `git status`.
        "local_task_extra/validation/runtime-logs/solution-t.log",
        "local_task_extra/tools/audit.log",
        "build/submission.zip",
    ):
        assert is_ignored(repo, dropped), dropped


def test_the_repository_fixtures_are_clean(capsys) -> None:
    """The regression itself: an evidence file lost from a real task must fail here.

    The counts are exact, not floors. A floor holds while the number it names is the
    current one, so any defect that switches a fixture from "verified" to "states no
    verbatim answer" — an unparsable checker, a renamed answer dict, a value below the
    length threshold — would pass under `>=` the moment a new fixture is added. The
    price is one edit here per new task package, and that edit is the point: adding a
    fixture should require saying whether it is checkable.
    """
    code = main(["check_fixture_answers.py"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "checked 24 fixtures: 5 state a verbatim answer" in out, out
