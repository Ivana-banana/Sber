"""Runtime adapter: the boundary between the runner's shell call and the agent.

Its whole job is to turn `./run.sh "<task instruction>"` into a state object with a
deadline, and to turn whatever comes back into an exit code. It holds no task logic;
the loop plugs into the marked seam below once it exists.

Kept startable on purpose. This module is the first thing that runs inside the target
microVM, so it must not import anything optional, must not need a reachable endpoint
to report its own status, and must not raise on a malformed argument list.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent.llm import LlmSettings
from agent.state import AgentState, Budget

EXIT_OK = 0
EXIT_USAGE = 2

FINGERPRINT_CHARS = 12


def fingerprint(instruction: str) -> str:
    """A short digest of the task text, safe to print.

    The entrypoint has to carry arbitrary text through a shell: quotes, newlines,
    backslashes, leading dashes. A quoting bug there is invisible until a real
    instruction contains one, and by then it is a zero. Printing a digest lets the
    packaging preflight prove the round trip without ever echoing task content, which
    is untrusted data and may be large.
    """
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def build_state(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> AgentState:
    """Assemble the run's state. Separate from `main` so tests do not need a shell."""
    return AgentState(
        instruction=argv[1],
        workspace=cwd,
        budget=Budget.from_env(env),
    )


def main(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print('usage: run.sh "<task instruction>"', file=sys.stderr)
        return EXIT_USAGE

    state = build_state(argv, env, cwd)
    settings = LlmSettings.from_env(env)
    missing = settings.missing()

    # Status only, and deliberately terse: this line exists so a packaging smoke can
    # tell "started and understood its inputs" from "started". It carries no secrets
    # and no task content.
    print(
        f"agent ready: workspace={state.workspace} "
        f"instruction={len(state.instruction)}:{fingerprint(state.instruction)} "
        f"budget={state.budget.total_s:.0f}s "
        f"reserve={state.budget.verify_reserve_s:.0f}s "
        f"steps={state.budget.max_steps} "
        f"endpoint={'configured' if not missing else 'missing:' + ','.join(missing)}"
    )

    # --- seam: the inspect/act/verify loop lands here -----------------------
    # Until it does, the exit code says "the contract works", not "the task is
    # solved". Nothing here may ever report success on the strength of having run.

    return EXIT_OK


if __name__ == "__main__":
    import os

    raise SystemExit(main(sys.argv, os.environ, Path.cwd()))
