"""What a tool is allowed to hand back to a small model.

Two failure modes drive every test here. A command that never returns eats the whole
budget and the task scores zero; and output that arrives in full fills the context
window, at which point the session ends mid-task with nothing produced. So the
contract under test is narrow: every call terminates, every observation is bounded
and marked when cut, and no failure escapes as an exception.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from agent import limits
from agent.tools import (
    clip,
    file_info,
    list_dir,
    read_range,
    run_argv,
    run_shell,
    search,
    write_file,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="target image is Linux; /bin/sh absent on Windows")


def python_argv(source: str) -> list[str]:
    """Run a snippet with the interpreter running the tests, on any platform."""
    return [sys.executable, "-c", source]


# --- clipping ---------------------------------------------------------------


def test_short_text_survives_untouched() -> None:
    text, truncated = clip("hello")

    assert text == "hello"
    assert truncated is False


def test_long_text_keeps_both_ends_and_says_it_was_cut() -> None:
    body = "S" + "x" * (limits.TOOL_OUTPUT_HEAD_BYTES + limits.TOOL_OUTPUT_TAIL_BYTES + 1000) + "E"

    text, truncated = clip(body)

    assert truncated is True
    assert text.startswith("S"), "the command and its arguments live at the start"
    assert text.endswith("E"), "the error usually lives at the end"
    assert "truncated" in text
    assert len(text) < len(body)


# --- running a command ------------------------------------------------------


def test_a_successful_command_reports_its_output_and_zero(tmp_path: Path) -> None:
    result = run_argv(python_argv("print('done')"), cwd=tmp_path, timeout_s=30)

    assert result.ok is True
    assert result.exit_code == 0
    assert "done" in result.output


def test_a_failing_command_is_a_result_and_not_an_exception(tmp_path: Path) -> None:
    result = run_argv(
        python_argv("import sys; sys.stderr.write('boom'); sys.exit(3)"), cwd=tmp_path, timeout_s=30
    )

    assert result.ok is False
    assert result.exit_code == 3
    assert "boom" in result.output, "stderr is where the failure explains itself"


def test_a_missing_executable_is_a_result_and_not_an_exception(tmp_path: Path) -> None:
    result = run_argv(["definitely-not-a-real-binary-xyz"], cwd=tmp_path, timeout_s=5)

    assert result.ok is False
    assert result.exit_code is None
    assert result.detail


def test_a_hanging_command_is_killed_and_does_not_cost_its_full_sleep(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_argv(python_argv("import time; time.sleep(30)"), cwd=tmp_path, timeout_s=1.0)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "timeout" in result.detail
    assert elapsed < 10, "the loop must get its step back, not wait out the child"


def test_a_flood_of_output_is_bounded_before_it_reaches_the_model(tmp_path: Path) -> None:
    flood = "import sys\nfor i in range(200000): sys.stdout.write('%d line of noise\\n' % i)"

    result = run_argv(python_argv(flood), cwd=tmp_path, timeout_s=60)

    assert result.ok is True
    assert result.truncated is True
    assert len(result.output) < limits.TOOL_OUTPUT_HEAD_BYTES + limits.TOOL_OUTPUT_TAIL_BYTES + 200
    assert "0 line of noise" in result.output, "the head survives"
    assert "199999 line of noise" in result.output, "so does the tail, where a failure would be"


def test_a_secret_never_reaches_the_output(tmp_path: Path) -> None:
    secret = "sk-not-a-real-key-0123456789"
    result = run_argv(python_argv(f"print('token={secret}')"), cwd=tmp_path, timeout_s=30, secrets=(secret,))

    assert secret not in result.output
    assert "redacted" in result.output


@POSIX_ONLY
def test_the_shell_form_runs_a_pipeline(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    result = run_shell("cat a.txt | grep beta", cwd=tmp_path, timeout_s=30)

    assert result.ok is True
    assert "beta" in result.output
    assert "alpha" not in result.output


@POSIX_ONLY
def test_a_killed_command_leaves_no_grandchild_behind(tmp_path: Path) -> None:
    marker = tmp_path / "orphan.txt"
    # The shell exits immediately; the background child outlives it unless the whole
    # process group is killed. If it survives, it writes the marker.
    result = run_shell(f"(sleep 1.5; touch {marker}) & sleep 30", cwd=tmp_path, timeout_s=0.5)

    assert result.ok is False
    time.sleep(2.5)
    assert not marker.exists(), "the process group outlived its timeout"


# --- looking at files -------------------------------------------------------


def test_file_info_reports_size_and_text_lines(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"one\ntwo\nthree\n")

    result = file_info(target)

    assert result.ok is True
    assert "14 bytes" in result.output
    assert "3 lines" in result.output


def test_file_info_calls_a_binary_a_binary(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x7fELF\x00\x01\x02\x00binary\x00content")

    result = file_info(target)

    assert result.ok is True
    assert "binary" in result.output


def test_file_info_on_a_missing_path_fails_without_raising(tmp_path: Path) -> None:
    result = file_info(tmp_path / "nope.txt")

    assert result.ok is False
    assert result.detail


def test_read_range_reaches_a_late_line_of_a_large_file(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    with target.open("w", encoding="utf-8") as handle:
        for i in range(50_000):
            handle.write('{"i": %d, "msg": "noise"}\n' % i)
        handle.write('{"i": 50000, "msg": "SENTINEL"}\n')

    result = read_range(target, start_line=50_001, max_lines=2)

    assert result.ok is True
    assert "SENTINEL" in result.output
    assert "noise" not in result.output, "only the requested window is read"


def test_read_range_bounds_a_single_enormous_line(tmp_path: Path) -> None:
    target = tmp_path / "oneline.txt"
    target.write_text("y" * (limits.READ_MAX_BYTES * 4), encoding="utf-8")

    result = read_range(target, start_line=1, max_lines=1)

    assert result.ok is True
    assert result.truncated is True
    assert len(result.output) < limits.READ_MAX_BYTES + 500


def test_reading_the_environment_out_of_a_file_does_not_leak_the_key(tmp_path: Path) -> None:
    # `/proc/self/environ` is a file, so the shell is not the only way a key reaches
    # the context. Whatever redaction the shell tool gets, the file tools get too.
    secret = "sk-not-a-real-key-0123456789"
    target = tmp_path / "environ"
    target.write_text(f"OPENAI_API_KEY={secret}\nPATH=/usr/bin\n", encoding="utf-8")

    window = read_range(target, secrets=(secret,))
    found = search("OPENAI_API_KEY", tmp_path, secrets=(secret,))

    assert secret not in window.output
    assert secret not in found.output


def test_read_range_past_the_end_says_so_instead_of_returning_nothing(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("only line\n", encoding="utf-8")

    result = read_range(target, start_line=900, max_lines=5)

    assert result.ok is False
    assert result.detail


# --- listing and searching --------------------------------------------------


def test_list_dir_marks_directories_and_sizes(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("abc", encoding="utf-8")

    result = list_dir(tmp_path)

    assert result.ok is True
    assert "sub/" in result.output
    assert "file.txt" in result.output


def test_list_dir_stops_at_the_entry_cap(tmp_path: Path) -> None:
    for i in range(limits.LIST_MAX_ENTRIES + 40):
        (tmp_path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")

    result = list_dir(tmp_path)

    assert result.truncated is True
    assert "more" in result.output


def test_search_finds_a_late_match_in_a_large_file(tmp_path: Path) -> None:
    target = tmp_path / "audit.log"
    with target.open("w", encoding="utf-8") as handle:
        for i in range(80_000):
            handle.write(f"{i} routine event\n")
        handle.write("80000 CANARY authentication bypass\n")

    result = search("CANARY", tmp_path)

    assert result.ok is True
    assert "audit.log:80001" in result.output
    assert "authentication bypass" in result.output


def test_search_stops_at_the_match_cap(tmp_path: Path) -> None:
    target = tmp_path / "many.txt"
    target.write_text("hit\n" * (limits.SEARCH_MAX_MATCHES + 50), encoding="utf-8")

    result = search("hit", tmp_path)

    assert result.truncated is True
    assert result.output.count("hit") <= limits.SEARCH_MAX_MATCHES + 5


def test_search_does_not_dump_a_binary_file(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01needle\x00\x02" * 10)
    (tmp_path / "text.txt").write_text("needle here\n", encoding="utf-8")

    result = search("needle", tmp_path)

    assert "text.txt:1" in result.output
    assert "blob.bin" not in result.output


def test_search_without_a_match_is_a_clean_empty_answer(tmp_path: Path) -> None:
    (tmp_path / "text.txt").write_text("nothing of interest\n", encoding="utf-8")

    result = search("absent-pattern", tmp_path)

    assert result.ok is False
    assert "no match" in result.detail


def test_a_broken_pattern_fails_without_raising(tmp_path: Path) -> None:
    result = search("([unclosed", tmp_path)

    assert result.ok is False
    assert result.detail


def test_write_file_puts_exactly_the_bytes_it_was_given(tmp_path: Path) -> None:
    result = write_file(tmp_path / "bye.txt", "Bye", workspace=tmp_path)

    assert result.ok is True
    assert (tmp_path / "bye.txt").read_bytes() == b"Bye", (
        "verifiers compare exact content; a helpful trailing newline is a zero"
    )


def test_write_file_creates_missing_parents(tmp_path: Path) -> None:
    result = write_file(tmp_path / "deep" / "nested" / "report.json", "{}", workspace=tmp_path)

    assert result.ok is True
    assert (tmp_path / "deep" / "nested" / "report.json").read_text(encoding="utf-8") == "{}"


def test_write_file_replaces_content_rather_than_appending(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    write_file(target, "first", workspace=tmp_path)
    write_file(target, "second", workspace=tmp_path)

    assert target.read_text(encoding="utf-8") == "second"


def test_write_file_refuses_to_leave_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"

    result = write_file(workspace / ".." / "outside.txt", "x", workspace=workspace)

    assert result.ok is False
    assert "outside the workspace" in result.detail
    assert not outside.exists(), "a refused write must not have happened first"


def test_write_file_accepts_a_path_the_task_named(tmp_path: Path) -> None:
    """E009's zero, at the tool boundary.

    The workspace guess was the agent's own directory, the task named its deliverable
    by absolute path, and confining the write to the workspace put a correct answer in
    the wrong place -- where the run then verified it and reported success.
    """
    workspace = tmp_path / "opt" / "harbor" / "local-agent"
    workspace.mkdir(parents=True)
    target = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    target.parent.mkdir(parents=True)

    result = write_file(target, "Ciao", workspace=workspace, permitted=(target,))

    assert result.ok is True
    assert target.read_bytes() == b"Ciao"


def test_write_file_still_refuses_a_path_nobody_named(tmp_path: Path) -> None:
    # Widening the policy to the task text must not widen it to everything: file
    # contents and command output are not the task, and cannot move a write.
    workspace = tmp_path / "app"
    workspace.mkdir()
    named = tmp_path / "srv" / "report.json"
    named.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.txt"

    result = write_file(elsewhere, "x", workspace=workspace, permitted=(named,))

    assert result.ok is False
    assert not elsewhere.exists()


def test_write_file_refuses_a_directory_without_raising(tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()

    result = write_file(tmp_path / "adir", "x", workspace=tmp_path)

    assert result.ok is False
    assert result.detail
