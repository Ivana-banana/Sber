"""The frozen transcript must behave like the real boundary, failures included."""

from __future__ import annotations

import json

from agent.ports import LlmError, LlmPort, LlmReply
from tests.stub_llm import StubLlm


def test_stub_satisfies_the_port() -> None:
    assert isinstance(StubLlm(), LlmPort)


def test_replies_come_back_in_order_and_calls_are_recorded() -> None:
    stub = StubLlm(replies=[LlmReply(text="first"), LlmReply(text="second")])

    assert stub.complete([{"role": "user", "content": "a"}], max_output_tokens=16).text == "first"
    assert stub.complete([{"role": "user", "content": "b"}], max_output_tokens=16).text == "second"

    assert [call[0]["content"] for call in stub.calls] == ["a", "b"]


def test_exhausted_transcript_is_an_error_not_a_repeat() -> None:
    stub = StubLlm(replies=[LlmReply(text="only")])
    stub.complete([], max_output_tokens=16)

    result = stub.complete([], max_output_tokens=16)
    assert isinstance(result, LlmError)
    assert not result.retryable


def test_transcript_roundtrip_including_a_recorded_failure(tmp_path) -> None:
    path = tmp_path / "transcript.json"
    path.write_text(
        json.dumps(
            [
                {"text": "ok", "input_tokens": 11, "output_tokens": 3},
                {"error": "invalid json in tool call", "retryable": True},
                {"text": "no usage block", "usage_reported": False},
            ]
        ),
        encoding="utf-8",
    )

    stub = StubLlm.from_transcript(path)

    first = stub.complete([], max_output_tokens=16)
    assert isinstance(first, LlmReply)
    assert (first.input_tokens, first.output_tokens) == (11, 3)

    second = stub.complete([], max_output_tokens=16)
    assert isinstance(second, LlmError)
    assert second.retryable, "a recorded transport blip must replay as retryable"

    third = stub.complete([], max_output_tokens=16)
    assert isinstance(third, LlmReply)
    assert not third.usage_reported
