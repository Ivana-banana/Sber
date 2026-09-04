"""A model that replays a recorded transcript instead of calling anything.

Borrowed from the frozen adapter in ai-v-massy (`src/adapters/search/frozen.py`,
`eval/run_frozen.py`). Ranking puts total tokens second, so every local regression
run that hits a live endpoint costs us on a criterion we are trying to win, and adds
sampling noise on top. Replaying a transcript makes the loop, the budget, the parsers
and the verifier deterministic and free.

This module is test-only and must not end up inside the submission archive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent.ports import LlmError, LlmReply, Message


@dataclass
class StubLlm:
    """Hands back queued replies in order and records what it was asked."""

    replies: list[LlmReply | LlmError] = field(default_factory=list)
    calls: list[list[Message]] = field(default_factory=list)
    schemas: list[dict[str, object]] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)
    """The ceiling each call was given. Recorded because the caller owns the deadline,
    and a controller that hands a call less time than the endpoint needs fails in a way
    no assertion about its output would catch -- E041 spent a whole pilot proving it."""

    _next: int = 0

    @classmethod
    def from_transcript(cls, path: str | Path) -> StubLlm:
        """Load replies from a recorded JSON transcript.

        Format: a list of objects. `{"text": ..., "input_tokens": ..., ...}` becomes
        an `LlmReply`; `{"error": ..., "retryable": ...}` becomes an `LlmError`, so a
        recorded failure can be replayed as faithfully as a recorded success.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        replies: list[LlmReply | LlmError] = []
        for item in raw:
            if "error" in item:
                replies.append(
                    LlmError(reason=item["error"], retryable=bool(item.get("retryable", False)))
                )
            else:
                replies.append(
                    LlmReply(
                        text=item["text"],
                        input_tokens=int(item.get("input_tokens", 0)),
                        output_tokens=int(item.get("output_tokens", 0)),
                        usage_reported=bool(item.get("usage_reported", True)),
                    )
                )
        return cls(replies=replies)

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        self.timeouts.append(timeout_s)
        return self._next_reply(messages)

    def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        # The transcript is shared between both methods: a recording is a sequence of
        # what the model said, not two independent streams.
        self.schemas.append(schema)
        self.timeouts.append(timeout_s)
        return self._next_reply(messages)

    def _next_reply(self, messages: list[Message]) -> LlmReply | LlmError:
        self.calls.append(list(messages))
        if self._next >= len(self.replies):
            # An exhausted transcript means the loop took a path the recording does
            # not cover. Surfacing it as an error keeps that visible instead of
            # letting the test pass on a silently repeated last reply.
            return LlmError(reason="transcript exhausted", retryable=False)
        reply = self.replies[self._next]
        self._next += 1
        return reply
