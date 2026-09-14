"""Boundary contracts between the loop and everything replaceable behind it.

Shape borrowed from the ai-v-massy pipeline (`src/ports/__init__.py`): the loop
depends on protocols, adapters are injected. Two consequences we actually need:

* the whole loop runs against a recorded transcript, so local regressions cost no
  tokens and no network;
* a real client can be swapped for a degraded one (native tool calls -> JSON ->
  plain text) without the loop knowing.

One rule is copied verbatim from that project and matters more than the rest:
**a failed call is a value, not an exception.** A small model returns malformed
output routinely; if that raises, one bad reply ends the task at zero. `LlmError`
travels back into the loop, which decides whether to retry, degrade or stop.

Standard library only. `agent/` ships inside the submission archive and the target
microVM installs nothing at run time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

Message = dict[str, str]
"""One OpenAI-compatible chat message: `{"role": ..., "content": ...}`."""


@dataclass(frozen=True)
class LlmReply:
    """A usable answer from the endpoint."""

    text: str

    input_tokens: int = 0
    output_tokens: int = 0

    usage_reported: bool = True
    """Whether the endpoint actually returned a usage block.

    False means we do not know the cost of this call. Token count is the second
    ranking criterion, so an invented estimate would be a fabricated metric; the
    flag propagates to telemetry instead.
    """


@dataclass(frozen=True)
class LlmError:
    """A call that produced nothing usable: transport, protocol or parse failure.

    It still carries token usage: a reply that failed to parse (empty content, a schema
    miss, a thinking model whose reasoning filled the completion) was generated and
    billed. On the closed set the tie-break counts every input and output token of every
    request, so tokens spent on demoted or truncated attempts have to be counted here or
    our total is understated against the organizer's server-side counter.
    """

    reason: str
    retryable: bool

    input_tokens: int = 0
    output_tokens: int = 0
    usage_reported: bool = True


@runtime_checkable
class LlmPort(Protocol):
    """The only way the loop is allowed to reach a model.

    Two methods rather than one, same split as the datacon extraction client: free
    text and a call whose reply is guaranteed to parse as JSON. They fail
    differently, so collapsing them would push protocol handling into the loop.

    `timeout_s` is passed in because the caller owns the deadline. A call allowed to
    run longer than the time left is how a run turns into a timeout zero.
    """

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError: ...

    def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError: ...
