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

    extra_actions: tuple[str, ...] = ()
    """Further actions the same reply asked for, as JSON text, in the order sent.

    `text` is always the first call and is unaffected by this field, so every consumer
    that ignores it behaves exactly as before -- that is why it is last and defaulted.

    It exists because the reply is not always one action. A reasoning model answers a
    code-fix step with three to six `read_range` calls at once, and a client that keeps
    only the first makes the model re-request the whole batch on the next step: 49 of
    51 calls in one measured episode were reads, no edit was ever made and the step cap
    ended the run. The calls are carried, not executed -- whether any of them may run,
    and under what contract, is `agent.loop`'s decision and nothing here assumes it.
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
    timed_out: bool = False
    """Set by the client only when *our* transport deadline expired.

    The loop classifies a failed call as slow-or-broken on this flag and nothing else:
    the reason text can quote the endpoint's own response body, and a 504 or a 400 that
    happens to say "timeout" must not read as our clock running out.
    """


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
