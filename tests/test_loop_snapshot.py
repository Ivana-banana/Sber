"""best-snapshot at `_finalise`: an emptied deliverable is refilled, nothing else is.

The external grader reads whatever is on disk when the run ends. A run that wrote a
structured report and later blanked the file scores zero for nothing, and that is the
observed `find-sqli-login` shape: a finding that passed every gate, rewritten to empty
twenty times.

The trigger is narrow on purpose, and the last three tests are what keeps it narrow.
`content_health` is a structure signal, not a correctness oracle: a *correct terse*
answer (an audit with no findings, a forensics reply that is one value) scores 1 while
an earlier *wrong verbose* report scores 2. Restoring on any health drop would turn
that pass into a zero, so a non-empty file is always left exactly as the model wrote
it, and only level-2 content is ever remembered.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_state, run


def test_best_snapshot_refills_a_deliverable_the_run_emptied(tmp_path: Path) -> None:
    """A structured report, then blanked to nothing: the blanking is undone.

    The run never reaches a green finish -- it spins until a detector or the step cap
    stops it, so it lands in `_finalise`. An empty file is never the correct final
    state for a task that asks for a report, so the last structured revision goes back.
    """
    target = tmp_path / "security_report.json"
    good = '{"findings": [{"id": 1, "vuln": "sqli", "endpoint": "/login"}]}'
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content=good),
            action("write_file", path=str(target), content=""),
            *[action("list_dir", path=str(tmp_path)) for _ in range(20)],
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.solved is False, "a rescued deliverable is still not a self-declared solve"
    assert outcome.deliverable == target
    assert json.loads(target.read_text(encoding="utf-8"))["findings"], (
        "an emptied deliverable must be refilled from the last structured revision"
    )


def test_best_snapshot_does_not_revive_a_wrong_verbose_over_a_correct_terse(
    tmp_path: Path,
) -> None:
    """An audit that genuinely has no findings is a correct answer that scores 1."""
    target = tmp_path / "security_report.json"
    wrong_verbose = '[{"id": 1, "vuln": "sqli", "endpoint": "/login"}]'
    correct_terse = '{"findings": []}'
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content=wrong_verbose),
            action("write_file", path=str(target), content=correct_terse),
            *[action("list_dir", path=str(tmp_path)) for _ in range(20)],
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.deliverable == target
    assert target.read_text(encoding="utf-8") == correct_terse, (
        "a non-empty answer is the model's call; restore must never override it"
    )


def test_best_snapshot_leaves_a_deliberately_emptied_code_deliverable_alone(
    tmp_path: Path,
) -> None:
    """Emptying a file can be the task, so only structured content is ever remembered."""
    target = tmp_path / "patch.py"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="def f():\n    return 1\n"),
            action("write_file", path=str(target), content=""),
            *[action("list_dir", path=str(tmp_path)) for _ in range(20)],
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.deliverable == target
    assert target.read_text(encoding="utf-8") == "", (
        "a code deliverable is never snapshotted, so emptying it must stand"
    )


def test_best_snapshot_respects_a_same_tier_revision(tmp_path: Path) -> None:
    """Two reports of equal health are the model's judgement, not a regression."""
    target = tmp_path / "incident_report.txt"
    first = "user=alice\nip=10.0.0.1\naction=login\n"
    second = "user=bob\nip=10.0.0.2\naction=login\n"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content=first),
            action("write_file", path=str(target), content=second),
            *[action("list_dir", path=str(tmp_path)) for _ in range(20)],
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.deliverable == target
    assert target.read_text(encoding="utf-8") == second, (
        "freezing the first plausible answer would refuse every later correction"
    )
