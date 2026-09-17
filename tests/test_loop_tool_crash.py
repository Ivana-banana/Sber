"""Действие, которое бросило исключение, — это наблюдение, а не конец эпизода.

`agent.loop._execute` документирован как «никогда не бросает», и пакетный путь
(`_batch_reads`) всё равно страхует его `try/except`, потому что потерять задачу
из-за дефекта в бонусном действии — худшая сделка в файле. Основной путь этой
страховки не имел, а довериться документации тут нельзя: путь приходит от модели,
`Path.resolve()` на NUL-байте бросает `ValueError`, а на одиночном суррогате —
`UnicodeEncodeError`, и ни то ни другое не `OSError`, который ловят `agent.tools`
и сам `loop`. Эпизод падал с уже записанным файлом на диске и без `finish`.

Здесь описано, что петля обязана делать вместо падения: превратить исключение в
`FAIL`-наблюдение с одним только именем класса (сообщение может цитировать
содержимое файла) и идти дальше. И две границы, которые она переходить не должна:
`KeyboardInterrupt`/`SystemExit` — не ответ модели, а `MemoryError` — состояние
процесса, в котором следующее действие всё равно не выполнится.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import loop
from agent.ports import LlmReply
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_state, run

NUL = chr(0)
LONE_SURROGATE = chr(0xD800)


def episode(tmp_path: Path, bad_path: str) -> tuple[StubLlm, Path]:
    """One hostile path, then the same work done properly — a realistic recovery."""
    target = tmp_path / "report.txt"
    return (
        StubLlm(
            replies=[
                action("write_file", path=bad_path, content="y"),
                action("write_file", path=str(target), content="a=b"),
                action("finish", path=str(target), verify="test -s report.txt", summary="готово"),
            ]
        ),
        target,
    )


# --- the exception the model can cause ----------------------------------------


@pytest.mark.parametrize(
    ("name", "bad_path", "expected"),
    [
        ("nul", "/tmp/a" + NUL + "b", "ValueError"),
        ("surrogate", "/tmp/" + LONE_SURROGATE + "x", "UnicodeEncodeError"),
    ],
)
def test_a_path_the_filesystem_cannot_take_is_an_observation(
    tmp_path: Path, name: str, bad_path: str, expected: str
) -> None:
    llm, target = episode(tmp_path, bad_path)

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified", f"{name} ended the episode"
    assert outcome.solved is True
    said = "\n".join(outcome.observations)
    assert "write_file -> FAIL" in said
    assert f"action failed: {expected}" in said
    assert target.read_text(encoding="utf-8") == "a=b"


def test_the_failed_action_relays_the_class_and_never_the_message(tmp_path: Path) -> None:
    """The message of a tool exception can quote a path or file content. Only the type."""
    secret = "s3cret-" + NUL + "value"
    llm = StubLlm(replies=[action("write_file", path=secret, content="y")])
    state = make_state(tmp_path)
    state.budget.max_steps = 1

    outcome = run(state, llm, FakeVerify())

    said = "\n".join(outcome.observations)
    assert "s3cret" not in said
    assert "action failed: ValueError" in said


def test_every_tool_that_takes_a_path_survives_one(tmp_path: Path) -> None:
    """The fuzzed set: five of the eight tools reached `Path.resolve` with it."""
    bad = "/tmp/a" + NUL + "b"
    for act in (
        {"tool": "read_range", "path": bad, "start_line": 1},
        {"tool": "list_dir", "path": bad},
        {"tool": "write_file", "path": bad, "content": "y"},
        {"tool": "write_json", "path": bad, "data": {"a": 1}},
        {"tool": "replace_text", "path": bad, "old": "a", "new": "b"},
    ):
        state = make_state(tmp_path)
        state.budget.max_steps = 1
        llm = StubLlm(replies=[LlmReply(text=json.dumps(act), input_tokens=1, output_tokens=1)])

        outcome = run(state, llm, FakeVerify())

        assert "action failed:" in "\n".join(outcome.observations), act["tool"]


def test_a_finish_path_the_filesystem_will_not_answer_about_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finish gate returns before `_execute`, so the guard above does not cover it.

    On the target 3.12 `Path.exists` hides only ENOENT, ENOTDIR, EBADF and ELOOP and
    re-raises the rest: a name that is too long (ENAMETOOLONG) or a parent directory
    that is not searchable (EACCES) ends the episode inside `_deliverable`. The audit's
    fuzzer reproduces it on /usr/bin/python3.12 for all four playbooks; 3.13 swallows
    every OSError there, so on a newer interpreter the defect is invisible and the
    failing call has to be arranged by hand.
    """
    real_exists = Path.exists

    def refuse(self: Path, **kwargs: object) -> bool:
        if "unanswerable" in str(self):
            raise OSError(36, "File name too long")
        return real_exists(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", refuse)
    target = tmp_path / "report.txt"
    llm = StubLlm(
        replies=[
            action("finish", path=str(tmp_path / "unanswerable"), verify="true", summary="done"),
            action("write_file", path=str(target), content="a=b"),
            action("finish", path=str(target), verify="test -s report.txt", summary="готово"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    assert "finish refused" in outcome.observations[0]


# --- and the exceptions it must not take ---------------------------------------


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, MemoryError])
def test_the_guard_does_not_stand_between_the_run_and_its_own_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """Not answers to the model: an interrupt, a shutdown and an exhausted machine.

    `agent.main` already turns any of them into the parseable `stop=crash` line with
    the deliverable left on disk. Swallowing one here would instead ask the model to
    keep acting inside a process that cannot carry out the next action.
    """

    def explode(*args: object, **kwargs: object) -> None:
        raise failure()

    monkeypatch.setattr(loop, "_execute", explode)
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path))])

    with pytest.raises(failure):
        run(state, llm, FakeVerify())


def test_the_loop_keeps_paying_for_the_failed_step(tmp_path: Path) -> None:
    """A failure is a step spent, and the detectors see it like any other FAIL."""
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[action("write_file", path="/tmp/a" + NUL + "b", content="y")])

    run(state, llm, FakeVerify())

    assert state.budget.steps_used == 1
    assert [record.tool for record in state.actions] == ["write_file"]
    assert state.actions[-1].ok is False


# --- the same class of trust in `_parse` ---------------------------------------


def test_a_degenerately_nested_reply_is_a_parse_error_and_not_a_crash() -> None:
    """`json.loads` recurses per bracket, and enough of them exhaust the stack.

    `_parse` is the second line of defence for the rungs where `agent.llm` cannot
    validate a reply -- a text-mode endpoint, or a stub -- and its docstring promises
    it never raises. `RecursionError` is not `JSONDecodeError`.

    The depth is asserted rather than assumed: how deep the C scanner may go is an
    interpreter build detail (20k is already too deep on 3.12, 3.14 takes more), so
    the test first proves this payload really does exhaust the stack here.
    """
    depth = 100_000
    deep = "[" * depth + "]" * depth
    with pytest.raises(RecursionError):
        json.loads(deep)

    parsed, error = loop._parse(deep)

    assert parsed == {}
    assert "reply was not json" in error
