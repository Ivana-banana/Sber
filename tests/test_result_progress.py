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


# --- F6: a fix is not always a .py file -------------------------------------


def test_a_fix_in_a_template_counts_as_a_production_source_change(tmp_path: Path) -> None:
    # The reviewer's probe: an XSS fix lands in templates/index.html, `changed_sources`
    # comes back empty and the code-fix gate refuses every finish until the cap.
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("{{ name }}\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "templates" / "index.html").write_text("{{ name | e }}\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ("templates/index.html",)


def test_template_and_config_kinds_are_all_seen(tmp_path: Path) -> None:
    written = {
        "page.j2": "{{ x }}\n",
        "layout.jinja": "{{ y }}\n",
        ".env": "DEBUG=1\n",
        "seed.txt": "one\n",
    }
    for name, text in written.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    for name in written:
        (tmp_path / name).write_text("changed\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == (".env", "layout.jinja", "page.j2", "seed.txt")


def test_an_extensionless_script_is_source_when_it_has_a_shebang(tmp_path: Path) -> None:
    (tmp_path / "backup").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("All rights reserved.\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "backup").write_text("#!/bin/sh\necho new\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("Now MIT.\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ("backup",), "a shebang makes it a script; prose stays prose"


def test_a_production_file_whose_name_merely_contains_test_is_not_a_test(tmp_path: Path) -> None:
    # "latest_events.py" contains the substring "test_", which used to disqualify it.
    (tmp_path / "latest_events.py").write_text("events = []\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "latest_events.py").write_text("events = [1]\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == ("latest_events.py",)


def test_a_real_test_file_beside_the_code_still_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("assert False\n", encoding="utf-8")
    (tmp_path / "app.spec.js").write_text("it('fails')\n", encoding="utf-8")
    baseline = snapshot_sources(tmp_path)

    (tmp_path / "test_app.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "app.spec.js").write_text("it('passes')\n", encoding="utf-8")

    changes = changed_sources(tmp_path, baseline)

    assert changes.known is True
    assert changes.paths == (), "changing the checks is not fixing the code"
