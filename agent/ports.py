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
    """A call that produced nothing usable: transport, protocol or parse failure."""

    reason: str
    retryable: bool


@runtime_checkable
class LlmPort(Protocol):
    """The only way the loop is allowed to reach a model."""

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
    ) -> LlmReply | LlmError: ...
