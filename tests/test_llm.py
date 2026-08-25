"""The client is tested against a fake endpoint: no network, no sleeping, no key."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent import limits, llm
from agent.ports import LlmError, LlmPort, LlmReply

KEY = "sk-test-0123456789abcdef"
ENV = {
    "LOCAL_AGENT_MODEL": "small-local",
    "OPENAI_BASE_URL": "http://127.0.0.1:8000/v1/",
    "OPENAI_API_KEY": KEY,
}

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"action": {"type": "string"}},
    "required": ["action"],
}


def chat_reply(content: str, *, usage: bool = True) -> bytes:
    payload: dict[str, object] = {"choices": [{"message": {"content": content}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 12, "completion_tokens": 4}
    return json.dumps(payload).encode()


def tool_reply(arguments: str) -> bytes:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"function": {"name": "emit_action", "arguments": arguments}}],
                }
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }
    return json.dumps(payload).encode()


@dataclass
class FakeEndpoint:
    """Returns queued (status, body) pairs and remembers what it was sent."""

    responses: list[tuple[int, bytes]]
    bodies: list[dict] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    _next: int = 0

    def __call__(self, url, body, headers, timeout_s):
        self.bodies.append(json.loads(body))
        self.headers.append(dict(headers))
        self.timeouts.append(timeout_s)
        index = min(self._next, len(self.responses) - 1)
        self._next += 1
        return self.responses[index]


def make_client(responses, *, style=llm.NATIVE_TOOLS, env=None):
    endpoint = FakeEndpoint(responses=responses)
    events: list[tuple[str, str]] = []
    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(env or ENV),
        transport=endpoint,
        style=style,
        sleep=lambda _: None,
        on_event=lambda kind, detail: events.append((kind, detail)),
    )
    return client, endpoint, events


def test_client_satisfies_the_port() -> None:
    client, _, _ = make_client([(200, chat_reply("hi"))])
    assert isinstance(client, LlmPort)


# --- settings and secrets ---------------------------------------------------


def test_sanitized_summary_never_contains_the_key() -> None:
    settings = llm.LlmSettings.from_env(ENV)
    summary = settings.sanitized_summary()
    assert summary == {
        "model": "small-local",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_present": True,
    }
    assert KEY not in json.dumps(summary)


def test_missing_variables_are_named_and_the_call_is_not_attempted() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("hi"))], env={"OPENAI_API_KEY": KEY})
    result = client.complete([], max_output_tokens=16)
    assert isinstance(result, LlmError)
    assert "LOCAL_AGENT_MODEL" in result.reason and "OPENAI_BASE_URL" in result.reason
    assert not result.retryable
    assert endpoint.bodies == [], "a misconfigured run must not reach the network"


def test_the_key_travels_only_in_the_authorization_header() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("hi"))])
    client.complete([{"role": "user", "content": "hi"}], max_output_tokens=16)
    assert endpoint.headers[0]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(endpoint.bodies[0])


def test_endpoint_error_bodies_are_redacted_before_they_reach_the_trace() -> None:
    leaky = json.dumps({"error": f"bad key {KEY}"}).encode()
    client, _, _ = make_client([(401, leaky)])
    result = client.complete([], max_output_tokens=16)
    assert isinstance(result, LlmError)
    assert KEY not in result.reason
    assert "<redacted>" in result.reason


# --- happy paths ------------------------------------------------------------


def test_plain_completion_reports_usage() -> None:
    client, _, _ = make_client([(200, chat_reply("answer"))])
    result = client.complete([], max_output_tokens=16)
    assert isinstance(result, LlmReply)
    assert (result.text, result.input_tokens, result.output_tokens) == ("answer", 12, 4)
    assert result.usage_reported


def test_missing_usage_block_is_flagged_not_guessed() -> None:
    client, _, _ = make_client([(200, chat_reply("answer", usage=False))])
    result = client.complete([], max_output_tokens=16)
    assert isinstance(result, LlmReply)
    assert not result.usage_reported
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_native_tool_call_arguments_become_the_reply() -> None:
    client, endpoint, _ = make_client([(200, tool_reply('{"action":"ls"}'))])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)
    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert endpoint.bodies[0]["tool_choice"]["function"]["name"] == "emit_action"


def test_plain_completion_never_requests_a_response_format() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("answer"))])
    client.complete([], max_output_tokens=16)
    assert "response_format" not in endpoint.bodies[0]
    assert "tools" not in endpoint.bodies[0]


# --- the ladder -------------------------------------------------------------


def test_rejected_request_shape_steps_down_one_rung_and_succeeds() -> None:
    client, endpoint, events = make_client(
        [
            (400, b'{"error":"tools are not supported"}'),
            (200, chat_reply('{"action":"ls"}')),
        ]
    )
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert client.style == llm.JSON_SCHEMA
    assert "tools" in endpoint.bodies[0]
    assert endpoint.bodies[1]["response_format"]["type"] == "json_schema"
    assert [kind for kind, _ in events] == ["degrade"]


def test_the_downgrade_sticks_for_the_rest_of_the_run() -> None:
    client, endpoint, _ = make_client(
        [
            (400, b'{"error":"tools are not supported"}'),
            (200, chat_reply('{"action":"ls"}')),
        ]
    )
    client.complete_structured([], schema=SCHEMA, max_output_tokens=16)
    client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert "tools" not in endpoint.bodies[2], "we pay for the discovery once, not per call"


def test_ladder_walks_all_the_way_down_to_salvaged_text() -> None:
    prose = 'Sure! Here you go:\n```json\n{"action": "ls"}\n```\nHope that helps.'
    client, endpoint, events = make_client(
        [
            (400, b"no tools"),
            (400, b"no json_schema"),
            (400, b"no json_object"),
            (200, chat_reply(prose)),
        ]
    )
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert client.style == llm.TEXT
    assert len(events) == 3
    assert "response_format" not in endpoint.bodies[3]


def test_unparseable_reply_also_steps_down() -> None:
    client, _, events = make_client(
        [
            (200, chat_reply("I cannot do that")),
            (200, chat_reply('{"action":"ls"}')),
        ],
        style=llm.JSON_SCHEMA,
    )
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)
    assert isinstance(result, LlmReply)
    assert client.style == llm.JSON_OBJECT
    assert events[0][0] == "degrade"


def test_bottom_of_the_ladder_gives_up_without_reasking() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("no json here at all"))], style=llm.TEXT)
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert client.style == llm.TEXT
    assert not result.retryable
    assert len(endpoint.bodies) == 1, (
        "temperature is 0 and the request would be identical, so a retry only spends tokens"
    )


def test_a_rejected_key_is_not_laddered_away() -> None:
    client, endpoint, _ = make_client([(401, b'{"error":"invalid api key"}')])
    result = client.complete([], max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert not result.retryable
    assert len(endpoint.bodies) == 1, "a plain call has no fallback to burn attempts on"


# --- retries and timeouts ---------------------------------------------------


def test_server_error_is_retried_once_then_reported() -> None:
    # Once, not until the schedule runs out. A 500 that survives one retry is more
    # likely a deterministic parser failure than an outage -- llama.cpp returns one
    # when it cannot parse the model's own tool arguments.
    client, endpoint, events = make_client([(503, b"upstream unavailable")])
    result = client.complete([], max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert result.retryable
    assert len(endpoint.bodies) == 2
    assert [kind for kind, _ in events] == ["retry"]


def test_a_persistent_server_error_changes_the_request_shape() -> None:
    client, _, events = make_client(
        [
            (503, b"parse failure"),
            (503, b"parse failure"),
            (200, chat_reply('{"action":"ls"}')),
        ]
    )
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert client.style == llm.JSON_SCHEMA
    assert [kind for kind, _ in events] == ["retry", "degrade"]


def test_transport_exception_becomes_an_error_not_a_crash() -> None:
    def explode(*_args):
        raise TimeoutError("connection timed out")

    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(ENV), transport=explode, sleep=lambda _: None
    )
    result = client.complete([], max_output_tokens=16)
    assert isinstance(result, LlmError)
    assert "TimeoutError" in result.reason


def test_the_call_never_outlives_the_time_it_was_given() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("answer"))])
    client.complete([], max_output_tokens=16, timeout_s=5.0)
    assert endpoint.timeouts == [5.0]


def test_a_generous_caller_still_cannot_exceed_the_ceiling() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("answer"))])
    client.complete([], max_output_tokens=16, timeout_s=9999.0)
    assert endpoint.timeouts == [limits.LLM_TIMEOUT_S]


def test_no_time_left_means_no_call() -> None:
    client, endpoint, _ = make_client([(200, chat_reply("answer"))])
    result = client.complete([], max_output_tokens=16, timeout_s=0.0)
    assert isinstance(result, LlmError)
    assert endpoint.bodies == []


def test_fallback_can_be_switched_off_for_measurement() -> None:
    client, endpoint, events = make_client([(400, b"no tools")])
    client.allow_fallback = False
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert client.style == llm.NATIVE_TOOLS, "the probe must report this rung, not a lower one"
    assert events == []
    assert len(endpoint.bodies) == 1


# --- what the local-server survey corrected ---------------------------------


def test_a_keyless_endpoint_is_usable() -> None:
    # vLLM, SGLang and llama.cpp check Bearer only when started with --api-key;
    # LM Studio and a local Ollama are keyless outright. Harbor also forwards only
    # non-empty variables, so an unset key reaches us as an absent one. Refusing to
    # call would be a zero on every task for a normal condition.
    keyless = {"LOCAL_AGENT_MODEL": "small-local", "OPENAI_BASE_URL": "http://127.0.0.1:8000/v1"}
    client, endpoint, _ = make_client([(200, chat_reply("answer"))], env=keyless)

    assert client.settings.missing() == ()
    result = client.complete([], max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert "Authorization" not in endpoint.headers[0], "no key means no empty Bearer header"


def test_rate_limiting_waits_instead_of_crippling_the_protocol() -> None:
    client, endpoint, events = make_client(
        [(429, b'{"error":"rate limit exceeded"}'), (200, tool_reply('{"action":"ls"}'))]
    )
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert client.style == llm.NATIVE_TOOLS, "a 429 says nothing about the request shape"
    assert [kind for kind, _ in events] == ["retry"]
    assert "tools" in endpoint.bodies[1], "the retry repeats the same shape"


def test_an_unknown_model_is_fatal_and_not_laddered() -> None:
    client, endpoint, events = make_client([(404, b'{"error":"model not found"}')])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert not result.retryable
    assert client.style == llm.NATIVE_TOOLS
    assert len(endpoint.bodies) == 1
    assert events == []


def test_a_rejected_key_is_fatal_even_for_a_structured_call() -> None:
    client, endpoint, _ = make_client([(401, b'{"error":"invalid api key"}')])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert len(endpoint.bodies) == 1, "no key problem was ever solved by dropping a rung"


def test_the_tgi_tool_call_shape_is_accepted() -> None:
    # TGI answers with tool_calls as an object and the payload already parsed under
    # function.parameters. Calling that unusable would discard a correct answer.
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": {
                            "function": {"name": "emit_action", "parameters": {"action": "ls"}}
                        },
                    }
                }
            ]
        }
    ).encode()
    client, _, _ = make_client([(200, payload)])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert client.style == llm.NATIVE_TOOLS, "a usable answer must not cost a rung"


def test_a_tool_call_stranded_in_content_is_recovered() -> None:
    # Documented on vLLM and LM Studio: when the server's parser fails, the call is
    # left in message.content with tool_calls empty.
    payload = json.dumps(
        {"choices": [{"message": {"content": '{"action": "ls"}', "tool_calls": []}}]}
    ).encode()
    client, _, events = make_client([(200, payload)])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert any(kind == "protocol" for kind, _ in events)


def test_several_tool_calls_are_recorded_not_silently_dropped() -> None:
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"function": {"name": "emit_action", "arguments": '{"action":"ls"}'}},
                            {"function": {"name": "emit_action", "arguments": '{"action":"rm"}'}},
                        ],
                    }
                }
            ]
        }
    ).encode()
    client, _, events = make_client([(200, payload)])
    result = client.complete_structured([], schema=SCHEMA, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert any("2 tool calls" in detail for _, detail in events)


def test_valid_json_that_breaks_the_schema_is_not_a_success() -> None:
    # vLLM has documented tool calls that are valid JSON with mangled argument names.
    # Without validation the loop would act on a reply that means nothing.
    strict: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
        "additionalProperties": False,
    }
    client, _, events = make_client(
        [(200, chat_reply('{"actoin":"ls"}')), (200, chat_reply('{"action":"ls"}'))],
        style=llm.JSON_SCHEMA,
    )
    result = client.complete_structured([], schema=strict, max_output_tokens=16)

    assert isinstance(result, LlmReply)
    assert json.loads(result.text) == {"action": "ls"}
    assert any("schema" in detail for _, detail in events)


def test_a_wrong_primitive_type_is_caught() -> None:
    typed: dict[str, object] = {
        "type": "object",
        "properties": {"steps": {"type": "integer"}},
        "required": ["steps"],
    }
    client, _, _ = make_client([(200, chat_reply('{"steps":"three"}'))], style=llm.TEXT)
    result = client.complete_structured([], schema=typed, max_output_tokens=16)

    assert isinstance(result, LlmError)
    assert "schema" in result.reason
