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
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent import limits, loop, macro, paths, playbooks, preflight, redact, result_progress, verify
from agent.llm import HttpLlm, LlmSettings
from agent.ports import LlmPort
from agent.state import AgentState, Budget, Event

EXIT_OK = 0
EXIT_USAGE = 2

FINGERPRINT_CHARS = 12


PROTOCOL_KINDS = frozenset({"degrade", "protocol"})
"""Event kinds about the request shape, printed alongside the timing telemetry.

They were recorded into the audit and shown to nobody, and that cost an experiment:
E037 registered "the log must show the envelope being unwrapped" as its refutation
criterion, and no run of this agent has ever printed such a line. A criterion that
cannot be read off the artefacts is not a criterion, and a ladder demotion is exactly
the kind of thing a trace should say out loud -- it is the difference between one
request per step and two.
"""

PROTOCOL_DETAIL_CHARS = 120
"""Bound on the printed detail. `degrade` quotes the endpoint's own complaint, which
is neither ours to trust nor bounded by us; the events are already redacted."""


def emit_phase(event: Event) -> None:
    """Print bounded timing telemetry that remains useful after an outer kill."""
    if event.kind in PROTOCOL_KINDS:
        print(
            f"agent {event.kind}: step={event.step} {event.detail[:PROTOCOL_DETAIL_CHARS]}",
            flush=True,
        )
        return
    if event.kind != "phase":
        return
    print(
        f"agent phase: step={event.step} phase={event.name} status={event.detail} "
        f"duration_s={event.duration_s:.3f}",
        flush=True,
    )


def fingerprint(instruction: str) -> str:
    """A short digest of the task text, safe to print.

    The entrypoint has to carry arbitrary text through a shell: quotes, newlines,
    backslashes, leading dashes. A quoting bug there is invisible until a real
    instruction contains one, and by then it is a zero. Printing a digest lets the
    packaging preflight prove the round trip without ever echoing task content, which
    is untrusted data and may be large.
    """
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def resolve_workspace(
    env: Mapping[str, str], cwd: Path, agent_dir: Path, instruction: str = ""
) -> tuple[Path, str]:
    """Find the directory the task actually lives in. Returns (path, why).

    The obvious answer -- "wherever the runner started us" -- is wrong here. Harbor
    execs the agent with the working directory set to the agent's own install
    directory, so `cwd` is this archive, not the task. Every public task keeps its
    files under the image's WORKDIR instead, and the instructions reference absolute
    paths there.

    Nothing is hardcoded as the answer: each candidate is checked, the reason is
    returned for the trace, and the runner's own cwd remains the final fallback so a
    layout we have not seen still leaves the agent somewhere real.
    """
    declared = env.get(limits.WORKSPACE_ENV_NAME, "").strip()
    if declared:
        candidate = Path(declared)
        if candidate.is_dir():
            return candidate, f"{limits.WORKSPACE_ENV_NAME}={declared}"

    image_workdir = Path(limits.IMAGE_WORKDIR)
    if image_workdir.is_dir() and image_workdir.resolve() != agent_dir.resolve():
        return image_workdir, f"image workdir {limits.IMAGE_WORKDIR}"

    # cwd before the task text, and only skipped when it is our own install directory.
    # A runner that starts us inside the task -- a manual run, a different harness, a
    # future contract without Harbor -- already knows the answer, and review caught the
    # first version of this overruling it with a guess.
    if paths.plausible_workspace(cwd, agent_dir):
        return cwd, "runner cwd"

    # Measured in E011: under the Harbor contract cwd *is* our own install directory,
    # so with no `/app` the run wrote its deliverable into the agent's source tree,
    # verified it there and reported success against a verifier that scored zero. The
    # task text is the only remaining thing that knows where the task is.
    named = paths.workspace_candidates(instruction, agent_dir)
    if named:
        # The path itself is already printed as `workspace=`; the reason says how many
        # candidates competed, so a zero can be read back as "there was only one" or
        # "it won over another" without echoing more of the task text.
        return named[0], f"named by the task text ({len(named)} candidate(s))"

    return cwd, "runner cwd (no better candidate)"


def build_state(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> AgentState:
    """Assemble the run's state. Separate from `main` so tests do not need a shell."""
    agent_dir = Path(__file__).resolve().parent.parent
    workspace, reason = resolve_workspace(env, cwd, agent_dir, argv[1])

    named = paths.writable_paths(argv[1], agent_dir)
    state = AgentState(
        instruction=argv[1],
        workspace=workspace,
        named_paths=named,
        baseline=paths.snapshot(named),
        source_snapshot=result_progress.snapshot_sources(workspace),
        budget=Budget.from_env(env),
        secrets=redact.secrets_from_env(env),
    )
    state.record("preflight", "workspace", reason)
    return state


CONTROLLERS = {"loop": loop, "macro": macro}
"""The two interchangeable drivers, both satisfying `loop.run`'s signature.

A dict and not an `if` because the name is also what the trace prints, and a run whose
log says `controller=macro` is the only way to read an experiment's arms apart after
the fact. Nothing else in the agent branches on this.
"""


def select_controller(env: Mapping[str, str]):
    """Which driver runs this task. Unknown or unset means the loop.

    Unset must mean `loop`: an archive handed to the competition runner sets no
    variables of ours, and the controller it gets then has to be the one every previous
    experiment measured. An unrecognised value falls back the same way rather than
    failing the run -- a typo in a harness flag should cost a comparison, not a task.
    """
    name = env.get(limits.CONTROLLER_ENV_NAME, "").strip().lower()
    return CONTROLLERS.get(name or limits.DEFAULT_CONTROLLER, loop)


def main(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    *,
    llm: LlmPort | None = None,
) -> int:
    """Run one task end to end and report how it went, never whether it deserves a 1.

    `llm` is injectable for the same reason the loop takes its client as an argument:
    otherwise the only way to test this function is to let it open a socket, and a
    test suite that reaches an endpoint costs tokens on the second ranking criterion
    and fails wherever the endpoint is not reachable. Production passes nothing and
    gets the real client.
    """
    if len(argv) < 2 or not argv[1].strip():
        print('usage: run.sh "<task instruction>"', file=sys.stderr)
        return EXIT_USAGE

    try:
        return _run(argv, env, cwd, llm=llm)
    except Exception as exc:  # noqa: BLE001 - a crash is a zero; a traceback is worse
        # An uncaught exception is a zero either way, but exit 1 with a traceback is the
        # worst version: it can end the run before any partial artifact reaches disk and
        # it dumps internal paths. `Path.resolve` alone raises ValueError on a NUL byte
        # and OSError(ENAMETOOLONG) on an over-long path, and the tree is untrusted.
        # Report the stop and leave exit 0 -- the entrypoint contract (started,
        # understood its inputs) still held. Only the class name is printed, never the
        # message, so nothing from the environment or the tree can leak through it.
        #
        # The line carries the full done-line field set because `run_suite.DONE_LINE` is
        # what reads a run, and a line it cannot parse leaves `stop_reason` at its
        # default -- filing our own crash as an ordinary reasoning zero. The counters are
        # zero rather than measured: the state that held them died with the exception.
        # `stop=crash` is what tells a reader they are unknown, not observed.
        print(
            "agent done: solved=False stop=crash stage=none deliverable=none "
            f"steps=0/0 recovery=0 tokens=0 ({type(exc).__name__})",
            flush=True,
        )
        return EXIT_OK


def _run(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    *,
    llm: LlmPort | None = None,
) -> int:
    """The end-to-end run itself; `main` wraps it so no exception becomes exit 1."""
    state = build_state(argv, env, cwd)
    controller = select_controller(env)
    state.event_sink = emit_phase
    settings = LlmSettings.from_env(env)
    missing = settings.missing()

    # Status only, and deliberately terse: this line exists so a packaging smoke can
    # tell "started and understood its inputs" from "started". It carries no secrets
    # and no task content.
    #
    # Flushed because stdout is a pipe here, not a terminal, so Python buffers it in
    # blocks -- and the runner ends a task by killing the process at its deadline,
    # which drops the buffer unwritten. E011 measured the cost: a task killed at 180 s
    # produced an empty log, so the run that most needed diagnosing was the one with
    # none. Nothing about scoring changes; no public verifier reads our stdout.
    print(
        f"agent ready: workspace={state.workspace} "
        f"({state.audit[0].detail}) "
        f"instruction={len(state.instruction)}:{fingerprint(state.instruction)} "
        f"budget={state.budget.total_s:.0f}s "
        f"reserve={state.budget.verify_reserve_s:.0f}s "
        f"steps={state.budget.max_steps} "
        f"controller={controller.__name__.rsplit('.', 1)[-1]} "
        f"endpoint={'configured' if not missing else 'missing:' + ','.join(missing)}",
        flush=True,
    )

    if missing:
        # No endpoint, no actions. Saying so and leaving is the honest outcome; the
        # exit code still reports that the entrypoint contract worked, because that is
        # all it has ever meant here.
        return EXIT_OK

    inspect_started = state.start_phase("inspect")
    brief = preflight.inspect_environment(state.workspace, env, state.instruction)
    # `classify` rather than `select` for the numbers alone: "general, because two
    # categories tied at 0" and "general, because the task said nothing we know" are
    # different runs, and when this task comes back a zero that difference is the
    # first thing worth reading.
    choice = playbooks.classify(state.instruction)
    playbook = choice.playbook
    state.finish_phase("inspect", inspect_started)
    state.record("preflight", "playbook", f"{playbook.name}: {choice.reason}")

    client = llm or HttpLlm(
        settings=settings,
        on_event=lambda kind, detail: state.record(kind, "llm", detail),
    )

    # The settings and the redaction set are built; the endpoint coordinates now live in
    # the frozen LlmSettings and in state.secrets, so the copies in our own process
    # environment are redundant -- and reachable. A child shell already gets a scrubbed
    # environment, but its parent is this process, and `cat /proc/$PPID/environ | base64`
    # would read the key straight out of ours, past substring redaction. Popping them
    # here, before the first shell runs, closes that. Guarded on identity so tests that
    # pass their own env mapping never mutate the real process environment.
    if env is os.environ:
        for name in redact.AGENT_CREDENTIAL_ENV_NAMES:
            os.environ.pop(name, None)

    state.record("preflight", "controller", controller.__name__)
    outcome = controller.run(state, client, verify.verify, brief=brief, playbook=playbook)

    # Diagnostics, not the answer. M13 established that no public verifier reads agent
    # stdout: the result lives in the file this line merely names. Printing the outcome
    # is for a human reading a failed run, and it carries no task text and no secret.
    print(
        f"agent done: solved={outcome.solved} stop={outcome.stop_reason} "
        f"stage={outcome.verdict.stage if outcome.verdict else 'none'} "
        f"deliverable={outcome.deliverable or 'none'} "
        f"steps={state.budget.steps_used}/{state.budget.max_steps} "
        f"recovery={state.budget.recovery_passes_used} "
        f"tokens={state.budget.total_tokens}"
        f"{'' if state.budget.usage_reported else ' (lower bound: endpoint omitted usage)'}",
        flush=True,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv, os.environ, Path.cwd()))
