"""Talking to the local endpoint, and surviving it when it talks back badly.

Three things are borrowed here.

From datacon (`src/agents/base.py`): settings assembled from the environment, and a
`sanitized_summary()` that is safe to log because it reports `api_key_present: bool`
instead of the key. Also the shape of the degradation chain -- try the richer request,
fall back to the plainer one when the endpoint refuses it.

From ai-v-massy (`adapters/llm/aistudio.py`): one client object reused across calls,
and a failed call returned as a value.

From our own constraints: standard library only, so the transport is `urllib.request`
behind a callable that tests replace.

The fallback ladder exists because the evaluated model is small and its support for
native tool calls is unverified. It degrades lazily and stickily: the first refusal
drops the level for the rest of the run, so we pay for the discovery once rather than
on every call.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent import limits
from agent.ports import LlmError, LlmReply, Message
from agent.redact import redact, secrets_from_env

NATIVE_TOOLS = "native_tools"
JSON_SCHEMA = "json_schema"
JSON_OBJECT = "json_object"
TEXT = "text"

LADDER: tuple[str, ...] = (NATIVE_TOOLS, JSON_SCHEMA, JSON_OBJECT, TEXT)
"""Request shapes from richest to plainest. Only downward moves are allowed."""

TOOL_NAME = "emit_action"
"""Function name used when the endpoint supports native tool calls."""

ERROR_DETAIL_LIMIT = 300
"""How much of an endpoint error body is kept. Enough to diagnose, not enough to
flood the trace with an HTML error page."""

Transport = Callable[[str, bytes, dict[str, str], float], "tuple[int, bytes]"]
"""(url, body, headers, timeout_s) -> (status, body). Replaced wholesale in tests."""


def _urllib_transport(
    url: str, body: bytes, headers: dict[str, str], timeout_s: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A 4xx carries the reason the endpoint rejected us, and that reason is what
        # tells the ladder to step down. Losing it would make every refusal look the
        # same as a network outage.
        return exc.code, exc.read()


@dataclass(frozen=True)
class LlmSettings:
    """Endpoint coordinates. The key lives here and leaves only in a header."""

    model: str
    base_url: str
    # repr=False so a stray f-string, a logging call or a traceback formatter cannot
    # print the key or the secret list: their only exit is the Authorization header.
    api_key: str = field(repr=False)
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LlmSettings:
        return cls(
            model=env.get(limits.MODEL_ENV_NAME, "").strip(),
            base_url=env.get(limits.BASE_URL_ENV_NAME, "").strip().rstrip("/"),
            api_key=env.get(limits.API_KEY_ENV_NAME, "").strip(),
            secrets=secrets_from_env(env),
        )

    def missing(self) -> tuple[str, ...]:
        """Which genuinely required variables are empty. Checked before the first call.

        The API key is deliberately not required. Local OpenAI-compatible servers are
        keyless unless explicitly started otherwise: vLLM checks Bearer only with
        `--api-key`/`VLLM_API_KEY`, SGLang defaults `--api-key` to None, llama.cpp
        only enforces it when started with `--api-key`, LM Studio and a local Ollama
        need none at all. Harbor also passes only non-empty variables through, so an
        unset key reaches us as an absent variable rather than an empty string.

        Treating that as a fatal misconfiguration would mean refusing to make a single
        request against an ordinary local endpoint -- a zero on every task for a
        condition that is normal.
        """
        absent = []
        if not self.model:
            absent.append(limits.MODEL_ENV_NAME)
        if not self.base_url:
            absent.append(limits.BASE_URL_ENV_NAME)
        return tuple(absent)

    def headers(self) -> dict[str, str]:
        """Request headers. Authorization appears only when there is a key to send."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def sanitized_summary(self) -> dict[str, Any]:
        """Everything about the endpoint that is safe to write down."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
        }


AUTH_STATUSES = frozenset({401, 403})
NOT_FOUND_STATUSES = frozenset({404})
PRESSURE_STATUSES = frozenset({408, 409, 425, 429})
SHAPE_STATUSES = frozenset({400, 422, 501})

FATAL = "fatal"
RETRY = "retry"
SHAPE = "shape"


def classify_status(status: int) -> str:
    """Decide what an HTTP status means for the ladder.

    Treating every 4xx as "this request shape is unsupported" is wrong, and the
    documented behaviour of local servers says so: Ollama returns 400 for malformed or
    missing parameters, 404 for an unknown model and 429 for rate limiting, while
    llama.cpp returns 401 for a bad key. Downgrading the protocol on a 429 would spend
    three needless requests and then permanently cripple the client for a condition
    that clears on its own.

    No source in the reviewed set promises a specific status for an unsupported
    parameter, so `SHAPE` is a candidate for stepping down, not proof of one.
    """
    if status in AUTH_STATUSES or status in NOT_FOUND_STATUSES:
        return FATAL
    if status in PRESSURE_STATUSES:
        return RETRY
    if status in SHAPE_STATUSES:
        return SHAPE
    if status >= 500:
        # Retried once, then treated as a shape problem. A 500 is not always an
        # outage: llama.cpp returns one when it fails to parse the model's tool-call
        # arguments, which at temperature 0 repeats forever.
        return RETRY
    return FATAL


def validate_against_schema(value: Any, schema: dict[str, Any]) -> str | None:
    """Return a reason the value fails the schema, or None if it passes.

    Deliberately partial: enough to catch what small models actually get wrong -- a
    missing required key, an invented key, a value of the wrong primitive type -- and
    no more. There is no jsonschema package in the target image and writing a full
    validator would be building a library instead of using one.

    This matters because syntactically valid JSON is not a correct answer. Tool calls
    with mangled argument names are documented on vLLM, and without this check such a
    reply is accepted as a success and never triggers the fallback.
    """
    if schema.get("type") == "object" and not isinstance(value, dict):
        return f"expected an object, got {type(value).__name__}"
    if not isinstance(value, dict):
        return None

    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    for key in schema.get("required", []):
        if key not in value:
            return f"missing required key {key!r}"

    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                return f"unexpected key {key!r}"

    kinds: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, spec in properties.items():
        if key not in value or not isinstance(spec, dict):
            continue
        expected = kinds.get(spec.get("type", ""))
        if expected and not isinstance(value[key], expected):
            return f"key {key!r} should be {spec['type']}"

    return None


def _extract_usage(payload: dict[str, Any]) -> tuple[int, int, bool]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, False
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return 0, 0, False
    return prompt, completion, True


def _first_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return message
    return {}


def _unwrap_function_envelope(value: Any, schema: dict[str, Any]) -> dict[str, Any] | None:
    """A function call written as prose, mapped onto the action schema. None if it isn't one.

    Measured, not imagined: in all 36 task runs of E033 and E034 the first reply of a
    task came back as `{"name": "list_dir", "arguments": {"path": "/app"}}` in
    `content` -- the OpenAI function shape, but naming the tool the model wants instead
    of the one function the request offers. The salvaged JSON then failed the schema on
    a missing `tool` key, the rung was declared unsupported and the same question was
    asked again one rung down. Two requests for the first action of every task, inside
    the step that already costs 39-49 s; benched at 21.6 s against 7.9 s
    (`scripts/eval/bench_first_call.py`).

    This is the fourth dialect of the decision of 25.08 that accepts a tool call stuck
    in `content`, and it is read off the schema rather than told about it: either the
    arguments are the action, or some required key of the schema takes an enum that the
    function name belongs to and the name is that key's value. Nothing here knows that
    the key is called `tool`.

    A reply that does not fit is left alone, so a genuinely malformed answer still
    costs a rung exactly as before.
    """
    if not isinstance(value, dict) or set(value) - {"name", "arguments"}:
        return None
    name = value.get("name")
    arguments = value.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None

    if validate_against_schema(arguments, schema) is None:
        return arguments

    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    for key in schema.get("required", []):
        allowed = (properties.get(key) or {}).get("enum")
        if isinstance(allowed, list) and name in allowed:
            candidate = {key: name, **arguments}
            if validate_against_schema(candidate, schema) is None:
                return candidate
    return None


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """Remove `<think>...</think>` reasoning a Qwen3-style model emits before its answer.

    Two failures it prevents, both silent. A model that drafts JSON inside the block --
    `<think>I'll return {"tool": "read_range"}</think>{"tool": "list_dir"}` -- would
    otherwise have the draft salvaged as the action, because `_salvage_json` takes the
    first `{` in the text; the real object after `</think>` must win. And a reply cut
    off mid-thought (the completion hit its token cap inside the block) leaves an
    unclosed `<think>` and no answer at all, which has to read as empty rather than be
    parsed as prose. Nothing is removed when the model emits no such tags, so a plain
    OpenAI endpoint is untouched.
    """
    text = _THINK_BLOCK.sub("", text)
    opened = text.find("<think>")
    if opened >= 0:
        text = text[:opened]
    return text.strip()


def _salvage_json(text: str) -> str | None:
    """Pull the outermost JSON object out of prose.

    Only needed on the bottom rung, where the model was merely asked to answer in
    JSON and tends to wrap it in an explanation or a fenced block.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


@dataclass
class HttpLlm:
    """OpenAI-compatible client with a finite retry budget and a one-way ladder."""

    settings: LlmSettings
    transport: Transport = _urllib_transport
    style: str = limits.DEFAULT_CALL_STYLE
    allow_fallback: bool = True
    """Off only for measurement: `scripts/probe_endpoint.py` needs to learn whether a
    single rung works, and silent recovery would hide exactly that answer."""

    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    on_event: Callable[[str, str], None] | None = field(default=None, repr=False)
    """Called as (kind, detail) for protocol downgrades and retries, so the loop can
    put them in the audit trail. Never receives secrets: text is redacted first."""

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        """Free-form answer. No response format is requested, so no ladder applies."""
        return self._attempt_loop(
            messages,
            schema=None,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )

    def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        """Answer that parses as JSON, or an error saying why it does not."""
        return self._attempt_loop(
            messages,
            schema=schema,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )

    # --- internals ----------------------------------------------------------

    def _note(self, kind: str, detail: str) -> None:
        if self.on_event is not None:
            self.on_event(kind, redact(detail, self.settings.secrets))

    def _leave_the_native_rung(self, in_content: bool) -> None:
        """Answer accepted; ask the next question in the shape this server really serves.

        A reply that arrives in `content` while `tools` were offered says the server is
        not doing tool calls -- it is completing text that looks like one. Keeping the
        rung then costs on every later call: E037 stayed native for whole runs and paid
        63.1 s for a first call against 44.7 s, took 84 planning steps instead of 128
        and left a deliverable on 3 tasks of 18 instead of 14, at 1354 tokens a call
        against 1199. The bench had said as much before the run -- native 16.1 s,
        json_schema 7.9 s -- and the reading was not carried through.

        The answer in hand is still used, so this costs no request. And it is not the
        bet that E036 lost: the ladder still starts on `native_tools`, so a server that
        only takes `tools` is always offered them first.

        Weighed and kept: `_read_tool_call` documents a fourth dialect -- vLLM or LM
        Studio failing to parse a call it does serve, leaving it in `content`. That
        reply is shaped exactly like a server that does not do tool calls at all, so
        this demotes on it too, and one parser glitch costs the rung for the rest of
        the run. Waiting for a second such reply would tell the two apart and cost a
        second native call on every task: 16.1 s against 7.9 on the stand, where the
        rung has never once answered in `tool_calls` and the demotion is therefore
        always right. A wrong demotion lands on `json_schema`, which is constrained by
        the same schema, and the ladder keeps descending if that fails too. See
        `docs/decisions.md`, 2026-09-04.
        """
        if in_content:
            self._demote("tool calls arrive in content")

    def _demote(self, why: str) -> bool:
        """Step down one rung. Returns False when already at the bottom."""
        if not self.allow_fallback:
            return False
        index = LADDER.index(self.style)
        if index + 1 >= len(LADDER):
            return False
        self.style = LADDER[index + 1]
        self._note("degrade", f"{why} -> call style {self.style}")
        return True

    def _build_body(
        self,
        messages: list[Message],
        schema: dict[str, object] | None,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": max_output_tokens,
            "temperature": 0,
        }
        if schema is None:
            return body

        if self.style == NATIVE_TOOLS:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": TOOL_NAME, "parameters": schema},
                }
            ]
            # `tool_choice` is deliberately not sent. The organisers' reference agent
            # registers its tools through pydantic-ai without forcing a choice, and
            # that is the configuration known to work against the evaluation endpoint.
            # Sending it would add the one parameter Ollama documents as unsupported
            # and that TGI expects in a different shape, buying a downgrade for
            # transport reasons rather than model quality. If the model answers in
            # prose instead of calling the tool, `_read_reply` recovers the JSON.
        elif self.style == JSON_SCHEMA:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": TOOL_NAME, "schema": schema, "strict": True},
            }
        elif self.style == JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}
            body["messages"] = [
                *messages,
                {
                    "role": "system",
                    "content": "Reply with one JSON object matching this schema and "
                    f"nothing else: {json.dumps(schema, separators=(',', ':'))}",
                },
            ]
        else:
            body["messages"] = [
                *messages,
                {
                    "role": "system",
                    "content": "Reply with one JSON object matching this schema and "
                    f"nothing else: {json.dumps(schema, separators=(',', ':'))}",
                },
            ]
        return body

    def _read_tool_call(self, message: dict[str, Any]) -> str | None:
        """Pull the arguments out of a tool call, across the dialects seen in the wild.

        Three shapes are accepted because three shapes are documented:

        * OpenAI and vLLM: `tool_calls` is a list, `function.arguments` is a JSON
          string;
        * TGI: `tool_calls` is a single object and the payload sits in
          `function.parameters` as an object already;
        * a flat entry: `name` and `arguments` at the top level with no `function`
          wrapper. llama.cpp documents this shape, though the server built at the
          commit that documents it returns the wrapped one (E010); accepted here
          because the endpoint is unknown by definition and the branch costs nothing;
        * a parser failure on vLLM or LM Studio: `tool_calls` is empty and the call is
          left in `message.content`, which the caller then salvages.

        Rejecting the TGI shape as unusable would throw away a correct answer and
        downgrade the protocol for no reason.
        """
        calls = message.get("tool_calls")
        if isinstance(calls, dict):
            calls = [calls]
        if not isinstance(calls, list) or not calls:
            return None

        if len(calls) > 1:
            # The protocol has exactly one action per turn. More than one means the
            # model produced conflicting intent; taking the first silently would hide
            # that, so it goes into the trace.
            self._note("protocol", f"{len(calls)} tool calls returned, using the first")

        if not isinstance(calls[0], dict):
            return None
        function = calls[0].get("function")
        if not isinstance(function, dict):
            # A flat entry carries name and arguments itself. Documented by
            # llama.cpp, not observed from it -- see the docstring.
            function = calls[0]

        if function.get("name") != TOOL_NAME:
            # The request offers exactly one function. Never execute arguments from
            # a call for another function just because their JSON happens to match
            # our action schema; an unknown function is a protocol error.
            return None

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            return arguments
        if isinstance(arguments, dict):
            return json.dumps(arguments)

        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            return json.dumps(parameters)
        return None

    def _read_reply(
        self, payload: dict[str, Any], schema: dict[str, object] | None
    ) -> tuple[str | None, str]:
        """Return (text, "") on success, or (None, reason) when the reply is unusable."""
        message = _first_message(payload)
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        # Before anything reads the content: drop a thinking model's <think> reasoning,
        # so a JSON draft inside it is never salvaged as the action. Harmless on a model
        # that emits none. tool_calls, when present, are read below and not affected.
        content = _strip_thinking(content)

        if schema is None:
            return (content, "") if content else ("", "empty content")

        in_content = False
        if self.style == NATIVE_TOOLS:
            arguments = self._read_tool_call(message)
            if arguments is not None:
                content = arguments
            elif content:
                # The server's parser failed but the model did produce a call. Better
                # to recover it than to change the prompt and hope.
                self._note("protocol", "tool call arrived in content, not tool_calls")
                in_content = True
            else:
                return None, "no tool call and no content"

        if not content:
            return None, "empty content"

        salvaged = _salvage_json(content)
        if salvaged is not None:
            content = salvaged

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, f"not json: {exc.msg}"

        reason = validate_against_schema(parsed, dict(schema))
        if reason is not None:
            unwrapped = _unwrap_function_envelope(parsed, dict(schema))
            if unwrapped is None:
                return None, f"schema: {reason}"
            self._note("protocol", "function envelope in content, unwrapped")
            self._leave_the_native_rung(in_content)
            return json.dumps(unwrapped), ""

        self._leave_the_native_rung(in_content)
        return content, ""

    def _attempt_loop(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object] | None,
        max_output_tokens: int,
        timeout_s: float | None,
    ) -> LlmReply | LlmError:
        missing = self.settings.missing()
        if missing:
            return LlmError(reason=f"endpoint not configured: {', '.join(missing)}", retryable=False)

        budget_s = limits.LLM_TIMEOUT_S if timeout_s is None else min(limits.LLM_TIMEOUT_S, timeout_s)
        if budget_s <= 0:
            return LlmError(reason="no time left for an llm call", retryable=False)

        # `budget_s` bounds the whole logical call -- retries, fallback demotions and
        # backoff sleeps included -- not each attempt's own transport timeout. The
        # deadline is fixed once; every attempt after the first is given whatever is
        # left of it. Attempt 0 uses `budget_s` directly, without an extra clock()
        # read, so a caller asking for an exact timeout gets exactly that value.
        deadline = self.clock() + budget_s

        url = f"{self.settings.base_url}/chat/completions"
        headers = self.settings.headers()

        last = LlmError(reason="no attempt was made", retryable=False)
        retries_at_style = 0

        for attempt in range(limits.LLM_MAX_ATTEMPTS):
            remaining = budget_s if attempt == 0 else deadline - self.clock()
            if remaining <= 0:
                return last

            body = self._build_body(messages, schema, max_output_tokens)
            try:
                status, raw = self.transport(
                    url, json.dumps(body).encode("utf-8"), headers, remaining
                )
            except Exception as exc:  # transport failure of any kind
                last = LlmError(reason=f"transport failed: {type(exc).__name__}", retryable=True)
                if not self._backoff(attempt, deadline):
                    return last
                continue

            if status >= 400:
                detail = redact(raw.decode("utf-8", "replace"), self.settings.secrets)
                verdict = classify_status(status)
                last = LlmError(
                    reason=f"endpoint returned {status}: {detail[:ERROR_DETAIL_LIMIT]}",
                    retryable=verdict == RETRY,
                )

                if verdict == FATAL:
                    # A rejected key, an unknown model or an unknown route. The ladder
                    # cannot fix any of them and retrying only spends the deadline.
                    return last

                if verdict == RETRY:
                    # One retry at the current rung. A server error that survives it is
                    # more likely a deterministic parser failure than an outage, so the
                    # second response is to change the request shape, not repeat it.
                    if retries_at_style == 0 and self._backoff(attempt, deadline):
                        retries_at_style += 1
                        continue
                    if schema is not None and self._demote(f"persistent http {status}"):
                        retries_at_style = 0
                        continue
                    return last

                if schema is not None and self._demote(f"http {status}"):
                    retries_at_style = 0
                    continue
                return last

            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                last = LlmError(reason="endpoint reply was not json", retryable=True)
                if not self._backoff(attempt, deadline):
                    return last
                continue

            text, reason = self._read_reply(payload, schema)
            if text is None:
                last = LlmError(reason=f"{reason} at style {self.style}", retryable=False)
                if schema is not None and self._demote(reason):
                    retries_at_style = 0
                    continue
                # Not retried on purpose. Sampling is at temperature 0 and the request
                # would be byte-identical, so a repeat buys the same reply and pays
                # for it in tokens, which is the second ranking criterion.
                return last

            prompt_tokens, completion_tokens, reported = _extract_usage(payload)
            return LlmReply(
                text=text,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                usage_reported=reported,
            )

        return last

    def _backoff(self, attempt: int, deadline: float) -> bool:
        """Wait before the next attempt. False when the schedule or the deadline is
        exhausted. The rung is clamped to what is actually left: sleeping the full
        rung regardless would blow past a deadline narrower than the schedule --
        a fast failure with little time left must not sleep 3s for a 1s budget."""
        if attempt >= len(limits.LLM_RETRY_BACKOFF_S):
            return False
        remaining = deadline - self.clock()
        if remaining <= 0:
            return False
        delay = min(limits.LLM_RETRY_BACKOFF_S[attempt], remaining)
        self._note("retry", f"attempt {attempt + 1} failed, waiting {delay}s")
        self.sleep(delay)
        return True
