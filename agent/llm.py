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
from dataclasses import dataclass, field, replace
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


def _finish_reason(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str):
            return reason
    return ""


def _is_timeout(exc: BaseException) -> bool:
    """Whether a transport failure is the deadline expiring rather than a broken endpoint.

    `urlopen` reports a read timeout as `TimeoutError` (`socket.timeout` is an alias since
    3.10) and a connect timeout wrapped in `URLError`, so both shapes are checked. The
    distinction matters twice over: a timeout is the only thinking signal available before
    any reply exists, and it is the failure the loop must not count as an unusable
    endpoint while the server is merely still generating.
    """
    seen: list[int] = []
    current: object = exc
    # Iterative and bounded: a wrapped cause is one level deep in practice, and a chain
    # that points back at itself must not become a RecursionError inside the client.
    for _ in range(4):
        if isinstance(current, TimeoutError):
            return True
        if id(current) in seen:
            return False
        seen.append(id(current))
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            current = reason
            continue
        return isinstance(reason, str) and "timed out" in reason.lower()
    return False


def _transport_reason(exc: BaseException) -> str:
    """One line naming a transport failure, with timeouts spelled out.

    The type name alone is ambiguous: a connect timeout arrives as `URLError`, which reads
    exactly like a refused connection, and the loop's failure classification turns on the
    difference.
    """
    if _is_timeout(exc):
        return f"transport timeout: {type(exc).__name__}"
    return f"transport failed: {type(exc).__name__}"


def _looks_like_context_overflow(detail: str) -> str | None:
    """The marker in a 400 body that says the raised output budget overran the context.

    vLLM rejects `prompt + max_tokens > num_ctx` with a 400 naming the context length or
    max_tokens; the fix is a smaller ceiling, not a protocol downgrade. Returns the
    marker matched, or None.
    """
    lowered = detail.lower()
    # Context-specific markers only: a generic 400 (an unsupported response_format on a
    # strict server, say) must still step the protocol down, not lower the ceiling.
    for marker in ("context length", "context window", "maximum context", "context_length", "max_tokens", "max_new_tokens"):
        if marker in lowered:
            return marker
    return None


def _has_reasoning_field(message: dict[str, Any]) -> bool:
    """Whether the endpoint returned reasoning in its own field (ollama, some vLLM).

    A signal the endpoint is a thinking model even when this particular content parsed:
    the reasoning was still generated and billed, and later calls will need the headroom.
    """
    for key in ("reasoning", "reasoning_content", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _thinking_evidence(
    message: dict[str, Any],
    finish: str,
    *,
    requested_tokens: int,
    completion_tokens: int,
    usage_reported: bool,
) -> str:
    """Why this reply says the endpoint reasons before answering, or "" when it does not.

    The distinction is the whole point, and it was bought with a measurement. F4 flipped
    the thinking flag on a bare `finish_reason=length`, and on the fast 7B that fired on a
    degenerate generation with an ordinary, non-empty answer: the run then paid the
    thinking-endpoint call ceiling on two more steps and cost 250 s of wall clock for the
    same zero (`F4-7b-regression-4080`, 15.09). A model that ran out of room while
    answering is looping; a model that ran out of room while *thinking* has produced
    tokens we cannot see.

    So a cutoff alone is not evidence. Three things are:

    * a `reasoning`/`reasoning_content`/`thinking` field -- the endpoint says so itself;
    * a `<think>`/`</think>` tag inside a truncated reply -- the model says so itself;
    * a truncated reply with nothing visible in it, having spent the whole output budget.
      The tokens were generated and billed and did not reach `content`, which is what a
      thinking span is. `completion_tokens` is only consulted when the endpoint reports
      usage at all; a server that reports none cannot be asked to corroborate.

    The third case is also the one that matters most in time: it is the shape the very
    first call of every episode came back in on the 30B stand (`F4-30b-paired`, ctok=2048
    and no answer), which is the earliest moment the run can learn what it is talking to.
    """
    if _has_reasoning_field(message):
        return "reasoning field"
    if finish != "length":
        return ""
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    if "<think>" in content or "</think>" in content:
        return "truncated inside a think block"
    if content.strip() or message.get("tool_calls"):
        # A truncated but ordinary answer -- prose or a function call (whose `content`
        # is null by the OpenAI shape) -- means the model was still talking to us. The
        # native-tools rung is the default path, and a long `write_file` body is the
        # commonest thing to overrun the ceiling; that is looping, not thinking.
        return ""
    if not usage_reported or completion_tokens < requested_tokens:
        # Nothing visible, and either the output budget was not spent (an empty reply,
        # not a hidden one) or the endpoint cannot corroborate that it was. Without
        # corroboration the cheap hypothesis wins: the thinking ceiling is expensive.
        return ""
    return "truncated with no visible answer"


def _runaway_reasoning(message: dict[str, Any], evidence: str, *, was_raised: bool) -> bool:
    """Whether this truncation is the model reasoning past the raised cap without acting.

    The failure it names, measured on the Qwen3.6-35B-A3B stand (16.09, treatment-5b call
    traces, calls 4-7): late in a run, at a prompt of ~6.9k tokens, every call comes back
    with `completion_tokens` equal to the raised ceiling, empty `content`, no tool call and
    ~70 s on the clock. The ladder has nothing left to try -- the budget is already raised
    and `enable_thinking=false` is ignored by ollama -- so the run spends its three
    consecutive failures and ends as `llm-unusable` with a correct but unverified
    deliverable on disk.

    Three conditions, and each removes a different thing that merely looks like it:

    * `was_raised` -- the request already carried every token of output room this client
      is willing to give. A first truncation at the default ceiling is answered by more
      room, and that retry happens before anyone needs this verdict. Sticky and not a
      comparison against the ceiling actually sent, which matters in one case: after a
      strict server rejects the raised budget with a context-length 400, `_output_cap`
      comes back down and the request is small again while the flags stay set. A reply
      with nothing visible is still a runaway there -- more emphatically so, since the
      server has just said the prompt is what leaves no room for the answer;
    * `evidence` -- `_thinking_evidence` says the tokens went somewhere we cannot read.
      Reused rather than re-derived, so the two readings of one reply cannot drift apart;
    * nothing visible came back. A truncated tool call or truncated prose is a model that
      was still talking to us: it overran the ceiling while answering, which is looping,
      and the answer to looping is not a smaller prompt.

    `_strip_thinking` decides what "visible" means, for the same reason `_read_reply` uses
    it: a reply that is `<think>` and nothing else has emitted no answer, whatever its
    length.
    """
    if not was_raised or not evidence:
        return False
    if _tool_call_entries(message):
        # `_thinking_evidence` already refuses a bare truncated tool call; this also
        # covers the endpoint that returns a reasoning field *and* a cut-off call.
        return False
    content = message.get("content")
    return not _strip_thinking(content if isinstance(content, str) else "").strip()


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

    Three shapes, all of which would otherwise feed the reasoning to `_salvage_json`,
    whose first-`{` rule then takes a JSON draft out of the thinking as the action:

    * a closed `<think>...</think>` block before the answer -- removed;
    * a reply cut off mid-thought, leaving a *leading* unclosed `<think>` and no answer
      -- read as empty rather than parsed as prose. Only when leading, so a literal
      `<think>` inside a JSON string value (e.g. a `write_file` content) is left intact;
    * a `</think>` with no opener -- R1-style templates inject `<think>` into the prompt
      as a generation prefix, so the content arrives as `reasoning</think>{answer}`. The
      answer after the closer wins, unless the text already began as JSON (guarding a
      `</think>` that appears inside a JSON string value).

    The tag set is intentionally just `<think>`: Qwen3, QwQ and R1 all use it. Nothing
    is removed when the model emits none, so a plain OpenAI endpoint is untouched.
    """
    text = _THINK_BLOCK.sub("", text)
    if text.lstrip().startswith("<think>"):
        return ""
    closer = text.find("</think>")
    if closer >= 0 and "<think>" not in text and text.lstrip()[:1] not in {"{", "["}:
        text = text[closer + len("</think>") :]
    return text.strip()


def _tool_call_entries(message: dict[str, Any]) -> list[Any]:
    """The reply's tool calls as a list, whatever container the server used.

    TGI sends a single object where OpenAI sends a list; anything else -- absent, null,
    a string -- is no calls at all rather than an error, because the caller has a
    `content` fallback for exactly that case.
    """
    calls = message.get("tool_calls")
    if isinstance(calls, dict):
        calls = [calls]
    return calls if isinstance(calls, list) else []


def _call_payload(entry: Any) -> str | None:
    """The argument JSON of one tool-call entry, or None when it is not ours to run.

    The three accepted dialects are documented on `HttpLlm._read_tool_call`; this is
    that reading applied to one entry, so the first call and the extras after it are
    parsed by the same code and cannot drift apart.
    """
    if not isinstance(entry, dict):
        return None
    function = entry.get("function")
    if not isinstance(function, dict):
        # A flat entry carries name and arguments itself. Documented by llama.cpp,
        # not observed from it -- see `HttpLlm._read_tool_call`.
        function = entry

    if function.get("name") != TOOL_NAME:
        # The request offers exactly one function. Never execute arguments from a call
        # for another function just because their JSON happens to match our action
        # schema; an unknown function is a protocol error.
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


def _normalise_action(payload: str, schema: dict[str, Any]) -> str | None:
    """One extra call reduced to the JSON text of a schema-valid action, or None.

    The same three repairs the first call gets -- prose around the JSON, a function
    envelope instead of the flat action, plain schema validation -- and for the same
    reason: an action in an unexpected wrapper is still an action, and the alternative
    to repairing it is paying a whole step to be asked for it again. What it does not
    do is raise or demote; an extra that cannot be read is simply not carried.
    """
    text = _salvage_json(payload) or payload
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    if validate_against_schema(parsed, schema) is None:
        return text if isinstance(parsed, dict) else None

    unwrapped = _unwrap_function_envelope(parsed, schema)
    return None if unwrapped is None else json.dumps(unwrapped)


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

    _thinking_endpoint: bool = field(default=False, repr=False)
    """Set once, for the rest of the run, the first time a reply is *evidence* of a
    reasoning model (`_thinking_evidence`) or a call times out having been handed no
    payload at all. The endpoint is then a thinking model: the output ceiling is raised on
    every later call rather than paying a truncated 2048-token attempt first, and the
    per-call deadline may reach `LLM_THINKING_TIMEOUT_S` instead of `LLM_TIMEOUT_S`.
    One-way, like the protocol ladder.

    Deliberately not set by a bare length cutoff any more: see `_thinking_evidence`. The
    response to a cutoff -- more output room -- lives in `_output_raised` instead, so a
    model that merely ran long keeps its headroom without the endpoint being treated as
    slow for the rest of the run."""

    _output_raised: bool = field(default=False, repr=False)
    """Set once, for the rest of the run, by any reply truncated by length that could not
    be used. The next request gets the raised ceiling from its first attempt.

    Sticky and not per-call, because the alternative is measurably worse on both endpoints
    it could meet: a model that genuinely needs a long answer would pay a truncated
    attempt before every raised one, and a model looping into a degenerate generation
    would pay the truncated attempt *as well as* the long one on every step. Stickiness
    here costs nothing on an endpoint that answers within its budget -- a ceiling is not a
    target -- which is why it is separated from the slow-endpoint verdict rather than
    dropped with it."""

    _thinking_off: bool = field(default=False, repr=False)
    """Set only after a raised request on a thinking endpoint is STILL truncated: then,
    and only then, ask the server to skip thinking (`enable_thinking=false`). Kept
    separate from raising the budget because on a vLLM server that honours the flag,
    turning thinking off is the part that can cut the model's capability -- the thing a
    big reasoning model is smart for -- so it is the last resort, not the first response
    to seeing a reasoning field. Never sent to an endpoint that showed no sign of
    thinking: there is nothing there to turn off, and the request buys another full-length
    generation for nothing."""

    _thinking_unsupported: bool = field(default=False, repr=False)
    """Set once, for the rest of the run, when the endpoint answered a body carrying
    `chat_template_kwargs` with a shape error: the key is not a dialect this server
    knows. Asking again buys the same refusal on every later call, so the switch is
    taken off the escalation for good and truncation stops one rung earlier."""

    _output_cap: int = field(default=limits.LLM_THINKING_MAX_OUTPUT_TOKENS, repr=False)
    """Upper bound on the raised ceiling, lowered only when the server answers a raised
    request with a 400 about context length: `prompt + max_tokens > num_ctx` on a strict
    server (vLLM). Never raised."""

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
        tokens = max_output_tokens
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": tokens,
            "temperature": 0,
        }
        if self._output_raised or self._thinking_endpoint:
            # Room for the thinking span plus the answer: the caller's ceiling times a
            # factor, capped, so a deliberately low ceiling (macro's runaway guard) is
            # raised in proportion rather than flattened to 8192.
            #
            # Two ways in, and they mean different things: `_thinking_endpoint` says the
            # completion has an invisible half to pay for, `_output_raised` only says a
            # previous answer did not fit. Both need the same room.
            raised = tokens * limits.LLM_THINKING_TOKEN_MULTIPLIER
            body["max_tokens"] = min(self._output_cap, max(tokens, raised))
        if self._thinking_off:
            # Last resort, only after a raised request still truncated: ask the server to
            # skip thinking. vLLM/llama.cpp honour it; ollama ignores the unknown key.
            body["chat_template_kwargs"] = {"enable_thinking": False}
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
        """Pull the arguments out of the *first* tool call, across the dialects.

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

        Calls after the first are read by `_read_extra_calls`, not here: this one
        answers "what is the action of this reply", and the answer is the same whether
        the model sent one call or six.
        """
        calls = _tool_call_entries(message)
        if not calls:
            return None
        return _call_payload(calls[0])

    def _read_extra_calls(
        self, message: dict[str, Any], schema: dict[str, object] | None
    ) -> tuple[str, ...]:
        """Every further call of our function in the same reply, validated, in order.

        A reply with several calls used to be one action plus a trace note. On a fast
        reasoning endpoint that is the dominant reply shape and the note was the only
        thing the dropped calls produced: the model asked for the same batch again on
        the next step, and the step cap -- not the clock -- ended the run with nothing
        written. So the calls are carried out of the client and `agent.loop` decides,
        under its own one-action-per-step rule, which of them may run.

        The first call is the reply's action and is returned through `text`, so only
        `calls[1:]` are collected here, and only when that first call is the one the
        caller actually used: a reply whose action was salvaged from `content`, or one
        read on a lower rung, has nothing to add and returns nothing.

        A malformed entry among the extras is dropped, never a failure. The reply
        already carries a usable action; refusing it over a bad third call would spend
        a rung and a step on something the loop would simply not have run.
        """
        if schema is None or self.style != NATIVE_TOOLS:
            return ()
        calls = _tool_call_entries(message)
        if len(calls) < 2 or _call_payload(calls[0]) is None:
            return ()

        kept: list[str] = []
        dropped = 0
        for entry in calls[1:]:
            payload = _call_payload(entry)
            action = None if payload is None else _normalise_action(payload, dict(schema))
            if action is None:
                dropped += 1
                continue
            kept.append(action)

        # The note the single-action protocol used to make, now saying what was done
        # with the calls rather than only how many arrived.
        self._note("protocol", f"{len(calls)} tool calls returned, keeping {1 + len(kept)}")
        if dropped:
            self._note("protocol", f"{dropped} of the extra tool calls were malformed and dropped")
        return tuple(kept)

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

        # The hard ceiling depends on what the endpoint has already shown itself to be.
        # One reply of a reasoning model measured 110 s of generation on the stand that
        # stands in for the evaluator, so capping it at `LLM_TIMEOUT_S` would throw away a
        # reply the server is still producing; a fast endpoint keeps the old number.
        ceiling = limits.LLM_THINKING_TIMEOUT_S if self._thinking_endpoint else limits.LLM_TIMEOUT_S
        budget_s = ceiling if timeout_s is None else min(ceiling, timeout_s)
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
        # Usage summed across every attempt, not just the one that finally parses: a
        # demoted or truncated attempt was still generated and billed. `stamped` puts
        # the running total on whatever object is returned, reply or error.
        acc = [0, 0, True]  # input tokens, output tokens, usage_reported

        def stamped(obj: LlmReply | LlmError) -> LlmReply | LlmError:
            return replace(obj, input_tokens=acc[0], output_tokens=acc[1], usage_reported=acc[2])

        for attempt in range(limits.LLM_MAX_ATTEMPTS):
            remaining = budget_s if attempt == 0 else deadline - self.clock()
            if remaining <= 0:
                return stamped(last)

            body = self._build_body(messages, schema, max_output_tokens)
            try:
                status, raw = self.transport(
                    url, json.dumps(body).encode("utf-8"), headers, remaining
                )
            except Exception as exc:  # transport failure of any kind
                # No usage came back, but a client-side timeout can cut off a reply the
                # server already generated and billed. Mark the total a lower bound so
                # `main` prints it as such rather than as an exact 0.
                acc[2] = False
                timed_out = _is_timeout(exc)
                last = LlmError(reason=_transport_reason(exc), retryable=True, timed_out=timed_out)
                if timed_out and not self._thinking_endpoint and acc[1] == 0:
                    # The one signal available on the very first call of a run, and the
                    # one that cost the most: on the 30B stand every episode's first call
                    # was cut by the pre-checkpoint clamp and the second was then sent at
                    # the default ceiling and truncated mid-reasoning. A call that ran out
                    # of wall clock having been handed no payload at all is an endpoint
                    # that is still generating, so raise the ceiling now rather than pay
                    # a second truncated attempt to learn the same thing.
                    self._thinking_endpoint = True
                    self._note("thinking", "call timed out with no payload; treating the endpoint as slow")
                if not self._backoff(attempt, deadline):
                    return stamped(last)
                continue

            if status >= 400:
                # A 4xx/5xx body may carry a usage block, but whether the server bills a
                # rejected request is server-specific and unknown for the evaluator, so
                # it is not counted; the total is marked a lower bound instead.
                acc[2] = False
                detail = redact(raw.decode("utf-8", "replace"), self.settings.secrets)
                verdict = classify_status(status)
                last = LlmError(
                    reason=f"endpoint returned {status}: {detail[:ERROR_DETAIL_LIMIT]}",
                    retryable=verdict == RETRY,
                )

                # A strict server (vLLM) rejects the raised thinking budget with a 400
                # naming the context length: lower the ceiling and retry the same rung
                # rather than downgrade the protocol, which would not fix the size.
                if (
                    verdict == SHAPE
                    and (self._thinking_endpoint or self._output_raised)
                    and self._output_cap > limits.LLM_MAX_OUTPUT_TOKENS
                    and _looks_like_context_overflow(detail)
                ):
                    self._output_cap = max(limits.LLM_MAX_OUTPUT_TOKENS, self._output_cap // 2)
                    self._note("thinking", f"context rejected the budget; capping output at {self._output_cap}")
                    continue

                if verdict == SHAPE and self._thinking_off:
                    # `chat_template_kwargs` is the one key in this body that no
                    # OpenAI-compatible server promises to accept, and a gateway
                    # validating with `extra="forbid"` rejects it. The rung is not what
                    # was refused: drop the switch and repeat the same request. Without
                    # this the ladder is spent on a field that has nothing to do with the
                    # rung, and the key -- sticky -- then refuses every later call too.
                    self._thinking_off = False
                    self._thinking_unsupported = True
                    self._note("thinking", "endpoint refused enable_thinking; dropping the flag")
                    continue

                if verdict == FATAL:
                    # A rejected key, an unknown model or an unknown route. The ladder
                    # cannot fix any of them and retrying only spends the deadline.
                    return stamped(last)

                if verdict == RETRY:
                    # One retry at the current rung for a 5xx. A server error that
                    # survives it is more likely a deterministic parser failure than an
                    # outage, so the second response is to change the request shape, not
                    # repeat it.
                    #
                    # Back-pressure (408/409/425/429) is not that: it says nothing about
                    # the shape, as `classify_status` spells out, and it clears on its
                    # own. It gets the whole retry schedule on its own rung and never
                    # steps down -- a downgrade here would be sticky for the rest of the
                    # task and take the batched extra tool calls with it, paid for a
                    # busy queue. `Retry-After` is still not read: the schedule is
                    # (1.0, 3.0) s and the call deadline is what bounds the wait.
                    pressure = status in PRESSURE_STATUSES
                    allowance = len(limits.LLM_RETRY_BACKOFF_S) if pressure else 1
                    if retries_at_style < allowance and self._backoff(attempt, deadline):
                        retries_at_style += 1
                        continue
                    if not pressure and schema is not None and self._demote(f"persistent http {status}"):
                        retries_at_style = 0
                        continue
                    return stamped(last)

                if schema is not None and self._demote(f"http {status}"):
                    retries_at_style = 0
                    continue
                return stamped(last)

            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                # A reply we could not parse was still generated; its usage is unknown,
                # so the total is a lower bound.
                acc[2] = False
                last = LlmError(reason="endpoint reply was not json", retryable=True)
                if not self._backoff(attempt, deadline):
                    return stamped(last)
                continue

            # Counted here, before the reply is judged: a response that fails to parse or
            # gets demoted was generated and billed just the same, and on a thinking
            # model that is every rung. `stamped` then carries the running total out.
            prompt_tokens, completion_tokens, reported = _extract_usage(payload)
            acc[0] += prompt_tokens
            acc[1] += completion_tokens
            acc[2] = acc[2] and reported

            # Evidence of reasoning -- not a bare cutoff -- marks a thinking endpoint. Set
            # it stickily so every later call gets the raised ceiling and the longer
            # deadline; `was_raised` records whether the request that just came back
            # already had the raised ceiling, to tell a first truncation from one that
            # survived the raise.
            message = _first_message(payload)
            finish = _finish_reason(payload)
            was_raised = self._thinking_endpoint or self._output_raised
            evidence = _thinking_evidence(
                message,
                finish,
                requested_tokens=int(body.get("max_tokens", max_output_tokens)),
                completion_tokens=completion_tokens,
                usage_reported=reported,
            )
            if evidence and not self._thinking_endpoint:
                self._thinking_endpoint = True
                self._note("thinking", f"{evidence}; raising the output ceiling and the call deadline")

            text, reason = self._read_reply(payload, schema)
            if text is None:
                if finish == "length":
                    # Truncated, not a shape problem, so the ladder must not step down.
                    # Retry the same rung: first with the raised ceiling, then -- only on
                    # an endpoint that showed it thinks -- with thinking disabled, then
                    # give up as truncated. Each is one attempt, bounded by
                    # LLM_MAX_ATTEMPTS. `last` is set before the retry so that if the
                    # deadline expires between attempts the returned error names the
                    # truncation, not the placeholder "no attempt was made".
                    last = LlmError(
                        reason=f"reply truncated by length at style {self.style}",
                        retryable=False,
                        # Computed on every truncated attempt, not only the last one:
                        # whichever attempt ends up returned -- this one, or this one
                        # after the deadline expired between retries -- carries the
                        # verdict about the reply it actually describes.
                        truncated_reasoning=_runaway_reasoning(
                            message, evidence, was_raised=was_raised
                        ),
                    )
                    if not was_raised:
                        self._output_raised = True
                        # `budget` and not `thinking`: the answer needs more room, which
                        # is a statement about this reply's length and not a verdict on
                        # the endpoint. Keeping the two notes apart is what makes a trace
                        # readable when only one of them fired.
                        self._note("budget", "reply truncated by length; raising the output ceiling")
                        continue
                    if (
                        self._thinking_endpoint
                        and not self._thinking_off
                        and not self._thinking_unsupported
                    ):
                        self._thinking_off = True
                        self._note("thinking", "still truncated; asking the endpoint to skip thinking")
                        continue
                    # Nothing said this endpoint thinks, so there is no thinking to turn
                    # off and a third full-length generation would buy the same answer.
                    return stamped(last)
                last = LlmError(reason=f"{reason} at style {self.style}", retryable=False)
                if schema is not None and self._demote(reason):
                    retries_at_style = 0
                    continue
                # Not retried on purpose. Sampling is at temperature 0 and the request
                # would be byte-identical, so a repeat buys the same reply and pays
                # for it in tokens, which is the second ranking criterion.
                return stamped(last)

            return stamped(
                LlmReply(text=text, extra_actions=self._read_extra_calls(message, schema))
            )

        return stamped(last)

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
