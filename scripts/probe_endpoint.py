"""Measure which request shapes the target endpoint actually supports.

    python scripts/probe_endpoint.py

This is a development tool, not part of the agent. It exists so that
`limits.DEFAULT_CALL_STYLE` is a recorded measurement rather than a guess, and so
that the answer is paid for once instead of once per evaluated task.

Run it against the model used for evaluation, put the result in
`docs/experiments.md` together with the model name, then set the constant.

Each rung costs one tiny call. Nothing here is imported by `agent/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import llm  # noqa: E402
from agent.ports import LlmError, LlmReply  # noqa: E402

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

PROMPT = [{"role": "user", "content": "Return the word ok as the answer field."}]


def probe_one(settings: llm.LlmSettings, style: str, timeout_s: float) -> tuple[bool, str]:
    """Try exactly one style. Fallback is off: we want the truth about this rung.

    A `native_tools` answer can arrive two ways: in `tool_calls`, which is the rung
    working, or in `content`, which is the model writing a call the server did not
    parse. Both give a usable reply, so both read as PASS unless the events are
    watched -- and the whole point of the probe is to tell them apart.
    """
    events: list[str] = []
    client = llm.HttpLlm(
        settings=settings,
        style=style,
        allow_fallback=False,
        on_event=lambda kind, detail: events.append(detail),
    )
    result = client.complete_structured(
        PROMPT, schema=SCHEMA, max_output_tokens=64, timeout_s=timeout_s
    )
    if isinstance(result, LlmReply):
        detail = f"ok, {result.input_tokens}+{result.output_tokens} tokens"
        if any("arrived in content" in event for event in events):
            detail += "; call arrived in content, not tool_calls"
        return True, detail
    if isinstance(result, LlmError):
        return False, result.reason
    return False, "unknown result type"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    settings = llm.LlmSettings.from_env(os.environ)
    missing = settings.missing()
    if missing:
        print(f"not configured: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not settings.api_key:
        # Not an error: local servers are keyless unless started with --api-key.
        print("note: no api key set, calling without an Authorization header")

    print(json.dumps(settings.sanitized_summary(), indent=2))

    supported: list[tuple[str, str]] = []
    for style in llm.LADDER:
        ok, detail = probe_one(settings, style, args.timeout)
        print(f"{'PASS' if ok else 'FAIL'}  {style:<13} {detail}")
        if ok:
            supported.append((style, detail))

    if not supported:
        print("\nno request shape worked; the endpoint or the model is the problem")
        return 1

    best, detail = supported[0]
    print(f"\nset limits.DEFAULT_CALL_STYLE = {best!r}")
    if "arrived in content" in detail:
        print(
            "but this rung passed on a call salvaged from content: the server offered "
            "tools and did not parse the answer as one. The agent demotes on that reply."
        )
    print("record this run in docs/experiments.md with the model name before relying on it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
