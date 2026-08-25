"""Single source of every hard number the agent obeys.

Rule borrowed from the ozon hackathon (`src/constants.py`): a bound that is written
down in two places is a defect, because the two copies drift and the diverged one is
the one that fires in production. Everything that limits time, size, steps or retries
lives here and is imported, never re-typed.

None of these values is measured yet. The task statement says every closed task has
its own time and token limit but never gives the numbers, so these are conservative
placeholders that must survive the first target-image baseline before they become
decisions. Marked TBD in `docs/architecture.md`; do not quote them as measured.
"""

from __future__ import annotations

# --- Wall clock -------------------------------------------------------------

DEFAULT_TASK_TIMEOUT_S = 120.0
"""Wall clock we assume for one task. The runner never tells us.

Measured from the public task configs: `[agent].timeout_sec` is 120 for the file
tasks and 600 for the audit, fix and forensics tasks. Harbor applies it with
`asyncio.wait_for` around the agent call and passes it to the agent neither as an
argument nor as an environment variable.

The smaller value is chosen deliberately. Assuming 600 on a 120-second task means
being killed mid-work with nothing written, which is a zero; assuming 120 on a
600-second task means stopping while time remains, which keeps whatever was already
produced. The asymmetry only holds if the loop writes its deliverable early and then
improves it -- that is a requirement on the loop, not a nice-to-have.
"""

TIMEOUT_ENV_NAMES: tuple[str, ...] = ("AGENT_TIMEOUT_S",)
"""Environment variables we read for the task deadline.

Only our own name, used by our harness and tests. Confirmed 25.08.2026 against Harbor
0.16.1 and the competition wrapper: the runner passes the timeout to no one. There is
nothing else to read.
"""

VERIFY_RESERVE_FRACTION = 0.20
"""Share of the task budget kept for verification and a clean finish."""

VERIFY_RESERVE_MIN_S = 30.0
VERIFY_RESERVE_MAX_S = 120.0
"""Clamp for the reserve. A short task still needs a verifier; a long one does not
need to hand it a quarter of an hour."""

# --- Loop -------------------------------------------------------------------

MAX_STEPS = 30
"""Hard cap on act/observe iterations. The loop counts them, the model cannot."""

MAX_RECOVERY_PASSES = 1
"""Directed retries after a red verifier, borrowed as a shape from the bounded
fact-check loop in the ai-v-massy pipeline (`factcheck_retries <= 2`)."""

# --- Tools ------------------------------------------------------------------

TOOL_TIMEOUT_MAX_S = 120.0
"""Ceiling for one shell command. The effective timeout is also bounded by the time
left before the verifier reserve, whichever is smaller."""

TOOL_OUTPUT_HEAD_BYTES = 4000
TOOL_OUTPUT_TAIL_BYTES = 2000
"""How much of a tool's output reaches the model. Head and tail are kept because the
command and its failure usually live at opposite ends. Truncation is always marked."""

# --- LLM --------------------------------------------------------------------

LLM_TIMEOUT_S = 120.0
LLM_MAX_OUTPUT_TOKENS = 2048
LLM_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
"""Finite retry schedule for transport failures. A permanently broken endpoint must
fail diagnosably rather than hang until the task times out."""

LLM_MAX_ATTEMPTS = 6
"""Total requests one logical call may cost, retries and protocol fallbacks included.

Six because the worst legitimate path is one retry plus three rungs of fallback, and
a cap that cuts the ladder short would look like an unsupported endpoint."""

DEFAULT_CALL_STYLE = "native_tools"
"""Which request shape we start from. TBD: set from the target-model baseline.

Deliberately a constant and not a run-time probe. The evaluated model is the same for
all fifteen tasks, so probing on every run would pay for the same answer fifteen
times in tokens, which is the second ranking criterion. Lazy fallback in `agent.llm`
covers the case where this constant is wrong.
"""

# --- Where the task lives ----------------------------------------------------

WORKSPACE_ENV_NAME = "LOCAL_AGENT_WORKDIR"
"""Convention from the sample entrypoint, which exports it for its child process."""

IMAGE_WORKDIR = "/app"
"""`WORKDIR` of `secureintelligent/acp`, and where every public task keeps its files.

Not a guess and not a path from anyone's laptop: read from the image config, and every
public task Dockerfile is `FROM secureintelligent/acp:latest` with `WORKDIR /app`.
Used only as a candidate; `agent.main.resolve_workspace` falls back when it is absent.
"""


# --- Packaging ---------------------------------------------------------------

MAX_SUBMISSION_BYTES = 10 * 1024 * 1024
"""Hard archive limit from the competition contract."""


MODEL_ENV_NAME = "LOCAL_AGENT_MODEL"
BASE_URL_ENV_NAME = "OPENAI_BASE_URL"
API_KEY_ENV_NAME = "OPENAI_API_KEY"
"""Names fixed by the competition contract. Values are never logged or persisted."""
