"""Production-source progress is separate from report and artifact progress."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.result_progress import (
    MAX_FILE_BYTES,
    SourceSnapshot,
    changed_sources,
    snapshot_sources,
)


def test_a_readme_change_is_not_reported_as_a_source_change(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("before\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "README.md").write_text("after\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ()


def test_source_and_config_changes_are_reported_by_relative_path(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("mode: safe\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "app.py").write_text("answer = 2\n", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("mode: strict\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ("app.py", "settings.yaml")


def test_a_new_source_file_is_a_change_and_same_bytes_are_not(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "new_module.py").write_text("answer = 2\n", encoding="utf-8")
    first = changed_sources(tmp_path, baseline)
    (tmp_path / "new_module.py").write_text("answer = 2\n", encoding="utf-8")
    second = changed_sources(tmp_path, snapshot_sources(tmp_path))

    assert first.paths == ("new_module.py",)
    assert second.known is True
    assert second.paths == ()


def test_rolling_a_source_back_restores_a_clean_comparison(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    target.write_text("answer = 2\n", encoding="utf-8")
    assert changed_sources(tmp_path, baseline).paths == ("app.py",)
    target.write_text("answer = 1\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ()


def test_tests_fixtures_dependencies_and_reports_do_not_count(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("assert False\n", encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "result.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("bad = true\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "reports" / "result.json").write_text('{"ok": true}\n', encoding="utf-8")
    (tmp_path / "node_modules" / "package.js").write_text("bad = false\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ()


def test_an_oversized_source_makes_the_result_unknown_not_clean(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    baseline = snapshot_sources(tmp_path)
    changes = changed_sources(tmp_path, baseline)

    assert baseline.known is False
    assert changes.known is False
    assert "exceeds" in changes.reason


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_an_unreadable_source_makes_the_result_unknown_not_absent(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    target.chmod(0)
    try:
        snapshot = snapshot_sources(tmp_path)
    finally:
        target.chmod(0o644)

    assert snapshot.known is False
    assert "could not be read" in snapshot.reason


def test_an_incomplete_baseline_never_becomes_a_clean_result() -> None:
    baseline = SourceSnapshot(files={}, known=False, reason="read failed")

    changes = changed_sources(Path("/does/not/matter"), baseline)

    assert changes.known is False
    assert changes.paths == ()
    assert changes.reason == "read failed"


def test_a_deleted_source_file_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)
    target.unlink()

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ("app.py",)
