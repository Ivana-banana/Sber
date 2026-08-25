"""Single source of every hard number the agent obeys.

Rule borrowed from the ozon hackathon (`src/constants.py`): a bound that is written
down in two places is a defect, because the two copies drift and the diverged one is
the one that fires in production. Everything that limits time, size, steps or retries
lives here and is imported, never re-typed.

Some of these are now measurements and say so in their own docstring: the task
timeouts, the image workdir, the archive limit and the starting call style all cite
what they were read from. The rest -- step caps, output sizes, retry counts -- remain
conservative placeholders that must survive the first target-image baseline before
they become decisions. Do not quote a placeholder as if it were measured.
"""

from __future__ import annotations

# --- Wall clock -------------------------------------------------------------

DEFAULT_TASK_TIMEOUT_S = 600.0
"""Outer bound on one task. The runner never tells us the real one.

Measured from the public task configs: `[agent].timeout_sec` is 120 for the file
tasks and 600 for the audit, fix and forensics tasks. Harbor applies it with
`asyncio.wait_for` around the agent call and passes it to the agent neither as an
argument nor as an environment variable.

Picking one of the two numbers is a false choice, and both options lose: assume 600
on a 120-second task and we are killed mid-work; assume 120 on a 600-second task and
we stop while five minutes of budget remain. So this constant is not the stop
condition at all. It is the outer bound on how long we may keep *improving* an
answer, while `EARLY_DELIVERABLE_DEADLINE_S` fixes when an answer must first exist.
Being killed then costs the improvement, never the deliverable.
"""

EARLY_DELIVERABLE_DEADLINE_S = 90.0
"""When a best-effort answer must already be written to disk.

Sits below the smallest observed task limit (120 s) with margin for the runner's own
startup. Past this point the loop stops exploring and produces something checkable,
whatever it has. A task that turns out to be a 600-second one simply continues
afterwards and refines what is already there.
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

SHELL_EXECUTABLE = "/bin/sh"
"""What `run_shell` execs. `sh` and not `bash`: the pipelines and redirections a
playbook needs are POSIX, and `sh` is present in every image we might be handed."""

READ_MAX_BYTES = 8000
READ_MAX_LINES = 200
"""One `read_range` window. Deliberately smaller than the shell output budget: a file
is re-readable at another offset, so a wide window buys nothing and costs context on
every step."""

LIST_MAX_ENTRIES = 100
"""Entries one directory listing may show before it says how many it hid."""

SEARCH_MAX_MATCHES = 50
"""Matches one search returns. A pattern with thousands of hits is the wrong pattern,
and the fix is a narrower query, not a longer answer."""

BINARY_SNIFF_BYTES = 4096
"""Prefix examined to decide text vs binary. A NUL byte in it means binary, which is
the rule `grep` uses and it is right often enough for triage."""

# --- Preflight ---------------------------------------------------------------

BRIEF_MAX_CHARS = 1500
BRIEF_MAX_ENTRIES = 30
BRIEF_MAX_DEPTH = 2
"""Size of the environment map handed to the model.

Paid on every call, so it is a map and not an inventory: the top of the tree, the
interpreter and the utilities that exist. Anything deeper is a search away, and a
search the model asks for is cheaper than a listing carried on every call."""

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
"""Which request shape we start from.

Not a guess. The organisers' own reference agent builds a pydantic-ai
`OpenAIChatModel` and registers `bash`, `read_file`, `apply_diff` and `append_file`
with `@agent.tool`, which is native function calling with no text fallback anywhere
in it. An evaluation endpoint that did not support native tool calls would score
their own baseline zero on every task, so the endpoint supports them.

Deliberately a constant and not a run-time probe: the evaluated model is the same for
all fifteen tasks, so probing on every run would pay for the same answer fifteen
times in tokens, which is the second ranking criterion. Lazy fallback in `agent.llm`
covers the case where this reasoning is wrong.
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
