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

EARLY_DELIVERABLE_STEP_FRACTION = 0.4
"""The same checkpoint expressed in steps, for endpoints too fast to reach it in time.

`EARLY_DELIVERABLE_DEADLINE_S` was measured when one call cost 10-40 s, so eight steps
carried a run past it. On the GPU stand a call costs 4-8 s and the whole step cap fits
in forty seconds: E052's seven empty-handed zeros died at steps 6-10, between 35 and
70 s, and never saw the checkpoint at all. Whichever bound arrives first is the point
where exploring stops and producing starts.

0.4 of `MAX_STEPS` is step 12 of 30 -- past the 6-10 where those runs died, and leaving
eighteen steps to write and refine.
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

OBSERVATION_BUDGET_CHARS = 8000
"""How much observation history travels back to the model, in characters.

Was a window of the last eight observations, which was cheap to reason about and
expensive to run. A window slides: on the ninth step the oldest observation leaves and
the text right after the system block changes, so the server re-reads the whole prompt
at full price. Measured on this stand, steps 2-8 cost 7-9 s a call and step 9 costs
22-24 s, in two runs independently (`docs/research/observation-window-cost.md`).

A budget does not slide. While the kept history fits, every request is a strict
extension of the one before it and the server pays for the new tail alone. When it no
longer fits, the history is re-laid once -- one full prefill, then extensions again.

Eight thousand characters is about two thousand tokens, which leaves room beside the
thousand-token system block inside a 4096-token context. A single observation may be
6000 bytes (`TOOL_OUTPUT_HEAD_BYTES` plus `TOOL_OUTPUT_TAIL_BYTES`), so the budget is
not a guarantee that two large observations both fit; it is a bound on the ordinary
case, and `OBSERVATION_RELAY_KEEP` is what holds the exceptional one.
"""

OBSERVATION_RELAY_KEEP = 4
"""How many observations survive a re-lay.

Four and not one because the verifier reserve can need the error from several steps
back; four and not more because a re-lay that keeps most of the history spends its
prefill again almost immediately.
"""

MAX_CONSECUTIVE_LLM_FAILURES = 3
"""Logical calls that may fail in a row before the endpoint is declared unusable.

Three and not one because a single transport failure is ordinary; three and not more
because `agent.llm` already retries inside one logical call, so this is three times
that ladder, not three requests. A run that cannot reach a model has no path to a
deliverable and should say so while there is still time to leave what it has.
"""

MAX_RECOVERY_PASSES = 1
"""Directed retries after a red verifier, borrowed as a shape from the bounded
fact-check loop in the ai-v-massy pipeline (`factcheck_retries <= 2`)."""

ACTION_HISTORY = 6
"""How many recent actions the loop remembers for its stuck detectors.

Six because the widest pattern below (`ALTERNATING_STOP`) needs six to be visible;
a window that cannot hold the pattern it looks for is decoration.
"""

REPEAT_FAILURE_BLOCK = 2
"""Identical failed calls after which the same call is refused before it runs.

Starting value, not a measurement. The action is refused rather than the run stopped:
a model that repeats one broken command has usually misread an error, and the cheapest
correction is telling it so while the budget is still there.
"""

EXACT_REPEAT_STOP = 4
ALTERNATING_STOP = 6
"""Defaults read from the OpenHands agent SDK stuck detector at revision `041078f`:
four identical action/observation pairs, six steps of an A-B-A-B cycle. Checked
against their source rather than quoted from prose.

Err toward these firing less often, not more, until measured: Lu et al. (2025)
found extrinsic early-exit on every step collapsed success rate on an 8B model
(11.7% -> 1.7% on PDDL) while cutting redundant steps, and the evaluation model's
size is unknown. See `docs/research/verdict-budget-policy.md`.
"""

NO_PROGRESS_STOP = 4
"""Consecutive actions that produce no observation the run has not already seen.

Conservative carry-over of the OpenHands window, not a measurement of our own loop.
An action counts as progress when it yields a new result fingerprint or leaves a new
artifact; anything else is the loop paying tokens to stand still.

Same small-model caution as `ALTERNATING_STOP` above applies here.
"""

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

BRIEF_MAX_CHARS = 2200
BRIEF_MAX_ENTRIES = 30
BRIEF_MAX_DEPTH = 2
"""Size of the environment map handed to the model.

Paid on every call, so it is a map and not an inventory: the top of the tree, the
interpreter and the utilities that exist. Anything deeper is a search away, and a
search the model asks for is cheaper than a listing carried on every call.

The ceiling was 1500 until the recon packet below was added. It moved because the two
new sections are bounded but not free, and a ceiling that clips them would have made
the sections silently useless on exactly the crowded workspaces they help most. The
tree is rendered first, so what a cut still costs is the tail, never the map."""

BRIEF_MAX_PORTS = 8
"""Listening TCP sockets named in the brief, from `/proc/net/tcp` and `tcp6`.

The E060 artifact review found `ctf-duplicate-parameter` and `ctf-signed-cookie` each
writing the exactly correct flag and still scoring zero, because the checker wants the
proof *issued* by the service; the trace shows four consecutive attempts to start a
server that was already listening. That is one read of a procfs file, spent once,
against a wrong plan the model pays for on every step it survives.

Eight because a task image runs a handful of services and a machine with dozens of
them is not the case this line was written for. `ss` and `nmap` do not exist in the
target image (`docs/research/image-substitutions.md`), which is why this is parsed
from procfs rather than shelled out."""

BRIEF_MAX_LITERALS = 8
BRIEF_LITERAL_MAX_CHARS = 48
BRIEF_LITERAL_MAX_HITS = 2
"""The backtick-quoted strings from the task text, and where they live in the tree.

`mut-audit-query-gate` says to "include the actual `login_name` data flow in `proof`"
and its checker requires exactly that string; `audit-path-normalization` names the
component, the report path and both category names the same way. Every one of those is
a `grep -rn` the model would otherwise buy a step at a time, and the answer does not
change between steps.

Eight literals and two hits each, because this is orientation and not a search result:
the point is to say which file to open, and the loop has a real `search` tool for the
rest. A literal is skipped below three characters -- `id` matches everything -- and
above `BRIEF_LITERAL_MAX_CHARS`, where it is prose rather than a name."""

BRIEF_SCAN_MAX_FILES = 400
BRIEF_SCAN_MAX_BYTES = 65536
"""Bound on the literal scan: files visited, and bytes read from each.

Preflight runs before the first token and must not become a reason the run starts
late. Both numbers are ceilings on a walk that normally ends far sooner, and a file
whose first `BINARY_SNIFF_BYTES` contain a NUL is skipped unread -- a capture or a
core dump contains any short string by accident."""

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

Briefly changed to `json_schema` on 04.09 and changed back the same hour. The measured
defect was real -- the rung failed 36 times out of 36 and cost every task a second
request -- but the cure was wrong: `LADDER` only ever moves down, so starting one rung
lower makes `native_tools` unreachable for the whole run. On an endpoint that accepts
`tools` and rejects `response_format` -- the configuration the organisers' baseline
actually documents -- the agent would spend three requests falling to `text` and never
try the one shape that works. The defect is fixed where it belongs, in
`agent.llm._unwrap_function_envelope`, which reads the reply this model really sends.
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


# --- Macro controller --------------------------------------------------------

CONTROLLER_ENV_NAME = "AGENT_CONTROLLER"
DEFAULT_CONTROLLER = "loop"
"""Which controller `agent.main` drives the task with, and what it picks unaided.

An environment switch and not a build flag, because the comparison it exists for is
paired: control and treatment then run from one archive with one sha256, and the only
thing that differs between the two arms is this variable. A second archive would have
been a second factor -- different bytes, different packaging, a different unpack -- in
a run whose whole purpose is to isolate the first.

Default `loop` so that an archive handed to the runner with no variable set behaves
exactly as every archive before it has. Treatment opts in; nothing opts out.
"""

MACRO_TOTAL_S = 170.0
"""Outer bound the macro controller sets for itself when it has no better number.

Was 85, then 90, and both numbers came from a rule that does not apply here. The loop's
rule is "produce early, be killed late": it has a deliverable on disk long before the
deadline, so stopping under the shortest observed kill (95 s) costs it a refinement and
never the answer. This controller has *nothing* on disk until its program runs, and its
program cannot run until the call returns.

That makes the loss asymmetric in the other direction, and E041/E043 measured both
sides of it. Against a 95 s kill: stopping at 73 s reports `llm-unusable` and scores 0,
being killed at 95 s scores 0 -- identical. Against the 180 s kill the same stand
applies to the audit, fix and forensics tasks: stopping at 73 s scores 0, while a call
allowed to finish has a real attempt behind it. A longer bound therefore loses nothing
it was going to win and wins what the shorter one threw away.

170 and not 180 leaves the margin for the program, the inspector and the exit. Note
that `LLM_TIMEOUT_S` (120) still caps any single call, which is above the 57-80 s a
program-writing call actually costs here and is deliberately left alone: it is the
loop's constant too, and the loop is the control arm.
"""

MACRO_FINALIZE_RESERVE_S = 5.0
"""Tail of the macro budget that belongs to inspecting and reporting, never to work."""

MACRO_FIRST_CALL_TOKENS = 1024
"""Ceiling on the program's length, and nothing more.

Not a tuning knob: measured 04.09.2026 on the evaluation model, the reply is 271
output tokens and 27 lines of program at every cap from 512 to 1536, so this bounds a
runaway and never shapes an ordinary answer. The timeout of that call is not a
constant at all -- `agent.macro` derives it from the deadline, because a fixed 45 s
here is exactly what ended E041 at zero with no tokens spent.
"""

MACRO_PROGRAM_TIMEOUT_S = 12.0
MACRO_REPAIR_PROGRAM_TIMEOUT_S = 5.0
"""How long a generated program may run. The second number is smaller because a repair
runs against a workspace the first attempt already moved, with less budget behind it."""

MACRO_REPAIR_MIN_S = 70.0
MACRO_REPAIR_TOKENS = 1024
"""Repair is attempted only with this much time left, and is itself bounded.

Seventy and not twenty, because twenty was arithmetic about a call that does not
exist: the measured cost of writing a program here is 57-74 s, so a repair beginning
with less than that left cannot finish, and starting it spends the budget the first
attempt's result was going to be reported with. On a task whose outer kill is 95 s
this threshold is never met and repair never runs -- which is the honest consequence
of the measurement, not a bug to tune around.
"""

MACRO_PACKET_CHARS = 2500
"""Ceiling on the dynamic half of the case packet: tree, paths, previews.

The program reads whatever else it needs itself -- that is the point of sending a
program instead of an action -- so this carries orientation, not data.

Was 6000 and is 2500 for a reason that is now measured rather than assumed: prefill
costs about 29 ms an uncached token on this stand (E034), so 6000 characters is
roughly 1500 tokens and 43 s of wall clock spent before the model writes anything --
inside a call whose whole budget is 73 s. The excerpts are what shrinks; the task text
is never cut.
"""

MACRO_PREVIEW_BYTES = 800
MACRO_PREVIEW_FILES = 4
"""Bounded previews of small files the task names by hand. Named by the task, never
found by walking: a preview of something the environment chose is untrusted input we
paid tokens to carry.
"""

MACRO_MAX_LLM_CALLS = 2
"""Logical calls one macro run may cost, repair included. Enforced in code."""
