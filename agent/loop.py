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
from pathlib import Path, PurePosixPath
from typing import Any

import json
import shlex

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
    history_base = 0
    llm_failures = 0
    blocks_in_a_row = 0
    last_verdict: VerifyResult | None = None
    stop_reason = ""
    summary = ""

    while state.budget.may_act():
        step = state.budget.start_step()

        if state.budget.past_early_deadline() and not state.artifacts:
            observations.append(
                "[бюджет] времени осталось мало, а на диске нет проверяемого результата. "
                "Запиши лучший вариант через write_file сейчас, улучшишь потом."
            )

        plan_started = state.start_phase("plan")
        # Anchored, not sliding: the base moves only when the kept history stops
        # fitting its budget, so every other request extends the previous one and the
        # server pays for the new tail alone.
        history_base = prompt.history_base(observations, history_base)
        reply = llm.complete_structured(
            prompt.build_messages(system, observations, history_base),
            schema=prompt.ACTION_SCHEMA,
            max_output_tokens=limits.LLM_MAX_OUTPUT_TOKENS,
            timeout_s=max(1.0, state.budget.llm_timeout_s()),
        )
        state.finish_phase("plan", plan_started)

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
            refusal = _cannot_fail(command, deliverable)
            if refusal:
                state.record("verify", "refused", refusal)
                observations.append(f"[step {step}] {refusal}")
                continue

            verify_started = state.start_phase("verify")
            last_verdict = verify(state, deliverable=deliverable, command=command)
            state.finish_phase("verify", verify_started)
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
                stop_reason = "stuck"
                summary = signal.detail
                break
            continue

        blocks_in_a_row = 0
        act_started = state.start_phase("act")
        result = _execute(tool, args, state)
        state.finish_phase("act", act_started)
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
            if state.artifacts:
                # Circling with something to lose. E028/E029 removed this stop
                # unconditionally and measured both halves of the trade: without it
                # `fix-redirect-scheme` was gained twice, and `mut-salutation-beacon`
                # was lost twice because the run spent its remaining steps rewriting a
                # file whose verifier was already green. The run has no oracle telling
                # it which version was the good one, so the only version it can protect
                # is the one on disk right now.
                stop_reason = "stuck"
                summary = signal.detail
                break
            # Nothing written yet, so there is nothing to protect and ending is a
            # certain zero: `_finalise` has no deliverable to name. E024's
            # `fix-redirect-scheme` quit here after 67.5 s of 180 s without a single
            # write. Hand the model the observation and let the clock do the ending.
            observations.append(
                f"[step {step}] похоже, ты ходишь по кругу: {signal.detail}. "
                f"Смени подход или проверь другую гипотезу."
            )

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

    A directory is refused here too, and the split is deliberate: the gate owns the
    *shape* of the claim, `agent.verify` owns its *content*. Letting a directory
    through cost the run its one recovery pass to be told "expected deliverable not
    found" about a path the model can see is there -- a true verdict wrapped in a
    false sentence, which is the worst kind of observation to hand a small model.
    """
    raw = str(act.get("path", "") or "").strip()
    if not raw:
        return None, "finish refused: no `path`. Результат должен быть файлом в среде."

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = state.workspace / candidate
    if not candidate.exists():
        return None, f"finish refused: {candidate} does not exist. Сначала создай файл."
    if candidate.is_dir():
        return None, (
            f"finish refused: {candidate} is a directory, not a file. "
            f"Назови файл с результатом."
        )
    return candidate, ""


READERS = frozenset(
    {
        "cat", "head", "tail", "less", "more", "ls", "stat", "file",
        "wc", "od", "xxd", "hexdump", "strings", "echo",
    }
)
"""Commands whose exit code says the file is readable and nothing else.

Not a security list -- `run_shell` is not confined and this is not where confinement
lives. It is the set of programs that, pointed at one existing non-empty file, exit 0
for every possible content, which is exactly the question the finish gate has already
answered before `verify` is reached.
"""


ALWAYS_OK = frozenset({"echo", "printf", "true", ":"})
"""Commands that exit 0 unconditionally, so a `||` branch ending in one never fails."""


def _cannot_fail(command: str, deliverable: Path) -> str:
    """Refuse a `verify` that cannot fail on a wrong answer. Returns the reason, or "".

    Two shapes, both measured rather than imagined, and both producing the same zero:
    the run stops as `verified` holding a wrong file, which is the most expensive class
    we have because it gives back the rest of its budget as well.

    * **A lone reader.** Ten of the night's fifteen `finish` calls offered
      `cat <deliverable>`, and the two tasks that reproduced a false win 5/5 each
      offered exactly that. `cat` exits 0 on any bytes, and existence and non-emptiness
      are what the finish gate has already established before `verify` is reached.
    * **A swallowed failure.** E031's `audit-hardcoded-token` offered a real check,
      was told no, and came back with `grep -q … && echo 'PASS' || echo 'FAIL'`. The
      `|| echo` makes the exit code 0 whatever `grep` decided. This one is worse than
      the first: the model was shown a red verdict and repaired the check rather than
      the result.
    """
    stripped = command.strip()
    if not stripped:
        return ""

    tail = stripped.rsplit("||", 1)[-1].strip() if "||" in stripped else ""
    if tail:
        try:
            words = shlex.split(tail)
        except ValueError:
            words = []
        if words and PurePosixPath(words[0]).name in ALWAYS_OK:
            return (
                f"finish refused: `{stripped}` заканчивается ветвью `|| {words[0]} …`, "
                f"поэтому выходит с нулём даже когда проверка не прошла. Дай команду, "
                f"код возврата которой зависит от результата, и не глуши её провал."
            )

    if any(token in stripped for token in ("&&", "||", ";", "|", "\n", "`", "$(")):
        # Left to `agent.verify`: every real check the model wrote had one of these --
        # `grep -q … && grep -q …` was the night's only true win -- and refusing a real
        # check costs the task outright, while missing a fake one costs what is already
        # being lost.
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        # An unbalanced quote is not our problem to diagnose; `agent.verify` will run
        # it and report the shell's own complaint, which is more useful than a guess.
        return ""
    if not parts or PurePosixPath(parts[0]).name not in READERS:
        return ""

    operands = [part for part in parts[1:] if not part.startswith("-")]
    if any(not _names(operand, deliverable) for operand in operands):
        # It reads something else as well, so its exit code can depend on more than the
        # deliverable's existence. Not our case.
        return ""

    return (
        f"finish refused: `{stripped}` только читает {deliverable.name} и выходит с нулём "
        f"при любом содержимом, а существование и непустоту этого файла проверка уже "
        f"прошла. Дай команду, которая падает, если результат неверный: сравни содержимое "
        f"с тем, что требует задание (`grep -q`, `diff`, `python3 -c ...`)."
    )


def _names(operand: str, deliverable: Path) -> bool:
    """Whether an argument refers to the deliverable, absolutely or workspace-relative."""
    return operand == str(deliverable) or PurePosixPath(operand).name == deliverable.name


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
        verify_started = state.start_phase("verify")
        verdict = verify(state, deliverable=deliverable, command="")
        state.finish_phase("verify", verify_started)
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
