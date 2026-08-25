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
    api_key: str
    secrets: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LlmSettings:
        return cls(
            model=env.get(limits.MODEL_ENV_NAME, "").strip(),
            base_url=env.get(limits.BASE_URL_ENV_NAME, "").strip().rstrip("/"),
            api_key=env.get(limits.API_KEY_ENV_NAME, "").strip(),
            secrets=secrets_from_env(env),
        )

    def missing(self) -> tuple[str, ...]:
        """Which required variables are empty. Checked before the first call."""
        absent = []
        if not self.model:
            absent.append(limits.MODEL_ENV_NAME)
        if not self.base_url:
            absent.append(limits.BASE_URL_ENV_NAME)
        if not self.api_key:
            absent.append(limits.API_KEY_ENV_NAME)
        return tuple(absent)

    def sanitized_summary(self) -> dict[str, Any]:
        """Everything about the endpoint that is safe to write down."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
        }


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
            body["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
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

    def _read_reply(
        self, payload: dict[str, Any], schema: dict[str, object] | None
    ) -> tuple[str, bool] | None:
        """Return (text, ok). None means the reply is unusable at this style."""
        message = _first_message(payload)
        content = message.get("content")
        content = content if isinstance(content, str) else ""

        if schema is None:
            return (content, True) if content else None

        if self.style == NATIVE_TOOLS:
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                return None
            arguments = calls[0].get("function", {}).get("arguments")
            if not isinstance(arguments, str):
                return None
            content = arguments

        if not content:
            return None
        if self.style == TEXT:
            salvaged = _salvage_json(content)
            if salvaged is None:
                return None
            content = salvaged
        try:
            json.loads(content)
        except json.JSONDecodeError:
            return None
        return content, True

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

        url = f"{self.settings.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.api_key}",
        }

        last = LlmError(reason="no attempt was made", retryable=False)
        for attempt in range(limits.LLM_MAX_ATTEMPTS):
            body = self._build_body(messages, schema, max_output_tokens)
            try:
                status, raw = self.transport(
                    url, json.dumps(body).encode("utf-8"), headers, budget_s
                )
            except Exception as exc:  # transport failure of any kind
                last = LlmError(reason=f"transport failed: {type(exc).__name__}", retryable=True)
                if not self._backoff(attempt):
                    return last
                continue

            if status >= 500:
                last = LlmError(reason=f"endpoint returned {status}", retryable=True)
                if not self._backoff(attempt):
                    return last
                continue

            if status >= 400:
                detail = redact(raw.decode("utf-8", "replace"), self.settings.secrets)
                last = LlmError(
                    reason=f"endpoint returned {status}: {detail[:ERROR_DETAIL_LIMIT]}",
                    retryable=False,
                )
                # A rejected request shape is exactly what the ladder is for; a
                # rejected key or model is not, and must not be retried forever.
                if schema is not None and self._demote(f"http {status}"):
                    continue
                return last

            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                last = LlmError(reason="endpoint reply was not json", retryable=True)
                if not self._backoff(attempt):
                    return last
                continue

            read = self._read_reply(payload, schema)
            if read is None:
                last = LlmError(reason=f"unusable reply at style {self.style}", retryable=False)
                if schema is not None and self._demote("unparseable reply"):
                    continue
                # Not retried on purpose. Sampling is at temperature 0 and the request
                # would be byte-identical, so a repeat buys the same reply and pays
                # for it in tokens, which is the second ranking criterion.
                return last

            text, _ = read
            prompt_tokens, completion_tokens, reported = _extract_usage(payload)
            return LlmReply(
                text=text,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                usage_reported=reported,
            )

        return last

    def _backoff(self, attempt: int) -> bool:
        """Wait before the next attempt. False when the schedule is exhausted."""
        if attempt >= len(limits.LLM_RETRY_BACKOFF_S):
            return False
        delay = limits.LLM_RETRY_BACKOFF_S[attempt]
        self._note("retry", f"attempt {attempt + 1} failed, waiting {delay}s")
        self.sleep(delay)
        return True
