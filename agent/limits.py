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

DEFAULT_TASK_TIMEOUT_S = 600.0
"""Assumed wall clock for one task when the runner tells us nothing. TBD."""

TIMEOUT_ENV_NAMES: tuple[str, ...] = ("AGENT_TIMEOUT_S",)
"""Environment variables we read for the task deadline.

Only our own name is listed. How the official runner communicates per-task limits is
an open question in `PLAN.md`; guessing organiser variable names here would put an
invented contract into production code.
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
LLM_MAX_RETRIES = 2
LLM_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
"""Finite retry schedule. A permanently broken endpoint must fail diagnosably rather
than hang until the task times out."""

MODEL_ENV_NAME = "LOCAL_AGENT_MODEL"
BASE_URL_ENV_NAME = "OPENAI_BASE_URL"
API_KEY_ENV_NAME = "OPENAI_API_KEY"
"""Names fixed by the competition contract. Values are never logged or persisted."""
