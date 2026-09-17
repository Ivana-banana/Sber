"""What the client is allowed to conclude from a reply, and what it must not.

One measurement on each side of the line, both from 15.09 on the RTX 4080 stand:

* `F4-30b-paired` -- on `qwen3:30b-a3b` the first call of every episode came back
  truncated inside its own reasoning (`ctok=2048`, no answer), and the run kept paying
  the default ceiling and the 90 s clamp afterwards. 12/12 fix episodes and 6/6 incident
  episodes lost 185-280 s before the first usable reply.
* `F4-7b-regression-4080` -- on `qwen2.5-coder:7b` a degenerate generation hit the same
  cap with an ordinary, non-empty answer, F4 read it as a reasoning model, and two steps
  then ran to the long call ceiling: +250 s of wall clock for the same zero.

So the signal has to fire on the first reply that is evidence of reasoning and on nothing
else. These tests pin both directions; no network, no sleeping, no key.
"""

from __future__ import annotations

import json
import socket
import urllib.error

from agent import limits, llm
from agent.ports import LlmError, LlmReply
from tests.test_llm import ACTION_LIKE, ENV, make_client, tool_reply


def _reply(
    content: str,
    *,
    reasoning: str = "",
    finish: str = "stop",
    completion: int = 10,
    usage: bool = True,
) -> bytes:
    message: dict[str, object] = {"content": content}
    if reasoning:
        message["reasoning"] = reasoning
    payload: dict[str, object] = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 5, "completion_tokens": completion}
    return json.dumps(payload).encode()


ACTION = '{"tool": "list_dir"}'


# --- what counts as evidence -------------------------------------------------


def test_a_reasoning_field_is_thinking_even_when_the_answer_parsed() -> None:
    client, _, events = make_client([(200, _reply(ACTION, reasoning="let me see"))])

    assert isinstance(client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048), LlmReply)
    assert client._thinking_endpoint is True
    assert any(kind == "thinking" for kind, _ in events)


def test_a_cutoff_that_spent_the_budget_with_nothing_visible_is_thinking() -> None:
    # The shape the 30B stand returned on the first call of every episode: the whole
    # output budget billed, no answer, the tokens spent somewhere we cannot read.
    client, _, _ = make_client([(200, _reply("", finish="length", completion=2048))] * 2)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert client._thinking_endpoint is True


def test_a_cutoff_inside_a_think_tag_is_thinking() -> None:
    cut = _reply("<think>still reasoning when the budget ran", finish="length", completion=2048)
    client, _, _ = make_client([(200, cut)] * 2)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert client._thinking_endpoint is True


def test_a_cutoff_with_no_usage_block_is_not_read_as_thinking() -> None:
    # An endpoint that reports no usage cannot corroborate that the cap was reached.
    # Without corroboration the cheap hypothesis wins (critic, 16.09): the thinking
    # ceiling costs 240 s per call, and a reasoning field or a timeout with no payload
    # will still flip the flag on an endpoint that genuinely reasons.
    cut = _reply("", finish="length", usage=False)
    client, _, _ = make_client([(200, cut)] * 2)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert client._thinking_endpoint is False


def test_a_truncated_tool_call_is_not_read_as_thinking() -> None:
    # A function call with `content: null` is the OpenAI shape of a visible answer; a
    # long write_file body overrunning the cap is looping, not hidden reasoning.
    from agent.llm import _thinking_evidence
    message = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "x", "type": "function",
                               "function": {"name": "emit_action", "arguments": '{"tool":"write_file"'}}]}
    assert _thinking_evidence(message, "length", requested_tokens=2048,
                              completion_tokens=2048, usage_reported=True) == ""


# --- what does not ------------------------------------------------------------


def test_a_length_cutoff_with_a_plain_answer_is_not_a_thinking_endpoint() -> None:
    """The `F4-7b-regression-4080` false positive, in one assertion.

    `finish_reason=length` with an ordinary answer and no reasoning field is a model
    looping, not a model thinking. Treating it as thinking raised the per-call deadline
    on a fast endpoint and cost 250 s for an unchanged zero.
    """
    client, endpoint, events = make_client(
        [(200, _reply("here is a very long ordinary answer", finish="length", completion=2048))] * 2
    )

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert client._thinking_endpoint is False
    assert not any(kind == "thinking" for kind, _ in events)


def test_a_parsed_length_cutoff_leaves_the_next_call_at_the_default_ceiling() -> None:
    # Same false positive from the other side: the cut-off reply happened to parse, so
    # nothing failed -- and the next call must still be an ordinary 2048-token call.
    client, endpoint, _ = make_client(
        [(200, _reply(ACTION, finish="length", completion=2048)), (200, _reply(ACTION))]
    )

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)
    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert [body["max_tokens"] for body in endpoint.bodies] == [2048, 2048]
    assert client._thinking_endpoint is False


def test_an_empty_reply_that_did_not_reach_the_cap_is_not_thinking() -> None:
    # Nothing visible and nothing generated either: an empty reply, not a hidden one.
    client, _, _ = make_client([(200, _reply("", finish="length", completion=3))] * 2)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert client._thinking_endpoint is False


def test_the_raised_output_ceiling_outlives_the_call_that_needed_it() -> None:
    """More room is sticky; the slow-endpoint verdict is not. The two were one flag.

    Separating them is what fixes the false positive without paying for it: a model whose
    answers do not fit keeps its headroom from the first attempt of every later call, so
    neither a genuinely long answer nor a degenerate loop buys a truncated attempt on
    every step -- while nothing about the call deadline moves.
    """
    cut = _reply("a long ordinary answer with no json in it", finish="length", completion=2048)
    client, endpoint, _ = make_client([(200, cut), (200, cut), (200, _reply(ACTION))])

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)
    assert [body["max_tokens"] for body in endpoint.bodies] == [2048, 8192]

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert endpoint.bodies[2]["max_tokens"] == 8192, "the next call starts with the room"
    assert client._thinking_endpoint is False, "and the endpoint is still not called slow"


def test_a_refused_connection_is_not_a_thinking_signal() -> None:
    def refused(*args: object, **kwargs: object) -> tuple[int, bytes]:
        raise ConnectionRefusedError("nothing is listening")

    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(ENV), transport=refused, sleep=lambda _: None
    )
    result = client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert isinstance(result, LlmError)
    assert result.reason.startswith("transport failed:")
    assert client._thinking_endpoint is False


# --- the timeout signal, the only one available on the first call -------------


def test_a_call_that_times_out_with_no_payload_marks_the_endpoint_as_thinking() -> None:
    # Case (a): the very first call of an episode has no reply to inspect, so the timeout
    # itself is the evidence. Learning it here saves the second call from being sent at
    # the default ceiling and truncated mid-reasoning all over again.
    def slow(*args: object, **kwargs: object) -> tuple[int, bytes]:
        raise TimeoutError("the read timed out")

    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(ENV), transport=slow, sleep=lambda _: None
    )
    result = client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert isinstance(result, LlmError)
    assert "timeout" in result.reason.lower()
    assert client._thinking_endpoint is True


def test_a_connect_timeout_wrapped_in_a_urlerror_is_recognised() -> None:
    def slow(*args: object, **kwargs: object) -> tuple[int, bytes]:
        raise urllib.error.URLError(socket.timeout("timed out"))

    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(ENV), transport=slow, sleep=lambda _: None
    )
    result = client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert isinstance(result, LlmError)
    assert "timeout" in result.reason.lower()
    assert client._thinking_endpoint is True


# --- what the flag buys -------------------------------------------------------


def test_a_known_thinking_endpoint_starts_the_next_call_already_raised() -> None:
    # Item 3: once the client knows, the first attempt of the next call carries the raised
    # ceiling. Nothing here raises the *first* call of a run -- that would be a 4x output
    # budget on every task of a fast endpoint, paid to learn what one reply tells us.
    client, endpoint, _ = make_client(
        [(200, _reply(ACTION, reasoning="thought")), (200, _reply(ACTION))]
    )

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)
    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert endpoint.bodies[0]["max_tokens"] == limits.LLM_MAX_OUTPUT_TOKENS
    assert endpoint.bodies[1]["max_tokens"] == limits.LLM_THINKING_MAX_OUTPUT_TOKENS


def test_a_thinking_endpoint_may_spend_the_longer_per_call_timeout() -> None:
    # The client's own hard ceiling, which is what clipped the raised call to 120 s in
    # `F4-30b-paired` even after the loop had lifted its clamp.
    client, endpoint, _ = make_client(
        [(200, _reply(ACTION, reasoning="thought")), (200, _reply(ACTION))]
    )

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=240.0)
    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=240.0)

    assert endpoint.timeouts[0] == limits.LLM_TIMEOUT_S, "the first call does not know yet"
    assert endpoint.timeouts[1] == limits.LLM_THINKING_TIMEOUT_S


def test_a_fast_endpoint_keeps_the_short_per_call_timeout() -> None:
    # The 7B regression guard: nothing about the fast path moves.
    client, endpoint, _ = make_client([(200, _reply(ACTION))] * 2)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=240.0)
    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=240.0)

    assert endpoint.timeouts == [limits.LLM_TIMEOUT_S, limits.LLM_TIMEOUT_S]


def test_the_callers_ceiling_still_wins_when_it_is_smaller() -> None:
    client, endpoint, _ = make_client(
        [(200, _reply(ACTION, reasoning="thought")), (200, _reply(ACTION))]
    )

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=12.0)
    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048, timeout_s=12.0)

    assert endpoint.timeouts == [12.0, 12.0], "a thinking endpoint never extends the caller's budget"


def test_the_thinking_ceiling_is_longer_than_one_measured_thinking_reply() -> None:
    # 6549 completion tokens at ~60 tok/s is 110 s of generation, and the same card shows
    # the request also waiting for a busy slot. A ceiling at LLM_TIMEOUT_S could not hold
    # one such reply, which is how lifting the clamp turned truncation into demotion.
    assert limits.LLM_THINKING_TIMEOUT_S >= 2 * 110.0
    assert limits.LLM_THINKING_TIMEOUT_S > limits.LLM_TIMEOUT_S


def test_nothing_is_sent_to_an_endpoint_that_never_showed_a_thought() -> None:
    # `chat_template_kwargs` is a request to stop reasoning. A model that never reasoned
    # gets none: ollama ignores the key anyway (probe L-Q3) and a strict server would
    # answer a 400, buying a protocol downgrade for nothing.
    client, endpoint, _ = make_client([(200, _reply(ACTION))] * 3)

    client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert not any("chat_template_kwargs" in body for body in endpoint.bodies)


def test_an_endpoint_that_refuses_the_thinking_switch_drops_it_and_keeps_the_rung() -> None:
    """A strict server answers the unknown key with a 400, and the key is sticky.

    `chat_template_kwargs` is the newest thing in the body and the only one no
    OpenAI-compatible server promises to accept: a gateway validating with
    `extra="forbid"` rejects it. Read as "this rung is unsupported" the client spends the
    whole ladder on it and then keeps sending the same key on every later call, so every
    request of the task is refused -- three consecutive failures and `llm-unusable`.
    The switch is a request about the model's reasoning, not about the request shape,
    so a shape error is answered by dropping it and repeating the same rung.
    """

    class StrictEndpoint:
        """Truncates twice, refuses any body carrying the thinking switch, then answers."""

        def __init__(self) -> None:
            self.bodies: list[dict] = []

        def __call__(self, url, body, headers, timeout_s):
            parsed = json.loads(body)
            self.bodies.append(parsed)
            if "chat_template_kwargs" in parsed:
                return 400, b'{"error":"Extra inputs are not permitted: chat_template_kwargs"}'
            if len(self.bodies) <= 2:
                return 200, _reply("", finish="length", completion=2048)
            return 200, tool_reply(ACTION)

    endpoint = StrictEndpoint()
    events: list[tuple[str, str]] = []
    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(ENV),
        transport=endpoint,
        sleep=lambda _: None,
        on_event=lambda kind, detail: events.append((kind, detail)),
    )

    first = client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert isinstance(first, LlmReply)
    assert client.style == llm.NATIVE_TOOLS, "a refused key is not a refused rung"
    assert "chat_template_kwargs" in endpoint.bodies[2], "the switch was tried once"
    assert "chat_template_kwargs" not in endpoint.bodies[3], "and dropped for the retry"

    second = client.complete_structured([], schema=ACTION_LIKE, max_output_tokens=2048)

    assert isinstance(second, LlmReply)
    assert "chat_template_kwargs" not in endpoint.bodies[4], "and never sent again"
    assert client.style == llm.NATIVE_TOOLS
