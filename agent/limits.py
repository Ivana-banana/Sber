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

STEP_EXTENSION = 15
"""Extra steps the loop may grant itself once, at the cap, when time is clearly left.

Measured on the Qwen3.6-35B-A3B stand: a call costs 2-4 s, so an episode spends all
thirty steps in 90-130 s and stops with 400+ s of a 600 s budget unused. Both zeros of
the six-repeat candidate run ended that way (`stop=steps`), and the failed code-fix
episode had located the vulnerable handler and simply not edited it yet. Buying steps
with time the run will otherwise not spend is the cheapest thing that changes that
outcome; the tokens it costs are accepted.

Half of `MAX_STEPS`, not another thirty: the extension exists to let a run that is
close finish, not to double the price of a run that is lost. It is granted once and
`Budget.may_act` ends the run at the raised cap exactly as it ended it at the old one.
"""

STEP_EXTENSION_MIN_SPARE_FRACTION = 0.5
"""How much of the task budget must still be spendable at the cap for the extension.

A fraction of the run's own budget and not a constant, so the guard reads the same way
on every deadline. On a slow endpoint the step cap and the clock arrive together --
spendable time there is near zero and nothing is granted, which is the fast-path change
having no effect on the arm it was not measured on. On a 120 s budget the reserve alone
is 30 s, so this asks for 60 s of spendable time at step 30; an endpoint that could
reach the cap that early does not exist on any stand we have measured.
"""

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

FOCUS_RELAY_KEEP = 2
"""How many observations travel in a *focused* request, after a reasoning runaway.

Not a re-lay and not sticky: it applies to exactly one request, the one sent after a
reply that was truncated at the raised output cap without emitting an action. The
measured shape is a prompt of ~6.9k tokens that the model answers by reasoning until
the 8192-token ceiling with empty content, four logical calls in a row (the 16.09
treatment-5b call traces under `build/`, calls 4-7 of the third forensics run). Sending
that same prompt again buys the same non-answer, so the one thing the next request can
change cheaply is its size.

Two and not one, which is the only place this departs from the plan it was written
from. A runaway appends its own `endpoint error: reply truncated by length` line to the
history before the focused request is built, so a window of one would relay that note
and nothing else -- the model would be told its reply was cut, which the focus line
already says, and nothing about what it was doing. Two carries the note and the thing
that happened before it. Three would start rebuilding the history the focus is shrinking.

It is a floor and not the whole rule. `prompt.focus_base` widens the window when the
newest entries are all the loop talking to itself -- a checkpoint hint, a refusal, a
batch entry, this error line -- so that a focused request always carries the newest tool
result. Counting alone could not promise that, and a narrowed request with no observed
fact in it asks the model to invent one.

Read only by `agent.loop`. `agent.macro` asks one question with a hand-built message
list and no history, so there is nothing there to narrow and nothing there to change.
"""

BATCH_READONLY_MAX = 4
BATCH_OBSERVATION_MAX_CHARS = 4000
"""Total text one step's batch of read-only extras may add to the history.

Less than the widest single observation (`TOOL_OUTPUT_HEAD_BYTES + TOOL_OUTPUT_TAIL_BYTES`
= 6000) on purpose: the batch travels as ONE history entry next to the primary action's
own, so with `OBSERVATION_RELAY_KEEP` = 4 it occupies one relay slot, not four, and the
primary observation the model must react to (a failed write, a refused finish, a
checkpoint hint) is never pushed out by its own extras (critic, 16.09).
"""
"""Extra read-only calls from one reply that may run inside that reply's step.

A reasoning model answers a code-fix step with three to six `read_range` calls at
once. Executing only the first spends a step per file and makes the model re-request
the whole batch: 49 of 51 calls of one measured episode were reads, no edit was ever
made and all 30 steps went (the 16.09 code-fix traces under
`build/afk-q36-treatment/logs`). On an endpoint fast enough that a call costs 2-4 s,
the step cap and not the clock is what ends such a run, so the fix has to buy steps
rather than seconds.

Four, and only for the three read tools: it covers the observed batch size while
keeping the one-action-per-step contract for everything that changes state, and four
observations of `OBSERVATION_BUDGET_CHARS / 4` each add no more text than the single
widest `read_range` a step may add today.
"""

MAX_CONSECUTIVE_LLM_FAILURES = 3
"""Logical calls that may fail in a row before the endpoint is declared unusable.

Three and not one because a single transport failure is ordinary; three and not more
because `agent.llm` already retries inside one logical call, so this is three times
that ladder, not three requests. A run that cannot reach a model has no path to a
deliverable and should say so while there is still time to leave what it has.
"""

MAX_RECOVERY_PASSES = 3
"""Directed retries after a red verifier, borrowed as a shape from the bounded
fact-check loop in the ai-v-massy pipeline (`factcheck_retries <= 2`). The first is
always allowed; each later one only while `RECOVERY_SPARE_FRACTION` of the run is left.
A single pass ended runs after their second red check with most of the budget unspent."""

RECOVERY_SPARE_FRACTION = 0.5
"""Share of the total time that must remain for a recovery pass after the first."""

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

REPEAT_RESULT_BLOCK = 2
"""Identical successful calls returning the same result, after which the next copy of
that call is refused before it runs.

Measured, unlike the thresholds around it. In E-P2d one audit run spent 11 of its 15
steps on exact duplicates -- four identical directory listings, four identical file
reads, four identical writes -- because a repeat returned the model a byte-identical
observation, and a deterministic model reading an identical history produces an
identical action. A repeat whose output differs stays allowed: re-reading a file that
changed is work, and refusing it would be the detector inventing a rule.
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

EDIT_ECHO_CONTEXT_LINES = 4
EDIT_ECHO_MAX_CHARS = 600
"""How much of the file an anchored edit reports back, around the region it changed.

Measured on the fix-sqli fixtures (`F4-30b-paired` traces, 15.09): after a successful
`replace_text` the observation was `replaced 1 occurrence at line N` and nothing else,
so the model never saw what its edit left behind. In 5 of 5 failed edits the anchor
covered the statement that built the query but not the call that used it, leaving a
stale or duplicated call line right next to the new one; in 4 of 4 successful edits the
anchor included that call. The neighbouring statement is exactly what fits in a few
lines of context, and it is the difference the model could not observe.

Four lines each side and 600 characters, because this is paid on every edit and travels
in the observation history: it is a window onto the edit, not a second `read_range`.
"""

VERIFY_DETAIL_CHARS = 1200
"""How much failed-check output is relayed in a verifier verdict.

The command and exit code already occupy part of the recovery observation. This smaller
bound carries a missing-binary error or the first useful assertion without replaying an
entire test suite into the next model call.
"""

SHELL_EXECUTABLE = "/bin/sh"
"""What `run_shell` execs. `sh` and not `bash`: the pipelines and redirections a
playbook needs are POSIX, and `sh` is present in every image we might be handed."""

READ_MAX_BYTES = 8000
READ_MAX_LINES = 200
"""One `read_range` window. Deliberately smaller than the shell output budget: a file
is re-readable at another offset, so a wide window buys nothing and costs context on
every step."""

REPLACE_MAX_BYTES = 1024 * 1024
"""Maximum source size accepted by the anchored edit tool.

`replace_text` must prepare the complete replacement before touching the original;
this cap keeps that operation bounded and prevents an accidental data-file edit from
consuming the task budget.
"""

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

SOURCE_INDEX_MAX_FILES = 300
SOURCE_INDEX_MAX_ROWS = 12
SOURCE_INDEX_MAX_NAMES = 6
SOURCE_READ_MAX_BYTES = 131072
SOURCE_EXCERPT_MAX_CHARS = 1600
"""Bounds on the excerpts (`agent/sources.py::source_excerpts`) carried with them.

Read cap: a source file larger than 128 KiB is generated, vendored or data, and
parsing it costs more than the one function it might hold is worth.

Excerpt budget: the stand serves 4096 tokens of context and truncates silently past
it, so the resident brief cannot grow by the 4000 characters the plan first named --
1600 is about two handlers of the size the public applications use, and it leaves the
observation history the room it had. A starting value chosen to fit a measured
serving context, not an optimum; raising it is its own experiment."""
"""Bounds on the local import index (`agent/sources.py`), all three resident.

Files is a parse budget and the largest of the three: an application whose handlers
sit behind three hundred modules is not one this index can usefully summarise, and
stopping is better than a truncated graph that looks complete. Rows and names are what
reaches the prompt -- twelve modules at six names each is the shape of the public
applications, and a row costs about as much as one line of the tree above it.

Starting values, not measurements: nothing has ablated them yet."""
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
"""Ceiling on one logical call against an ordinary, non-thinking endpoint."""

LLM_THINKING_TIMEOUT_S = 240.0
"""Ceiling on one logical call once the endpoint is known to reason before answering.

Measured, not chosen for roundness. On the 4080 stand `qwen3:30b-a3b` generates at ~60
tok/s and one raised thinking reply cost 6549 completion tokens = 110 s of generation
(`F4-30b-paired`, 15.09); the same card shows the request also waiting for a busy slot.
At 120 s such a call cannot finish, and two consecutive client timeouts demoted the run
to `llm-unusable` -- two cells the control arm did not have. 240 gives the measured 110 s
call room for a queue wait of the same size and still leaves the run bounded.

It is a ceiling and never a grant: `Budget.llm_timeout_s` takes the minimum of this, the
spendable time and (before the checkpoint, on a fast endpoint) the checkpoint clamp, so
the verifier reserve and the task deadline bound the call exactly as before. A fast
endpoint never reaches this number -- it keeps `LLM_TIMEOUT_S`.
"""

LLM_MAX_OUTPUT_TOKENS = 2048
LLM_THINKING_MAX_OUTPUT_TOKENS = 8192
"""Hard ceiling on the raised output budget once a thinking endpoint is detected. A
reasoning model spends most of its completion inside the thinking span, so the default
2048 truncates the answer mid-thought (finish_reason=length, empty content) and the run
degrades to llm-unusable. Measured: qwen3:30b-a3b finishes a hard audit prompt at ~610
completion tokens when given 8192, versus a wall at 2048. Reached only on a thinking
endpoint, and capped down on a 400 that says the context cannot hold it."""

LLM_THINKING_TOKEN_MULTIPLIER = 4
"""Factor the caller's own output ceiling is raised by on a thinking endpoint, capped by
LLM_THINKING_MAX_OUTPUT_TOKENS. A multiplier, not a flat 8192, so a deliberately low
ceiling keeps its shape: the macro runaway guard's 1024 becomes 4096 (still bounded),
not 8192, while the main loop's 2048 reaches the 8192 ceiling."""
LLM_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
"""Finite retry schedule for transport failures. A permanently broken endpoint must
fail diagnosably rather than hang until the task times out."""

LLM_MAX_ATTEMPTS = 8
"""Total requests one logical call may cost, retries and protocol fallbacks included.

Raised from six with the thinking-endpoint recovery: the worst legitimate path is now
two length retries (raise, then thinking-off) plus up to two context cap-downs plus the
three-rung protocol fallback, and a cap that cuts the ladder short would strand a
recoverable call and look like an unsupported endpoint. Eight covers the measured chains
(length + length-with-thinking-off + ladder to text; length + two context cap-downs +
ladder). The transport-retry backoff is indexed by the global attempt number against a
two-long schedule, so it is spent by the first two attempts whatever they were; this cap
is the ceiling on total requests, the deadline still bounds wall time underneath it."""

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

# --- Is anything still running the code we changed? ---------------------------

SERVICE_SCAN_BUDGET_S = 0.5
"""Wall clock one whole process scan may take, procfs reads included.

The scan runs on `finish`, inside the reserve that belongs to verification, so it is
priced as part of the check and not as a step of its own. Half a second is about what
reading four hundred `/proc/<pid>` entries costs on the target image; past that the
scan stops where it stands and reports itself incomplete, because a bounded blind spot
is cheaper than an unbounded delay before the deliverable is verified.
"""

SERVICE_SCAN_MAX_PROCESSES = 2000
"""How many `/proc/<pid>` entries one scan reads before it stops and says so.

The count is the guard against a pathological process table, not the cost bound --
`SERVICE_SCAN_BUDGET_S` above is. Measured on the development host, reading an entry
(cmdline, stat, cwd) and matching it against the changed files costs about 40 us, so
this ceiling is roughly 80 ms and the clock ends the scan long before it does.

It is deliberately well above the few dozen processes a task container runs: entries
are visited in pid order, so a cap set near the real population would silently drop the
newest processes on any busier machine, and "stopped early" is reported as an incomplete
scan rather than as an answer. Stopping early can only hide a stale service (a hint not
given), never invent one.
"""

SERVICE_SCAN_MAX_MODIFIED = 32
"""Changed production files matched against the process table.

A repair touches one or two files and a refactor still runs behind one service, so this
bounds the cross product rather than shaping an answer.
"""

SERVICE_SCAN_MAX_MATCHES = 4
"""Processes reported as serving changed code before the scan stops looking.

More independent services than this referencing one change is not a picture the hint
can describe usefully, and the model needs the first one named, not a list.
"""

SERVICE_SCAN_ARGV_TOKENS = 3
"""Command-line tokens kept for the hint. `uvicorn main:app` is three; the fourth is
already an option value, and an option value is where a password travels."""

SERVICE_SCAN_TOKEN_CHARS = 60
"""Ceiling on one kept token. A very long path is orientation, not evidence."""

SERVICE_SCAN_MAX_ARGV_SCANNED = 12
"""Tokens of one command line examined while matching.

The entry point of a service is named early -- `uvicorn main:app`, `python manage.py
runserver`. What follows is options, and scanning them costs a path resolution each.
"""

SERVICE_SCAN_MAX_DEPTH = 8
"""Directory levels above a changed file that may be treated as its project root.

`/app/routers/auth.py` must let `uvicorn main:app` started from `/app` match, so the
walk goes up; it stops before it can reach something so broad that every process on
the machine appears to load the change. Generic system directories are excluded by
name regardless of depth.
"""

SERVICE_SCAN_MAX_FDS = 512
"""Descriptors of one process examined while looking for its listening sockets."""

SERVICE_SCAN_MAX_SOCKET_LINES = 4000
"""Lines read from `/proc/net/tcp` and `tcp6`.

In a busy container these files hold one line per established connection and can reach
megabytes; the listening sockets we are after are a handful. Reading a bounded prefix
can only cost a port number in the hint, never correctness.
"""

SERVICE_SCAN_START_MARGIN_S = 1.0
"""How much later than a process started a file must have changed to count as newer.

`/proc/<pid>/stat` gives the start time in ticks since boot and `/proc/stat` gives the
boot time as a whole second, so a process start reconstructed from them carries up to a
second of error; a file mtime does not. The margin is that error. It is spent in the
safe direction: a service that legitimately restarted itself just after the edit is
left alone, at the price of not warning about a process started in the same second as
the change.
"""

VACUOUS_VERIFY_MAX_REFUSALS = 2
"""Finishes refused for a verify command that only reads the deliverable. The refusal
is a nudge towards a real check; a model that offers the reader a third time is let
through rather than refused until the step cap with its deliverable already on disk."""

SERVICE_SCAN_MAX_REFUSALS = 1
SERVICE_RESTART_MIN_SPARE_S = 90.0
"""Spendable seconds a run must still have for a stale-service refusal to be worth it.

Refusing a finish costs the model the steps to stop the service, start it again, wait
for the port and re-run its check -- three or four actions. Inside the verifier reserve
the loop cannot even grant one more step, so a refusal there ends the run on `budget`
without the model's own verify command ever running (critic, 16.09). Below this margin
the finish goes through and the finding is only audited.
"""
"""Times one run may have `finish` refused because a pre-change process serves the code.

Once. The observation says what to do about it; if the model looks and decides the
process does not matter, that is its call to make, and a gate that keeps saying no to
a model that has already answered it burns the budget the repair needs. The audit note
is written either way, so a run that finished over the warning is visible afterwards.
"""

SERVICE_SCAN_MAX_CHECKS = 2
"""Scans one run may pay for: the one that can refuse, and the one that records what
the run finished with anyway."""


# --- Diagnostics --------------------------------------------------------------

TRACE_TOOLS_ENV_NAME = "AGENT_TRACE_TOOLS"
TRACE_BUDGET_ENV_NAME = "AGENT_TRACE_BUDGET"
"""Opt-in live traces, both off unless the variable is set to a non-empty value.

`AGENT_TRACE_TOOLS` prints which tool ran with what result; `AGENT_TRACE_BUDGET` prints
the `thinking` and `budget` notes the run writes to `state.audit` -- the endpoint being
recognised as a reasoning model, the call clamp being lifted, a detector refusing an
action. Both exist because a live zero cannot be told apart from the outside otherwise,
and an evaluator reading the terminal should not have to open a trace file to see why a
call was given 240 s instead of 90.

Never on in a submission: the runner sets none of our variables, and both printers emit
already-redacted text, so neither can carry the endpoint key.
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
that `LLM_TIMEOUT_S` (120) still caps any single call on an ordinary endpoint, which is
above the 57-80 s a program-writing call actually costs here and is deliberately left
alone: it is the loop's constant too, and the loop is the control arm. On an endpoint
that has shown itself to reason the cap becomes `LLM_THINKING_TIMEOUT_S`, and this
budget minus the program's own seconds is then what actually bounds the call.
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
