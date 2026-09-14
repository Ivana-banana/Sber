"""What a tool is allowed to hand back to a small model.

Two failure modes drive every test here. A command that never returns eats the whole
budget and the task scores zero; and output that arrives in full fills the context
window, at which point the session ends mid-task with nothing produced. So the
contract under test is narrow: every call terminates, every observation is bounded
and marked when cut, and no failure escapes as an exception.
"""

from __future__ import annotations

import json
import os
import stat
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
    replace_text,
    run_argv,
    run_shell,
    search,
    write_file,
    write_json,
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
def test_the_shell_does_not_inherit_the_endpoint_credentials(tmp_path: Path, monkeypatch) -> None:
    # A shell the model runs can `env`, `env > file` or `echo $OPENAI_API_KEY | base64`.
    # Value redaction catches the plain dump but not the base64 pipe, and `env > file`
    # writes the key to disk unredacted. Removing the three endpoint variables from the
    # child's environment closes all of these at once, while the task's own environment
    # and PATH are preserved. secrets=() here so the test proves scrubbing, not redaction.
    monkeypatch.setenv(limits.API_KEY_ENV_NAME, "sk-canary-0123456789")
    monkeypatch.setenv(limits.BASE_URL_ENV_NAME, "http://10.0.0.5:8000/v1")
    monkeypatch.setenv(limits.MODEL_ENV_NAME, "canary-model-name")
    monkeypatch.setenv("POSTGRES_PASSWORD", "task-owned-secret")

    dumped = run_shell("env", cwd=tmp_path, timeout_s=30, secrets=())
    piped = run_shell("echo $OPENAI_API_KEY | tr -d '\\n' | wc -c", cwd=tmp_path, timeout_s=30, secrets=())

    assert "sk-canary-0123456789" not in dumped.output
    assert "http://10.0.0.5:8000/v1" not in dumped.output
    assert "canary-model-name" not in dumped.output
    assert "0" == piped.output.strip(), "the key expands to nothing in the child shell"
    assert "task-owned-secret" in dumped.output, "the task's own environment survives"


@POSIX_ONLY
def test_a_killed_command_leaves_no_grandchild_behind(tmp_path: Path) -> None:
    marker = tmp_path / "orphan.txt"
    # The shell exits immediately; the background child outlives it unless the whole
    # process group is killed. If it survives, it writes the marker.
    result = run_shell(f"(sleep 1.5; touch {marker}) & sleep 30", cwd=tmp_path, timeout_s=0.5)

    assert result.ok is False
    time.sleep(2.5)
    assert not marker.exists(), "the process group outlived its timeout"


@POSIX_ONLY
def test_a_backgrounded_child_does_not_hold_the_call_past_its_timeout(tmp_path: Path) -> None:
    # `cmd &` makes the shell exit at once while the job keeps the stdout pipe open.
    # The reader threads -- and the pipe close that contends for their buffer lock --
    # then block on that job until it ends, freezing the call well past its timeout
    # unless the lingering session is reaped when the readers do not finish in time.
    marker = tmp_path / "late.txt"
    started = time.monotonic()
    result = run_shell(f"(sleep 8; touch {marker}) &", cwd=tmp_path, timeout_s=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5, "a leaked background child must not freeze the call"
    assert result.ok is False, "a reaped background job is not a success"
    time.sleep(9.0)
    assert not marker.exists(), "the backgrounded child was reaped, not left running"


# --- looking at files -------------------------------------------------------


def test_file_info_reports_size_and_text_lines(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"one\ntwo\nthree\n")

    result = file_info(target)

    assert result.ok is True
    assert "14 bytes" in result.output
    assert "3 lines" in result.output


@POSIX_ONLY
def test_a_fifo_is_described_not_opened_and_never_hangs(tmp_path: Path) -> None:
    # open() on a FIFO blocks until a writer appears; a CTF/forensic tree can hold one,
    # and read_range/file_info/search opened it with no S_ISREG guard, freezing the
    # agent until the runner's kill. These must return quickly and never open it.
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    info = file_info(fifo)
    read = read_range(fifo)

    assert info.ok is True and "not a regular file" in info.output
    assert read.ok is False and "not a regular file" in read.detail


@POSIX_ONLY
def test_search_skips_a_fifo_in_the_tree_and_still_finds_the_real_match(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "real.txt").write_text("the flag is here\n", encoding="utf-8")

    result = search("flag", tmp_path)

    assert result.ok is True
    assert "real.txt" in result.output


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


def test_read_range_reports_source_and_actual_truncated_window_after_raw_body(tmp_path: Path) -> None:
    target = tmp_path / "same.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_range(target, start_line=2, max_lines=1)

    assert result.ok is True
    assert result.output.startswith("two\n"), "metadata must not prefix copyable anchor text"
    assert f"source={target}" in result.output
    assert "start_line=2" in result.output
    assert "end_line=2" in result.output
    assert "eof=no" in result.output
    assert "truncated=yes" in result.output


def test_read_range_marks_eof_when_the_requested_window_reaches_file_end(tmp_path: Path) -> None:
    target = tmp_path / "tail.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_range(target, start_line=2, max_lines=5)

    assert result.ok is True
    assert "start_line=2" in result.output
    assert "end_line=3" in result.output
    assert "eof=yes" in result.output
    assert "truncated=no" in result.output


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


def test_list_dir_reports_the_full_source_path_for_same_named_directories(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_text("left", encoding="utf-8")
    (right / "same.txt").write_text("right", encoding="utf-8")

    left_result = list_dir(left)
    right_result = list_dir(right)

    assert f"source={left}" in left_result.output
    assert f"source={right}" in right_result.output
    assert left_result.output != right_result.output


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


def test_write_file_warns_when_a_python_source_no_longer_compiles(tmp_path: Path) -> None:
    # The 14B fix-sqli trace overwrote a router with a stub that referenced an undefined
    # name; the app failed to import and the verifier never ran, yet "wrote N bytes"
    # read as success. A .py write now reports a syntax break the way a .json write
    # reports an unparseable file, so "broke the file and did not notice" gets a signal.
    target = tmp_path / "mod.py"
    good = write_file(target, "def f():\n    return 1\n", workspace=tmp_path)
    bad = write_file(target, "def f(:\n    return 1\n", workspace=tmp_path)

    assert good.ok is True and "does not compile" not in good.output
    assert bad.ok is True and "does not compile" in bad.output


def test_write_file_does_not_flag_a_bom_python_file(tmp_path: Path) -> None:
    # Compiling from bytes (not the decoded str) lets the tokenizer accept a BOM the way
    # `import` does; compiling the str would reject a file that imports fine.
    result = write_file(tmp_path / "mod.py", "﻿x = 1\n", workspace=tmp_path)
    assert result.ok is True
    assert "does not compile" not in result.output


def test_replace_text_reports_when_the_edit_breaks_compilation(tmp_path: Path) -> None:
    # replace_text is the tool the model is steered toward; a bad indentation in `new`
    # breaks the module the same way a bad write_file would, so it is reported too.
    target = tmp_path / "mod.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = replace_text(target, "    return 1", "  return 1\n  bad indent", workspace=tmp_path)
    assert result.ok is True
    assert "does not compile" in result.output


def test_write_json_serialises_a_structured_value_into_valid_json(tmp_path: Path) -> None:
    # The failure it exists to prevent: the model's finding quotes the vulnerable code,
    # so its JSON, escaped as a string inside the action's own JSON, breaks. Here the
    # model passes the value structured and the agent serialises, so the quotes survive
    # and the file always parses.
    report = {
        "findings": [
            {
                "title": "SQL injection in login",
                "evidence": "query = f\"SELECT id FROM users WHERE username = '{req.username}'\"",
                "recommendation": 'Use parameterized queries: "SELECT id FROM users WHERE username = $1"',
            }
        ]
    }
    target = tmp_path / "security_report.json"

    result = write_json(target, report, workspace=tmp_path)

    assert result.ok is True
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == report, "the value round-trips through valid JSON, quotes intact"


def test_write_json_parses_a_value_the_model_still_sent_as_a_string(tmp_path: Path) -> None:
    target = tmp_path / "r.json"
    result = write_json(target, '{"findings": [{"title": "x"}]}', workspace=tmp_path)
    assert result.ok is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"findings": [{"title": "x"}]}


def test_write_json_refuses_a_string_that_is_not_json(tmp_path: Path) -> None:
    result = write_json(tmp_path / "r.json", "not json at all", workspace=tmp_path)
    assert result.ok is False
    assert "not valid JSON" in result.detail


def test_write_json_refuses_a_missing_value_rather_than_writing_null(tmp_path: Path) -> None:
    result = write_json(tmp_path / "r.json", None, workspace=tmp_path)
    assert result.ok is False
    assert not (tmp_path / "r.json").exists()


def test_write_json_stays_inside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = write_json(workspace / ".." / "escape.json", {"x": 1}, workspace=workspace)
    assert result.ok is False
    assert "outside the workspace" in result.detail


def test_write_file_refusing_unencodable_content_does_not_truncate_the_file(tmp_path: Path) -> None:
    # A small model that emits a lone surrogate (`\ud83d` from a split emoji escape) hit
    # UnicodeEncodeError -- but only after open("w") had already truncated the file to
    # zero bytes, and only OSError was caught. Encoding before opening means a refusal
    # leaves the previous content intact rather than destroying the deliverable.
    target = tmp_path / "report.json"
    write_file(target, '{"findings": []}', workspace=tmp_path)

    result = write_file(target, "\ud83d broken", workspace=tmp_path)

    assert result.ok is False
    assert "UTF-8" in result.detail
    assert target.read_text(encoding="utf-8") == '{"findings": []}', "the old content survives"


def test_write_file_refuses_to_leave_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"

    result = write_file(workspace / ".." / "outside.txt", "x", workspace=workspace)

    assert result.ok is False
    assert "outside the workspace" in result.detail
    assert not outside.exists(), "a refused write must not have happened first"


def test_write_file_accepts_a_path_the_task_named(tmp_path: Path) -> None:
    """E011's zero, at the tool boundary.

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


def test_search_inside_a_file_the_task_named(tmp_path: Path) -> None:
    # `os.walk` over a file yields nothing, so a pattern that is plainly in the file
    # came back as "no match" -- an inapplicable tool reported as absent evidence.
    # The model has no way to tell that apart from a genuinely clean file.
    target = tmp_path / "handler.py"
    target.write_text("def login(name):\n    return db.execute(query)\n", encoding="utf-8")

    result = search("execute", target)

    assert result.ok is True
    assert "handler.py:2" in result.output


def test_search_says_the_path_is_missing_rather_than_finding_nothing(tmp_path: Path) -> None:
    result = search("anything", tmp_path / "absent")

    assert result.ok is False
    assert "no such path" in result.detail
    assert "no match" not in result.detail


MODULE = """\
from fastapi import APIRouter, HTTPException

from db import get_pool
from models import LoginRequest, LoginResponse

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    query = f"SELECT id FROM users WHERE username = '{req.username}'"
    row = await conn.fetchrow(query)
    return LoginResponse(token=f"token-{row['id']}")
"""
"""A module shaped like the one E-BASE destroyed: the interesting line is at the
bottom, and everything that makes it importable is above the read window."""


def test_replace_text_changes_the_anchor_and_nothing_else(tmp_path: Path) -> None:
    target = tmp_path / "auth.py"
    target.write_text(MODULE, encoding="utf-8")
    old = "    query = f\"SELECT id FROM users WHERE username = '{req.username}'\"\n    row = await conn.fetchrow(query)"
    new = '    query = "SELECT id FROM users WHERE username = $1"\n    row = await conn.fetchrow(query, req.username)'

    result = replace_text(target, old, new, workspace=tmp_path)

    assert result.ok, result.detail
    after = target.read_text(encoding="utf-8")
    assert new in after
    assert old not in after
    # The whole point: the imports and the router the model never read survive.
    assert after.startswith("from fastapi import APIRouter, HTTPException")
    assert "router = APIRouter()" in after
    assert after.endswith("return LoginResponse(token=f\"token-{row['id']}\")\n")


def test_replace_text_preserves_crlf_and_unicode_bytes_outside_anchor(tmp_path: Path) -> None:
    target = tmp_path / "mixed.py"
    before = "prefix\r\nvalue = 1\r\ntrailer = 'трейлер'\r\n".encode("utf-8")
    target.write_bytes(before)

    result = replace_text(target, "value = 1", "value = 2", workspace=tmp_path)

    assert result.ok, result.detail
    assert target.read_bytes() == "prefix\r\nvalue = 2\r\ntrailer = 'трейлер'\r\n".encode("utf-8")


def test_replace_text_refuses_invalid_utf8_without_changing_file(tmp_path: Path) -> None:
    target = tmp_path / "invalid.py"
    before = b"value = 1\ntrailer = '\xff'\n"
    target.write_bytes(before)

    result = replace_text(target, "value = 1", "value = 2", workspace=tmp_path)

    assert not result.ok
    assert "UTF-8" in result.detail
    assert target.read_bytes() == before


def test_replace_text_refuses_an_empty_anchor(tmp_path: Path) -> None:
    target = tmp_path / "empty.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = replace_text(target, "", "value = 2", workspace=tmp_path)

    assert not result.ok
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_replace_text_refuses_an_unchanged_replacement(tmp_path: Path) -> None:
    target = tmp_path / "same.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = replace_text(target, "value = 1", "value = 1", workspace=tmp_path)

    assert not result.ok
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_replace_text_refuses_an_oversized_file_without_reading_it_all(tmp_path: Path) -> None:
    target = tmp_path / "large.py"
    before = b"x" * (1024 * 1024 + 1) + b"\nneedle\n"
    target.write_bytes(before)

    result = replace_text(target, "needle", "changed", workspace=tmp_path)

    assert not result.ok
    assert "large" in result.detail or "size" in result.detail
    assert target.read_bytes() == before


def test_replace_text_keeps_original_when_atomic_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "atomic.py"
    before = "value = 1\n"
    target.write_text(before, encoding="utf-8")

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("agent.tools.os.replace", fail_replace)
    result = replace_text(target, "value = 1", "value = 2", workspace=tmp_path)

    assert not result.ok
    assert "disk full" in result.detail
    assert target.read_text(encoding="utf-8") == before


@POSIX_ONLY
def test_replace_text_preserves_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "mode.py"
    target.write_text("value = 1\n", encoding="utf-8")
    os.chmod(target, 0o640)

    result = replace_text(target, "value = 1", "value = 2", workspace=tmp_path)

    assert result.ok, result.detail
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@POSIX_ONLY
def test_replace_text_refuses_a_symlink_to_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    link = workspace / "link.py"
    link.symlink_to(outside)

    result = replace_text(link, "value = 1", "value = 2", workspace=workspace)

    assert not result.ok
    assert outside.read_text(encoding="utf-8") == "value = 1\n"


def test_replace_text_refuses_an_anchor_that_is_not_there(tmp_path: Path) -> None:
    target = tmp_path / "auth.py"
    target.write_text(MODULE, encoding="utf-8")

    result = replace_text(target, "no such line", "x", workspace=tmp_path)

    assert not result.ok
    assert target.read_text(encoding="utf-8") == MODULE


def test_replace_text_refuses_an_anchor_that_matches_more_than_once(tmp_path: Path) -> None:
    target = tmp_path / "twice.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    result = replace_text(target, "value = 1", "value = 2", workspace=tmp_path)

    assert not result.ok
    assert "2" in result.detail, result.detail
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_replace_text_refuses_a_missing_file(tmp_path: Path) -> None:
    result = replace_text(tmp_path / "absent.py", "a", "b", workspace=tmp_path)

    assert not result.ok


def test_replace_text_refuses_to_leave_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = replace_text(workspace / ".." / "outside.txt", "secret", "x", workspace=workspace)

    assert not result.ok
    assert outside.read_text(encoding="utf-8") == "secret"


def test_write_file_reports_that_a_json_deliverable_does_not_parse(tmp_path: Path) -> None:
    # E-BASE-r1 find-sqli-login: the model wrote a correct finding whose `evidence`
    # string held an unescaped quote, `write_file` answered "wrote N bytes", and the
    # model then replaced its own analysis with `{"findings": []}` twenty times.
    broken = '{"findings": [{"evidence": "query = f"SELECT 1""}]}'
    result = write_file(tmp_path / "security_report.json", broken, workspace=tmp_path)

    assert result.ok is True, "the deliverable still has to exist before the deadline"
    assert (tmp_path / "security_report.json").read_text(encoding="utf-8") == broken
    assert "not valid JSON" in result.output
    assert "delimiter" in result.output, "say what is wrong, not merely that something is"


def test_write_file_stays_quiet_about_json_that_parses(tmp_path: Path) -> None:
    result = write_file(tmp_path / "report.json", '{"findings": []}', workspace=tmp_path)

    assert result.ok is True
    assert "JSON" not in result.output


def test_write_file_does_not_parse_files_that_are_not_json(tmp_path: Path) -> None:
    result = write_file(tmp_path / "incident_report.txt", "attacker_ip=203.0.113.50", workspace=tmp_path)

    assert result.ok is True
    assert "JSON" not in result.output
