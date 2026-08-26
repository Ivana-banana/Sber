"""inspect -> plan -> act -> verify -> finish/recover, and nothing else.

The loop is deliberately boring and deliberately suspicious. Everything expensive it
does is bounded by code the model cannot reach: the deadline, the step cap, the
verifier reserve, the stuck detectors and the finish gate all live here, and the model
only ever gets to answer one question -- what is the single next action.

Three rules shape the whole file.

**Only a verifier ends a task.** M13 established that none of the six public verifiers
reads the agent's stdout: the reward is decided by a file at an absolute path or by
the behaviour of a service. So a `finish` whose path does not exist is refused, and a
red check spends the recovery pass rather than being reported as done.

**A failed call is a value.** Borrowed verbatim from `agent.ports`: a small model
returns malformed output routinely, and a loop that raises on it ends the task at zero
over one bad reply. Every failure below becomes an observation the model reads.

**Produce before you polish.** Past `EARLY_DELIVERABLE_DEADLINE_S` with nothing on
disk, the loop says so on every step. Being killed then costs the refinement, never
the deliverable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json

from agent import detectors, limits, prompt, tools
from agent.playbooks import Playbook
from agent.ports import LlmError, LlmPort
from agent.preflight import EnvironmentBrief
from agent.state import AgentState
from agent.verify import VerifyResult

VerifyFn = Callable[..., VerifyResult]
"""`verify(state, *, deliverable: Path, command: str) -> VerifyResult`, injected.

Injected rather than imported so the loop can be regression-tested against a recorded
transcript and a scripted verdict, at no tokens and no network.
"""


@dataclass(frozen=True)
class Outcome:
    """What the run has to show for itself. `solved` is never the model's opinion."""

    solved: bool
    """True only when the run stopped at a green check, i.e. `stop_reason` is
    `verified`. Never inferred from an artifact being present: a file on disk is
    evidence that something was written, not that it is right."""

    stop_reason: str
    """`verified`, `verify-failed`, `stuck`, `steps`, `budget` or `llm-unusable`."""

    deliverable: Path | None = None
    summary: str = ""
    verdict: VerifyResult | None = None
    observations: tuple[str, ...] = field(default=())


PRODUCE_NOW = (
    "[бюджет] на диске нет проверяемого результата. "
    "Запиши лучший вариант через write_file сейчас, улучшишь потом."
)
"""Said once when the run is out of time, and once when it is out of ideas.

Two triggers, one instruction, because the situation is the same one: the run has
nothing a verifier could read, and every step from here is worth less than an answer.
"""

STUCK_GRACE_STEPS = 3
"""Steps the run gets to act on that instruction before the detector may end it.

The detector fires on the shape of the last four actions and needs nothing new to fire
again, so one step of grace is one action. Three is not generosity -- reading a file,
then writing the answer, is an ordinary way to comply."""


def run(
    state: AgentState,
    llm: LlmPort,
    verify: VerifyFn,
    *,
    brief: EnvironmentBrief,
    playbook: Playbook,
) -> Outcome:
    """Drive one task to a checked result, or to an honest account of why not."""
    system = prompt.build_system(brief, playbook, state.instruction)
    observations: list[str] = []
    llm_failures = 0
    blocks_in_a_row = 0
    production_forced_at = 0
    last_verdict: VerifyResult | None = None
    stop_reason = ""
    summary = ""

    while state.budget.may_act():
        step = state.budget.start_step()

        if state.budget.past_early_deadline() and not state.artifacts:
            observations.append(PRODUCE_NOW)

        reply = llm.complete_structured(
            prompt.build_messages(system, observations),
            schema=prompt.ACTION_SCHEMA,
            max_output_tokens=limits.LLM_MAX_OUTPUT_TOKENS,
            timeout_s=max(1.0, state.budget.llm_timeout_s()),
        )

        if isinstance(reply, LlmError):
            llm_failures += 1
            state.record("llm", "error", reply.reason)
            observations.append(f"[step {step}] endpoint error: {reply.reason}")
            if llm_failures >= limits.MAX_CONSECUTIVE_LLM_FAILURES:
                stop_reason = "llm-unusable"
                summary = f"endpoint unusable after {llm_failures} failed calls: {reply.reason}"
                break
            continue

        llm_failures = 0
        state.budget.add_usage(reply)

        act, error = _parse(reply.text)
        if error:
            state.record("llm", "malformed", error)
            observations.append(f"[step {step}] {error}")
            continue

        tool = str(act.get("tool", ""))

        if tool == prompt.FINISH:
            deliverable, refusal = _deliverable(act, state)
            if refusal:
                state.record("verify", "refused", refusal)
                observations.append(f"[step {step}] {refusal}")
                continue

            assert deliverable is not None
            command = str(act.get("verify", "") or "")
            last_verdict = verify(state, deliverable=deliverable, command=command)
            state.record(
                "verify",
                last_verdict.stage,
                last_verdict.detail or "passed",
                exit_code=0 if last_verdict.ok else 1,
            )
            if last_verdict.ok:
                return Outcome(
                    solved=True,
                    stop_reason="verified",
                    deliverable=deliverable,
                    summary=str(act.get("summary", "") or ""),
                    verdict=last_verdict,
                    observations=tuple(observations),
                )

            if not state.budget.may_recover():
                stop_reason = "verify-failed"
                summary = f"check failed at {last_verdict.stage}: {last_verdict.detail}"
                break

            state.budget.start_recovery()
            observations.append(
                f"[step {step}] проверка не прошла на этапе {last_verdict.stage}: "
                f"{last_verdict.detail}. Исправь причину и повтори finish."
            )
            continue

        args, problem = _arguments(act, state)
        if problem or args is None:
            observations.append(f"[step {step}] {problem}")
            state.record("llm", "bad-action", problem)
            continue

        signal = detectors.blocked(state, tool, detectors.args_digest(args))
        if signal is not None:
            blocks_in_a_row += 1
            state.record("budget", signal.name, signal.detail)
            observations.append(f"[step {step}] {signal.detail}")
            if blocks_in_a_row >= limits.REPEAT_FAILURE_BLOCK:
                if not production_forced_at and not state.artifacts:
                    production_forced_at = step
                    blocks_in_a_row = 0
                    state.record("budget", "forced-production", signal.detail)
                    observations.append(PRODUCE_NOW)
                    continue
                stop_reason = "stuck"
                summary = signal.detail
                break
            continue

        blocks_in_a_row = 0
        result = _execute(tool, args, state)
        state.record(
            "tool",
            tool,
            result.detail or "ok",
            duration_s=result.duration_s,
            exit_code=result.exit_code,
        )
        body = result.output or result.detail
        state.observe(
            tool,
            args,
            body,
            ok=result.ok,
            error_code="" if result.ok else _error_code(result),
        )
        observations.append(
            f"[step {step}] {tool} -> {'ok' if result.ok else 'FAIL'}\n{body}".rstrip()
        )

        signal = detectors.stuck(state)
        if signal is not None:
            state.record("budget", signal.name, signal.detail)
            if not production_forced_at and not state.artifacts:
                # Measured in E010: eight of eighteen runs stopped here between the
                # fourth and the ninth step, with sixty to a hundred and twenty seconds
                # of budget left and nothing on disk -- the loop killed them before its
                # own early-deliverable checkpoint could. Being out of ideas is a reason
                # to stop exploring, which is what the checkpoint already says; it is
                # not a reason to hand back a zero while there is time to write an
                # answer. Once, and only while nothing has been produced: a run that is
                # still going in circles after this really has nothing left to spend on.
                production_forced_at = step
                observations.append(PRODUCE_NOW)
                continue
            if 0 < production_forced_at and step - production_forced_at < STUCK_GRACE_STEPS:
                # The instruction to produce arrived one step ago and the detector
                # needs no new evidence to fire again, so without this the run gets
                # exactly one action to act on it. Two is not generosity: reading a
                # file before writing the answer is an ordinary way to comply.
                continue
            stop_reason = "stuck"
            summary = signal.detail
            break

    if not stop_reason:
        stop_reason = "steps" if state.budget.steps_used >= state.budget.max_steps else "budget"

    return _finalise(state, verify, stop_reason, summary, last_verdict, observations)


# --- pieces small enough to read in one sitting -------------------------------


def _parse(text: str) -> tuple[dict[str, Any], str]:
    """Turn a reply into an action, or into the sentence the model needs to read.

    `agent.llm` already validates structured replies against the schema, so this is
    the second line of defence and exists for the rungs where it cannot: a text-mode
    endpoint, or a stub. It never raises.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}, "reply was not json: send one JSON object with a `tool` field and nothing else"
    if not isinstance(parsed, dict):
        return {}, "reply was not a json object: send one object with a `tool` field"
    if not parsed.get("tool"):
        return {}, "action had no `tool` field"
    return parsed, ""


def _deliverable(act: dict[str, Any], state: AgentState) -> tuple[Path | None, str]:
    """Resolve the path a `finish` claims, refusing the claim when nothing is there.

    This is the finish gate, and it is the single most valuable check in the file. A
    small model will announce success it has not achieved; every public verifier reads
    the environment instead of the announcement, so an absent file is simply a zero
    that arrived with a confident sentence attached.
    """
    raw = str(act.get("path", "") or "").strip()
    if not raw:
        return None, "finish refused: no `path`. Результат должен быть файлом в среде."

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = state.workspace / candidate
    if not candidate.exists():
        return None, f"finish refused: {candidate} does not exist. Сначала создай файл."
    return candidate, ""


REQUIRED: dict[str, str] = {
    prompt.RUN_SHELL: "command",
    prompt.READ_RANGE: "path",
    prompt.SEARCH: "pattern",
    prompt.WRITE_FILE: "path",
}
"""The one argument each tool cannot be run without.

Absent from the list: `list_dir` and `search`'s path, which default to the workspace,
and `write_file`'s content, because writing an empty file is a legitimate thing to ask
for even though no verifier accepts one.
"""


def _arguments(act: dict[str, Any], state: AgentState) -> tuple[dict[str, object] | None, str]:
    """Normalise one action into the arguments its tool takes. Returns (args, problem).

    Two shapes are repaired rather than rejected, for the same reason `agent.llm`
    accepts three tool-call dialects: a correct answer in an unexpected wrapper is
    still a correct answer, and throwing it away costs a step for nothing.

    * **Nested arguments.** Most agent frameworks use `{"tool": ..., "args": {...}}`,
      so a model trained on them sends it against our flat schema. `ACTION_SCHEMA`
      declares no `additionalProperties`, so that reply passes validation and would
      arrive here looking valid with every field empty -- a silently wrong action,
      which is worse than an error because nothing marks it.
    * **Relative paths.** The process cwd is the agent's own install directory, not
      the task's; that is the entire reason `resolve_workspace` exists. A relative
      path left alone reads the wrong tree and comes back with a clean "not found",
      which the model has no way to tell from an empty directory.

    A missing required argument is *not* repaired. `sh -c ""` exits 0 with no output,
    which reads as a successful step and teaches the model nothing; naming the missing
    field costs the same step and can be acted on.
    """
    tool = str(act.get("tool", ""))
    if tool not in prompt.TOOLS:
        return None, f"unknown tool: {tool}. Доступны: {', '.join(prompt.TOOLS)}"

    fields: dict[str, Any] = {}
    for wrapper in ("args", "arguments", "parameters"):
        nested = act.get(wrapper)
        if isinstance(nested, dict):
            fields.update(nested)
    # Top level last: it is the shape the schema actually asked for, so it wins.
    fields.update({key: value for key, value in act.items() if key != "tool"})

    needed = REQUIRED.get(tool, "")
    if needed and not str(fields.get(needed, "") or "").strip():
        return None, f"{tool} needs `{needed}`, and it was missing or empty"

    path = _in_workspace(str(fields.get("path", "") or "").strip(), state)

    if tool == prompt.RUN_SHELL:
        return {"command": str(fields["command"])}, ""
    if tool == prompt.READ_RANGE:
        return {"path": path, "start_line": _line(fields.get("start_line"))}, ""
    if tool == prompt.LIST_DIR:
        return {"path": path or str(state.workspace)}, ""
    if tool == prompt.SEARCH:
        return {"pattern": str(fields["pattern"]), "path": path or str(state.workspace)}, ""
    return {"path": path, "content": str(fields.get("content", "") or "")}, ""


def _in_workspace(raw: str, state: AgentState) -> str:
    """Anchor a relative path to the workspace, leaving an absolute one alone."""
    if not raw:
        return ""
    candidate = Path(raw)
    return str(candidate if candidate.is_absolute() else state.workspace / candidate)


def _line(value: object) -> int:
    """A start line, however the model spelled it. Never raises on junk."""
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return 1


def _execute(tool: str, args: dict[str, object], state: AgentState) -> tools.ToolResult:
    """Run one action. Every failure comes back as a `ToolResult`, never as a raise."""
    secrets = state.secrets

    if tool == prompt.RUN_SHELL:
        return tools.run_shell(
            str(args["command"]),
            cwd=state.workspace,
            timeout_s=state.budget.tool_timeout_s(),
            secrets=secrets,
        )
    if tool == prompt.READ_RANGE:
        return tools.read_range(
            Path(str(args["path"])), start_line=int(args["start_line"]), secrets=secrets
        )
    if tool == prompt.LIST_DIR:
        return tools.list_dir(Path(str(args["path"])))
    if tool == prompt.SEARCH:
        return tools.search(str(args["pattern"]), Path(str(args["path"])), secrets=secrets)

    target = Path(str(args["path"]))
    result = tools.write_file(
        target,
        str(args["content"]),
        workspace=state.workspace,
        permitted=state.named_paths,
        secrets=secrets,
    )
    if result.ok and target not in state.artifacts:
        # Tracked as the model named it, not as the filesystem resolves it: the
        # deliverable contract in these tasks is an absolute path, and a resolved
        # variant of it is a different string to a verifier that compares paths.
        state.artifacts.append(target)
    return result


def _error_code(result: tools.ToolResult) -> str:
    """Short stable label for a failure, so repeats of the same failure look alike."""
    if result.exit_code is not None:
        return f"exit:{result.exit_code}"
    return f"error:{result.detail.split(':')[0][:40]}"


def _finalise(
    state: AgentState,
    verify: VerifyFn,
    stop_reason: str,
    summary: str,
    verdict: VerifyResult | None,
    observations: list[str],
) -> Outcome:
    """Look at whatever exists, report it, and claim nothing about it.

    Every path into this function is a run that never reached a green check: the step
    cap, the clock, a detector, an unusable endpoint, or a verifier that already said
    no. So the artifact is still worth checking and still worth naming -- the external
    grader will read that file whether or not we called ourselves finished, and a
    human debugging a zero needs to know which file was left behind.

    But `solved` stays False here, always. `unverified` is an honest `ok=True` for the
    case `agent.verify` was built for -- the model chose to finish without a
    self-check, and refusing that would invent a rule the real verifier does not have.
    It is not honest for this case, where the model never got as far as claiming to be
    done. The two say "a non-empty file is there" and differ entirely in what that is
    evidence of, and treating them alike is how a timeout with a half-written file
    becomes a success in our own baseline. `stop_reason == "verified"` is the only
    thing that ever proves a task, and it never reaches this function.
    """
    deliverable = state.artifacts[-1] if state.artifacts else None
    if deliverable is not None and stop_reason != "verify-failed":
        verdict = verify(state, deliverable=deliverable, command="")
        state.record(
            "verify",
            verdict.stage,
            verdict.detail or "passed",
            exit_code=0 if verdict.ok else 1,
        )

    return Outcome(
        solved=False,
        stop_reason=stop_reason,
        deliverable=deliverable,
        summary=summary or (verdict.detail if verdict else ""),
        verdict=verdict,
        observations=tuple(observations),
    )
