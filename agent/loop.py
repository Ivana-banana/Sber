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
import os
import re
import shlex
import stat
import time

from agent import detectors, limits, prompt, result_contract, result_progress, sources, tools
from agent.playbooks import Playbook
from agent.ports import LlmError, LlmPort, LlmReply
from agent.preflight import EnvironmentBrief
from agent.state import AgentState
from agent.verify import VerifyResult

VerifyFn = Callable[..., VerifyResult]
"""`verify(state, *, deliverable: Path, command: str) -> VerifyResult`, injected.

Injected rather than imported so the loop can be regression-tested against a recorded
transcript and a scripted verdict, at no tokens and no network.
"""

READ_ONLY_SHELLS = frozenset(
    {
        "cat", "echo", "file", "find", "grep", "head", "id", "ls", "more", "printf",
        "ps", "pwd", "readlink", "rg", "stat", "tail", "true", "false", "uname", "wc",
        "which", "whoami",
    }
)
"""Commands treated as read-only when no shell operator is present.

Unknown commands and compound commands are considered capable of changing files or
services. This is a progress-detector hint, not a shell security boundary.
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
            # H06b measured what happens when this hint is replaced by a probe for the
            # missing fact: the deliverable collapsed to 1 of 6 public tasks. It stays.
            # What P2b measured is narrower: with the vulnerable handler both in the
            # brief and in an observation one step earlier, "write your best now" was
            # answered with `{"findings": []}` four times. So for an audit the hint says
            # what best means. It does not ask for a finding -- a safe application is a
            # legitimate empty report, and requiring content would be the answer
            # smuggled in through the loop.
            checkpoint = (
                "[бюджет] запас шагов и времени на исходе, а на диске нет проверяемого "
                "результата. "
                "Запиши лучший вариант через write_file сейчас, улучшишь потом."
            )
            if playbook.name == "code-audit":
                checkpoint += (
                    " Лучший вариант — то, что подтверждают твои наблюдения: перенеси в "
                    "отчёт прочитанные строки и файлы. В evidence приведи саму строку "
                    "кода из наблюдения, с путём и номером, а не пересказ. Пустой "
                    "список findings — это утверждение, что прочитанный код безопасен."
                )
            elif playbook.name == "code-fix":
                checkpoint += (
                    " Для code-fix запиши минимальную правку production source/config и "
                    "проверку поведения; README, отчёт или тест сами по себе не являются "
                    "исправлением."
                )
            observations.append(checkpoint)

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

        if not state.budget.allow_long_llm and _endpoint_is_slow_or_thinking(reply, llm):
            # A thinking or slow endpoint: lift the 90s pre-checkpoint clamp so the next
            # call gets the full budget instead of being truncated mid-reasoning. The
            # call that just timed out or truncated is already lost -- this saves the
            # rest of the run (F4).
            state.budget.allow_long_llm = True
            state.record("budget", "allow-long-llm", "slow/thinking endpoint; lifting the call clamp")

        if isinstance(reply, LlmError):
            llm_failures += 1
            state.budget.add_usage(reply)  # a failed attempt was still generated and billed
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
            # Folded here and not only in `_arguments`: the finish branch reads its
            # own fields and returns before that function is ever reached, so
            # `finish(arguments={...})` -- the wrapper every other tool survives --
            # was refused for having no path while carrying one.
            act = {"tool": tool, **_fields(act)}
            deliverable, refusal = _deliverable(act, state)
            if refusal:
                state.record("verify", "refused", refusal)
                observations.append(f"[step {step}] {refusal}")
                continue

            assert deliverable is not None
            command = str(act.get("verify", "") or "")
            refusal = _cannot_fail(command, deliverable)
            if refusal and result_contract.exact_file_requirement(state.instruction) is None:
                state.record("verify", "refused", refusal)
                observations.append(f"[step {step}] {refusal}")
                continue

            source_changes = _source_changes(state, playbook)
            if source_changes is not None and source_changes.known and not source_changes.paths:
                refusal = (
                    "finish refused: code-fix has not changed a production source/config file. "
                    "A README, report, test, or other artifact is not a repair; change the "
                    "relevant source and verify its behavior."
                )
                state.record("verify", "refused", refusal)
                observations.append(f"[step {step}] {refusal}")
                continue
            if source_changes is not None and not source_changes.known:
                state.record("verify", "source-progress-unknown", source_changes.reason)
                observations.append(
                    f"[step {step}] production source progress is unknown: "
                    f"{source_changes.reason}. Do not treat an artifact alone as a repair."
                )

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
                has_protected_candidate = bool(state.artifacts)
                source_changes = _source_changes(state, playbook)
                if source_changes is not None:
                    has_protected_candidate = source_changes.known and bool(source_changes.paths)
                    if not source_changes.known:
                        observations.append(
                            f"[step {step}] production source progress is unknown: "
                            f"{source_changes.reason}. Keep working instead of treating "
                            "the current artifact as a code repair."
                        )
                    elif not source_changes.paths:
                        observations.append(
                            f"[step {step}] code-fix has only artifact/report progress; "
                            "change production source before stopping."
                        )
                if has_protected_candidate:
                    stop_reason = "stuck"
                    summary = signal.detail
                    break
                # The guard E031 put on the stuck exit below, missing here until E060.
                # This is the exit that fires on a fast endpoint: seven of eighteen
                # fixtures in E052 ended through it at steps 6-10 of 30, 35-70 s into a
                # 600 s budget, with nothing written. With no deliverable there is
                # nothing to protect and ending is a certain zero, so the block stays
                # advice and the caps do the ending.
                blocks_in_a_row = 0
            continue

        blocks_in_a_row = 0
        act_started = state.start_phase("act")
        result = _execute(tool, args, state, playbook=playbook)
        state.finish_phase("act", act_started)
        if os.environ.get("AGENT_TRACE_TOOLS"):
            # Diagnostic only, off unless explicitly enabled: the loop otherwise prints
            # phases but never which tool ran with what result, so a live zero cannot be
            # told from the outside. Output is already redacted by the tool. Never on in
            # a submission -- the runner sets none of our variables.
            summary = (result.output or result.detail or "").replace("\n", " ")[:160]
            print(f"agent tool: step={step} {tool} ok={result.ok} {summary}", flush=True)
        if _advances_state_epoch(tool, args) and (
            result.ok or tool == prompt.RUN_SHELL
        ):
            state.advance_state_epoch()
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

        if result.ok and tool == prompt.WRITE_FILE:
            exact = _verify_exact_write(state, verify, args, playbook=playbook)
            if exact is not None:
                deliverable, verdict = exact
                if verdict.ok:
                    return Outcome(
                        solved=True,
                        stop_reason="verified",
                        deliverable=deliverable,
                        summary="exact-file instruction oracle passed after write",
                        verdict=verdict,
                        observations=tuple(observations),
                    )
                observations.append(
                    f"[step {step}] exact-file check failed: {verdict.detail}"
                )

        signal = detectors.stuck(state)
        if signal is not None:
            state.record("budget", signal.name, signal.detail)
            has_protected_candidate = bool(state.artifacts)
            source_changes = _source_changes(state, playbook)
            if source_changes is not None:
                has_protected_candidate = source_changes.known and bool(source_changes.paths)
                if not source_changes.known:
                    observations.append(
                        f"[step {step}] production source progress is unknown: "
                        f"{source_changes.reason}. Keep working instead of treating the "
                        "current artifact as a code repair."
                    )
                elif not source_changes.paths:
                    observations.append(
                        f"[step {step}] code-fix has only artifact/report progress; "
                        "change production source before stopping."
                    )
            if has_protected_candidate:
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

    return _finalise(
        state, verify, stop_reason, summary, last_verdict, observations, playbook=playbook
    )


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

    Three shapes, all measured rather than imagined, and all producing the same zero:
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
    * **A pipeline of readers.** `cat <deliverable> | od -A x -t x1z -v`, offered once
      the lone reader was refused. Nothing in it compares anything, so it exits 0 for
      any bytes; the pipe alone made it pass a rule that let every `|` through.

    The exemption for a pipe is narrowed, not withdrawn: one stage that can disagree
    with the file -- `grep`, `diff`, `python3` -- and the whole command is left to
    `agent.verify`, because refusing a real check costs the task outright while missing
    a fake one costs what is already being lost. Known gap, shared with the lone reader
    and not widened here: a stage whose flag takes the path as a value, such as
    `head -c 4 <deliverable>`, reads as an operand list we do not recognise and passes.
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

    if any(token in stripped for token in ("&&", "||", ";", "\n", "`", "$(")):
        # Left to `agent.verify`: every real check the model wrote had one of these --
        # `grep -q … && grep -q …` was the night's only true win -- and refusing a real
        # check costs the task outright, while missing a fake one costs what is already
        # being lost.
        return ""

    stages = stripped.split("|")
    if not all(
        _only_reads(stage, deliverable, first=index == 0) for index, stage in enumerate(stages)
    ):
        # One stage that can disagree with the file makes the pipeline a real check,
        # and the exit code is that stage's when it is last. Same reasoning as above:
        # refusing a real check costs the task, letting a fake one through costs what
        # is already lost.
        return ""

    what = "конвейер" if len(stages) > 1 else "команда"
    return (
        f"finish refused: `{stripped}` — {what}, который только читает {deliverable.name} "
        f"и выходит с нулём при любом содержимом, а существование и непустоту этого файла "
        f"проверка уже прошла. Дай команду, которая падает, если результат неверный: "
        f"сравни содержимое с тем, что требует задание (`grep -q`, `diff`, `python3 -c ...`)."
    )


def _only_reads(stage: str, deliverable: Path, *, first: bool) -> bool:
    """Whether one stage exits 0 for every possible content of the deliverable.

    The two positions are read differently because a bare word means different things
    in them. The first stage names its input, so every operand of it has to be the
    deliverable. A later stage reads the pipe, and its bare words are mostly values of
    flags -- `od -A x -t x1z` carries no operand at all -- so only a token that looks
    like a path is evidence that it reads something else.
    """
    try:
        parts = shlex.split(stage.strip())
    except ValueError:
        # An unbalanced quote is not our problem to diagnose; `agent.verify` will run
        # it and report the shell's own complaint, which is more useful than a guess.
        return False
    if not parts or PurePosixPath(parts[0]).name not in READERS:
        return False

    operands = [part for part in parts[1:] if not part.startswith("-")]
    if not first:
        operands = [part for part in operands if "/" in part]
    return all(_names(operand, deliverable) for operand in operands)


def _names(operand: str, deliverable: Path) -> bool:
    """Whether an argument refers to the deliverable, absolutely or workspace-relative."""
    return operand == str(deliverable) or PurePosixPath(operand).name == deliverable.name


REQUIRED: dict[str, str] = {
    prompt.RUN_SHELL: "command",
    prompt.READ_RANGE: "path",
    prompt.SEARCH: "pattern",
    prompt.WRITE_FILE: "path",
    prompt.WRITE_JSON: "path",
    prompt.REPLACE_TEXT: "path",
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

    fields = _fields(act)

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
    if tool == prompt.REPLACE_TEXT:
        return {
            "path": path,
            "old": str(fields.get("old", "") or ""),
            "new": str(fields.get("new", "") or ""),
        }, ""
    if tool == prompt.WRITE_JSON:
        # `data` is kept as the value the model sent -- a dict or list, not str()'d --
        # because serialising it ourselves is the whole point; the tool parses it if the
        # model still sent a JSON string.
        return {"path": path, "data": fields.get("data")}, ""
    return {"path": path, "content": str(fields.get("content", "") or "")}, ""


def _fields(act: dict[str, Any]) -> dict[str, object]:
    """Every argument the action carries, however the model wrapped it."""
    fields: dict[str, object] = {}
    for wrapper in ("args", "arguments", "parameters"):
        nested = act.get(wrapper)
        if isinstance(nested, dict):
            fields.update(nested)
    # Top level last: it is the shape the schema actually asked for, so it wins.
    fields.update({key: value for key, value in act.items() if key != "tool"})
    return fields


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


_EVALUATOR_DIRS = frozenset({"test", "tests", "spec", "specs", "fixtures"})
_EVALUATOR_FILES = frozenset({"conftest.py", "pytest.ini", "tox.ini"})
_FIND_MUTATION_OPTIONS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)
_GIT_READ_ONLY_COMMANDS = frozenset({"diff", "log", "ls-files", "show", "status"})
_SHELL_WRAPPERS = frozenset({"bash", "command", "dash", "env", "exec", "ksh", "nohup", "sh", "sudo", "zsh"})
_OPAQUE_COMMAND_RUNNERS = frozenset(
    {
        "at",
        "batch",
        "bwrap",
        "busybox",
        "ccache",
        "chroot",
        "crontab",
        "daemon",
        "daemonize",
        "chrt",
        "doas",
        "fakeroot",
        "firejail",
        "flock",
        "ionice",
        "intercept-build",
        "lxc-attach",
        "nsenter",
        "nohup",
        "nice",
        "openssl",
        "parallel",
        "prlimit",
        "proot",
        "runc",
        "runuser",
        "scan-build",
        "screen",
        "setsid",
        "setpriv",
        "ssh",
        "ssh-agent",
        "ssh-keygen",
        "sqlite3",
        "stdbuf",
        "start-stop-daemon",
        "su",
        "sudo",
        "systemd-run",
        "tmux",
        "time",
        "timeout",
        "taskset",
        "unshare",
        "watch",
        "zip",
    }
)
_CODE_LOADING_ASSIGNMENTS = frozenset(
    {
        "BASH_ENV",
        "CLASSPATH",
        "CCC_OVERRIDE_OPTIONS",
        "COMPILER_PATH",
        "DYLD_INSERT_LIBRARIES",
        "ENV",
        "GEM_HOME",
        "GEM_PATH",
        "GCONV_PATH",
        "GCC_EXEC_PREFIX",
        "GIT_ASKPASS",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_EDITOR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE",
        "GIT_INDEX_FILE",
        "GIT_PAGER",
        "GIT_PROXY_COMMAND",
        "GIT_SEQUENCE_EDITOR",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
        "HOME",
        "JAVA_TOOL_OPTIONS",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_AUDIT_32",
        "LD_AUDIT_64",
        "LD_PRELOAD",
        "LUA_CPATH",
        "LUA_INIT",
        "LUA_PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "PERL5LIB",
        "PERL5OPT",
        "PROMPT_COMMAND",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "RUBYLIB",
        "RUBYOPT",
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
        "ZDOTDIR",
    }
)
_BROAD_MUTATORS = frozenset(
    {
        "cp",
        "cmake",
        "chmod",
        "chown",
        "aria2c",
        "curl",
        "go",
        "install",
        "7z",
        "bsdtar",
        "ar",
        "cpio",
        "docker",
        "ex",
        "gdb",
        "jar",
        "java",
        "jshell",
        "jjs",
        "lldb",
        "make",
        "m4",
        "mkdir",
        "mv",
        "npm",
        "npx",
        "nerdctl",
        "ninja",
        "patch",
        "pip",
        "pip3",
        "rm",
        "rmdir",
        "rsync",
        "scp",
        "setfacl",
        "sftp",
        "tar",
        "touch",
        "truncate",
        "unzip",
        "uv",
        "vim",
        "nvim",
        "podman",
        "wget",
    }
)
_GIT_MUTATION_AFTER_SEPARATOR = re.compile(
    r"(?:^|(?:;|&&|\|\||\||\n)\s*)"
    r"(?:(?:[a-z_][a-z0-9_]*=\S+|"
    r"(?:bash|command|dash|env|exec|ksh|nohup|sh|sudo|zsh))\s+)*"
    r"(?:[^\s;&|]+/)?git\s+([a-z][a-z0-9-]*)(?=\s|$)",
    re.IGNORECASE,
)
_GIT_COMMAND_ANYWHERE = re.compile(
    r"(?<![a-z0-9_./-])git\s+([a-z][a-z0-9-]*)(?=\s|$)", re.IGNORECASE
)
_EVALUATOR_TOKEN = re.compile(
    r"(?<![a-z0-9_])(?:tests?|specs?|fixtures)(?=[/\\\s:'\"]|$)"
    r"|(?<![a-z0-9_])(?:test_[a-z0-9_.-]+\.(?:py|js|jsx|ts|tsx)|"
    r"[a-z0-9_.-]+_test\.py|conftest\.py)"
    r"(?![a-z0-9_])",
    re.IGNORECASE,
)


def _is_evaluator_path(path: Path, state: AgentState) -> bool:
    try:
        root = state.workspace.resolve()
        target = (path if path.is_absolute() else root / path).resolve()
    except OSError:
        return False

    try:
        relative = target.relative_to(root)
        parts = tuple(part.lower() for part in relative.parts)
    except ValueError:
        # Named task paths may intentionally live outside the process workspace. In
        # that case there is no relative path, so inspect the absolute path instead.
        parts = tuple(part.lower() for part in target.parts)
    filename = parts[-1] if parts else ""
    return any(part in _EVALUATOR_DIRS for part in parts) or (
        filename in _EVALUATOR_FILES
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


_INODE_SCAN_SKIP = sources.SKIP_DIRECTORIES - {".git"}
"""Directories the hardlink-alias scan never walks. Derived from the one broad skip set
in `agent.sources` (`.venv`, `venv`, `site-packages`, `.tox`, `build`, `dist`, caches)
rather than a third copy. `.git` is deliberately kept: the repository-control guard
finds an alias by matching a source file's inode against the `.git` file it shadows, so
it has to see `.git`. The cost was walking `.venv` on every code-fix write (~9.5 s each,
~170 s of a 248 s fix run in the 14B trace); no evaluator or repo-control file hides in
a vendored or cache tree."""


def _workspace_files(workspace: Path) -> list[Path]:
    """Every regular file under the workspace, skipping vendored and cache trees.

    Keeps `.git` (see `_INODE_SCAN_SKIP`) so the repository-control alias guard still
    works, while pruning the `.venv`/cache trees that made the scan cost seconds a write.
    """
    found: list[Path] = []
    for current, directories, names in os.walk(workspace):
        directories[:] = [d for d in directories if d not in _INODE_SCAN_SKIP]
        for name in names:
            found.append(Path(current) / name)
    return found


def _shares_evaluator_inode(path: Path, state: AgentState) -> bool:
    """Catch a non-obvious hardlink alias for an evaluator file."""
    if _is_evaluator_path(path, state):
        return True
    try:
        target = (path if path.is_absolute() else state.workspace / path).resolve()
        target_stat = target.stat()
    except OSError:
        return False
    target_inode = (target_stat.st_dev, target_stat.st_ino)
    try:
        candidates = _workspace_files(state.workspace)
    except OSError:
        candidates = []
    candidates.extend(state.named_paths)
    for candidate in candidates:
        try:
            if candidate.resolve() == target or not candidate.is_file():
                continue
            if not _is_evaluator_path(candidate, state):
                continue
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if (candidate_stat.st_dev, candidate_stat.st_ino) == target_inode:
            return True
    return False


def _is_repository_control_path(path: Path, state: AgentState) -> bool:
    try:
        root = state.workspace.resolve()
        raw_target = path if path.is_absolute() else root / path
        try:
            raw_relative = raw_target.relative_to(root)
            raw_parts = tuple(part.lower() for part in raw_relative.parts)
        except ValueError:
            raw_parts = tuple(part.lower() for part in raw_target.parts)
        if ".git" in raw_parts:
            return True
        target = raw_target.resolve()
    except OSError:
        return False
    try:
        relative = target.relative_to(root)
        parts = tuple(part.lower() for part in relative.parts)
    except ValueError:
        parts = tuple(part.lower() for part in target.parts)
    return ".git" in parts


def _shares_repository_control_inode(path: Path, state: AgentState) -> bool:
    if _is_repository_control_path(path, state):
        return True
    try:
        target = (path if path.is_absolute() else state.workspace / path).resolve()
        target_stat = target.stat()
    except OSError:
        return False
    target_inode = (target_stat.st_dev, target_stat.st_ino)
    try:
        candidates = _workspace_files(state.workspace)
    except OSError:
        candidates = []
    candidates.extend(state.named_paths)
    for candidate in candidates:
        try:
            if candidate.resolve() == target or not candidate.is_file():
                continue
            if not _is_repository_control_path(candidate, state):
                continue
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if (candidate_stat.st_dev, candidate_stat.st_ino) == target_inode:
            return True
    return False


def _shares_code_fix_protected_inode(path: Path, state: AgentState) -> bool:
    return _shares_evaluator_inode(path, state) or _shares_repository_control_inode(
        path, state
    )


def _code_fix_shell_expand_workspace_alias(value: str, state: AgentState) -> tuple[str, ...]:
    """Resolve the shell's cwd aliases without trying to emulate arbitrary env vars."""
    expanded = [value]
    try:
        workspace = state.workspace.resolve()
    except OSError:
        return tuple(expanded)
    for alias in ("$PWD", "${PWD}"):
        if value == alias or value.startswith(f"{alias}/"):
            expanded.append(str(workspace / value[len(alias) :].lstrip("/")))
    return tuple(expanded)


_SHELL_READERS = frozenset({"cat", "head", "tail", "more", "nl", "grep", "rg"})
"""Shell verbs that open a file to show its content -- the ones that count as reading it
for the unread-source guard. Deliberately narrow: `ls`/`wc`/`stat`/`echo` name a file
without showing enough of it to edit safely, so counting them would weaken the guard."""


def _read_paths_from_shell(command: str, state: AgentState) -> list[Path]:
    """Resolved files a reader command opened, so a later write is not blind.

    Reads the first pipe segment -- `cat f | head` and `grep x f | head` read `f` -- and
    only when that segment's verb (normalised, so `/bin/cat` counts) is a known reader.
    Each operand that resolves to an existing file counts as read, exactly like
    `read_range`. A model that inspected a module this way has seen it.
    """
    segment = command.split("|", 1)[0]
    try:
        words = shlex.split(segment)
    except ValueError:
        return []
    if not words or PurePosixPath(words[0]).name not in _SHELL_READERS:
        return []
    found: list[Path] = []
    for word in words[1:]:
        if word.startswith("-") or word in {">", ">>", "<"}:
            continue
        candidate = Path(word) if Path(word).is_absolute() else state.workspace / word
        try:
            if candidate.is_file():
                found.append(candidate.resolve())
        except OSError:
            continue
    return found


def _workspace_files_touched_since(workspace: Path, since: float) -> list[Path]:
    """Resolved workspace files whose mtime is at or after `since`.

    After a shell command that may change files, this is how a file the model *created*
    (`echo > new.py`, `cp a.py b.py`, `tee`, `sed -i`) becomes known to the run without
    parsing redirections: `artifacts` only tracks the file tools, so without this a
    later `write_file` to a shell-created source would be refused as blind -- a false
    refusal that can zero a task. Bounded by the pruned `_workspace_files` walk.
    """
    touched: list[Path] = []
    for path in _workspace_files(workspace):
        try:
            info = path.stat()
            # ctime as well as mtime: `mv a.py c.py` and `cp -p` preserve the content's
            # mtime but not the entry's ctime, so a renamed or attribute-copied module
            # would be missed by mtime alone -- a false refusal on a plausible edit.
            if max(info.st_mtime, info.st_ctime) >= since:
                touched.append(path.resolve())
        except OSError:
            continue
    return touched


def _unread_source_refusal(target: Path, state: AgentState) -> str:
    """Refuse a code-fix `write_file` that overwrites a source file this run never read.

    The 14B fix-sqli trace overwrote a router it had not opened with a stub, broke the
    application's import, and the verifier never ran -- while "wrote N bytes" read as
    success. Narrow on purpose: only `write_file` (replace_text is the right tool for a
    targeted change and write_json is for JSON deliverables), only an existing non-empty
    source file (report templates like .txt/.md are not source candidates and pass), and
    only when the run has neither read nor produced it. The hint names the safe path.
    """
    try:
        resolved = target.resolve()
        info = resolved.stat()
    except OSError:
        return ""
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        return ""
    if not result_progress._is_source_candidate(resolved.name):
        return ""
    known = set(state.read_paths)
    for artifact in state.artifacts:
        try:
            known.add(artifact.resolve())
        except OSError:
            continue
    if resolved in known:
        return ""
    return (
        f"code-fix refused: {resolved} exists and this run has not read it. "
        "read_range it first, or change one part with replace_text(path, old, new)."
    )


def _evaluator_path_refusal(path: Path, state: AgentState) -> str:
    """Refuse a code-fix edit that would rewrite its own evaluator."""
    try:
        target = (path if path.is_absolute() else state.workspace / path).resolve()
    except OSError:
        return ""
    repository_control = _is_repository_control_path(path, state) or (
        _shares_repository_control_inode(target, state)
    )
    if not _shares_evaluator_inode(target, state) and not repository_control:
        return ""
    if repository_control:
        return (
            f"code-fix refused: {target} is a repository control path. Keep .git "
            "metadata and hooks immutable; change production source/config instead."
        )
    return (
        f"code-fix refused: {target} is an evaluator/test path. Keep tests, fixtures "
        "and expected answers immutable; change production source/config instead."
    )


def _code_fix_shell_has_output_option(words: list[str]) -> bool:
    return any(
        word.startswith("-o")
        or word == "--basetemp"
        or word.startswith("--basetemp=")
        or (
            word.startswith("-")
            and any(
                marker in word.lower()
                for marker in ("output", "report", "cache", "junit", "xml", "html")
            )
        )
        for word in words
    )


def _code_fix_shell_c_payloads(command: str) -> tuple[str, ...]:
    try:
        words = shlex.split(command)
    except ValueError:
        return ()
    payloads: list[str] = []
    for index, word in enumerate(words):
        if PurePosixPath(word).name not in {"bash", "dash", "ksh", "sh", "zsh"}:
            continue
        for option_index in range(index + 1, len(words) - 1):
            if words[option_index] in {"-c", "-ec", "-lc"}:
                payloads.append(words[option_index + 1])
                break
            if not words[option_index].startswith("-"):
                break
    return tuple(payloads)


def _code_fix_is_dynamic_loader(word: str) -> bool:
    name = PurePosixPath(word).name.lower()
    return (
        name.startswith("ld-linux")
        or name.startswith("ld-musl")
        or name.startswith("ld64")
        or name in {"ld.so", "ld.so.1"}
    )


def _code_fix_shell_has_find_mutation(command: str) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_find_mutation(nested_command):
            return True
    for nested in re.finditer(
        r"\$\(([^()]*)\)|`([^`]*)`|\(([^()]*)\)|\{([^{}]*)\}", command
    ):
        nested_command = (
            nested.group(1) or nested.group(2) or nested.group(3) or nested.group(4) or ""
        )
        if nested_command and _code_fix_shell_has_find_mutation(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return (
        any(PurePosixPath(word).name == "find" for word in words)
        and any(word in _FIND_MUTATION_OPTIONS for word in words)
    )


def _code_fix_shell_operator_flags(command: str) -> tuple[bool, bool]:
    compound = False
    output_redirect = False
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            elif char == "`" or (char == "$" and command[index + 1 : index + 2] == "("):
                compound = True
            index += 1
            continue
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|\n<" or char in "({" or char == "`":
            compound = True
        elif char == "$" and command[index + 1 : index + 2] == "(":
            compound = True
        elif char == ">":
            output_redirect = True
        index += 1
    return compound, output_redirect


def _code_fix_shell_has_compound_syntax(command: str) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_compound_syntax(nested_command):
            return True
    return _code_fix_shell_operator_flags(command)[0]


def _code_fix_shell_redirect_targets(command: str) -> tuple[str, ...]:
    targets: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char != ">":
            index += 1
            continue
        index += 1
        if index < len(command) and command[index] == ">":
            index += 1
        while index < len(command) and command[index].isspace():
            index += 1
        if index >= len(command):
            break
        if command[index] in {"'", '"'}:
            target_quote = command[index]
            index += 1
            start = index
            while index < len(command) and command[index] != target_quote:
                index += 1
            targets.append(command[start:index])
            if index < len(command):
                index += 1
            continue
        start = index
        while (
            index < len(command)
            and not command[index].isspace()
            and command[index] not in ";|&<>"
        ):
            index += 1
        if index > start:
            targets.append(command[start:index])
    return tuple(targets)


def _code_fix_is_python_executable(word: str, workspace: Path | None = None) -> bool:
    names = [PurePosixPath(word).name.lower()]
    if workspace is not None and ("/" in word or "\\" in word):
        try:
            target = (Path(word) if Path(word).is_absolute() else workspace / word).resolve()
            names.append(target.name.lower())
        except OSError:
            pass
    return any(
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name) is not None
        for name in names
    )


def _code_fix_executable_names(word: str, workspace: Path | None = None) -> set[str]:
    names = {PurePosixPath(word).name.lower()}
    if workspace is not None:
        try:
            candidate = Path(word) if Path(word).is_absolute() else workspace / word
            if candidate.exists() or candidate.is_symlink():
                names.add(candidate.resolve().name.lower())
        except OSError:
            pass
    return names


def _code_fix_shell_has_resolved_unsafe_executable(
    command: str, state: AgentState
) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_resolved_unsafe_executable(nested_command, state):
            return True
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        return False
    if not words:
        return False
    raw_name = PurePosixPath(words[0]).name.lower()
    names = _code_fix_executable_names(words[0], state.workspace)
    resolved_names = names - {raw_name}
    script_names = {
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "lua",
        "luajit",
        "node",
        "nodejs",
        "perl",
        "php",
        "rscript",
        "ruby",
        "tclsh",
        "wish",
        "bash",
        "dash",
        "ksh",
        "sh",
        "zsh",
        "find",
        "xargs",
        "git",
    }
    return bool(
        any(_code_fix_is_dynamic_loader(name) for name in resolved_names)
        or resolved_names & (script_names | _OPAQUE_COMMAND_RUNNERS | _BROAD_MUTATORS)
    )


def _code_fix_shell_has_unknown_local_script_runner(
    command: str, state: AgentState
) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_unknown_local_script_runner(nested_command, state):
            return True
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        return False
    if not words or "/" not in words[0]:
        return False
    raw_name = PurePosixPath(words[0]).name.lower()
    known_names = (
        set(READ_ONLY_SHELLS)
        | set(_SHELL_WRAPPERS)
        | set(_OPAQUE_COMMAND_RUNNERS)
        | set(_BROAD_MUTATORS)
        | set(_GIT_READ_ONLY_COMMANDS)
        | {"git", "pytest", "py.test", "ruff", "mypy", "flake8", "pylint"}
    )
    if raw_name in known_names or _code_fix_is_python_executable(words[0], state.workspace):
        return False
    try:
        executable = (Path(words[0]) if Path(words[0]).is_absolute() else state.workspace / words[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return False
    except OSError:
        return False
    return True


def _code_fix_shell_has_compiler_plugin(command: str) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_compiler_plugin(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    words = _code_fix_shell_unwrap_simple_wrappers(words)
    executable_name = PurePosixPath(words[0]).name.lower() if words else ""
    compiler_names = {
        "cc",
        "c++",
        "clang",
        "clang-cpp",
        "clang++",
        "clang-cl",
        "clang-tidy",
        "gcc",
        "g++",
        "gcc-ar",
        "gcc-nm",
        "gcc-ranlib",
        "ld",
        "ld.bfd",
        "ld.gold",
        "ld.lld",
        "nm",
        "ranlib",
        "objcopy",
        "objdump",
        "readelf",
        "strip",
    }
    versioned_driver = any(
        re.fullmatch(rf"{re.escape(base)}-\d+(?:\.\d+)*", executable_name)
        for base in {
            "cc",
            "c++",
            "clang",
            "clang-cpp",
            "clang++",
            "clang-cl",
            "clang-tidy",
            "gcc",
            "g++",
            "gcc-ar",
            "gcc-nm",
            "gcc-ranlib",
            "ld",
            "ld.bfd",
            "ld.gold",
            "ld.lld",
        }
    )
    if not words or (executable_name not in compiler_names and not versioned_driver):
        return False
    if any(word.startswith("@") for word in words[1:]):
        return True
    linker_names = {"ld", "ld.bfd", "ld.gold", "ld.lld"}
    versioned_linker = any(
        re.fullmatch(rf"{re.escape(base)}-\d+(?:\.\d+)*", executable_name)
        for base in linker_names
    )
    if (executable_name in linker_names or versioned_linker) and any(
        word == "-c"
        or word == "--mri-script"
        or word.startswith("--mri-script=")
        for word in words[1:]
    ):
        return True
    return any(
        word == "-fplugin"
        or word.startswith("-fplugin=")
        or word.startswith("-fplugin-arg-")
        or word == "-fpass-plugin"
        or word.startswith("-fpass-plugin=")
        or word == "-load"
        or word.startswith("-load=")
        or word == "--config"
        or word.startswith("--config=")
        or word == "-B"
        or (word.startswith("-B") and len(word) > 2)
        or word == "-T"
        or (word.startswith("-T") and len(word) > 2)
        or word == "--script"
        or word.startswith("--script=")
        or word == "--default-script"
        or word.startswith("--default-script=")
        or word == "-dT"
        or (word.startswith("-dT") and len(word) > 2)
        or word == "--error-handling-script"
        or word.startswith("--error-handling-script=")
        or word == "-specs"
        or word.startswith("-specs=")
        or word == "-wrapper"
        or word.startswith("-wrapper=")
        or word == "-Xclang"
        or word.startswith("-Xclang=")
        or word == "-cc1"
        or word == "-Xlinker"
        or word.startswith("-Xlinker=")
        or word.lower().startswith("/clang:")
        or word == "-plugin"
        or word.startswith("-plugin=")
        or word == "--plugin"
        or word.startswith("--plugin=")
        or word == "-plugin-opt"
        or word.startswith("-plugin-opt=")
        or (
            word.startswith("-Wl,")
            and any(
                part == "-plugin"
                or part.startswith("-plugin=")
                or part == "-plugin-opt"
                or part.startswith("-plugin-opt=")
                or part == "--plugin"
                or part.startswith("--plugin=")
                or part == "--plugin-opt"
                or part.startswith("--plugin-opt=")
                or part == "-T"
                or (part.startswith("-T") and len(part) > 2)
                or part == "--script"
                or part.startswith("--script=")
                or part == "--default-script"
                or part.startswith("--default-script=")
                or part == "-dT"
                or (part.startswith("-dT") and len(part) > 2)
                or part == "--error-handling-script"
                or part.startswith("--error-handling-script=")
                or part == "-c"
                or part == "--mri-script"
                or part.startswith("--mri-script=")
                for part in word[4:].split(",")
            )
        )
        for word in words[1:]
    )


def _code_fix_shell_has_evaluator_redirect(command: str, state: AgentState) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_evaluator_redirect(nested_command, state):
            return True
    for target_text in _code_fix_shell_redirect_targets(command):
        if _shares_code_fix_protected_inode(Path(target_text), state):
            return True
    return False


def _code_fix_shell_has_evaluator_operand(command: str, state: AgentState) -> bool:
    """Catch mutating commands that name a production alias, not the evaluator."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_evaluator_operand(nested_command, state):
            return True
    if _code_fix_shell_is_read_only(command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    for word in words:
        candidates = [word]
        if "=" in word:
            candidates.append(word.split("=", 1)[1])
        if word.startswith("-o") and len(word) > 2:
            candidates.append(word[2:])
        if word.startswith("-MF") and len(word) > 3:
            candidates.append(word[3:])
        if word.startswith("-Wl,"):
            linker_parts = word[4:].split(",")
            for index, part in enumerate(linker_parts):
                if part in {"-Map", "--Map"} and index + 1 < len(linker_parts):
                    candidates.append(linker_parts[index + 1])
                if part.startswith(("-Map=", "--Map=")):
                    candidates.append(part.split("=", 1)[1])
                if part == "--dependency-file" and index + 1 < len(linker_parts):
                    candidates.append(linker_parts[index + 1])
                if part.startswith("--dependency-file="):
                    candidates.append(part.split("=", 1)[1])
        if word.startswith("-"):
            candidates = [candidate for candidate in candidates if not candidate.startswith("-")]
        for candidate in candidates:
            if candidate.startswith("$"):
                return True
            for expanded in _code_fix_shell_expand_workspace_alias(candidate, state):
                if expanded and _shares_code_fix_protected_inode(Path(expanded), state):
                    return True
    return False


def _code_fix_shell_is_read_only(command: str) -> bool:
    """Allow common test/read commands while leaving arbitrary shell mutation visible."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or any(_code_fix_shell_operator_flags(command)):
        return False
    if any(word in _FIND_MUTATION_OPTIONS for word in words):
        return False
    executable = PurePosixPath(words[0]).name
    if executable in READ_ONLY_SHELLS:
        return True
    if executable in {"pytest", "py.test"}:
        return not _code_fix_shell_has_output_option(words[1:])
    if executable == "ruff":
        if len(words) < 2:
            return False
        if words[1] == "check":
            return not any(
                word == "--fix" or word.startswith("--fix=") for word in words[2:]
            ) and not _code_fix_shell_has_output_option(words[2:])
        return (
            words[1] == "format"
            and "--check" in words[2:]
            and not _code_fix_shell_has_output_option(words[2:])
        )
    if executable in {"mypy", "flake8", "pylint"}:
        return not _code_fix_shell_has_output_option(words[1:])
    if _code_fix_is_python_executable(words[0]):
        if len(words) < 3 or words[1] != "-m" or words[2] not in {"pytest", "compileall"}:
            return False
        return not _code_fix_shell_has_output_option(words[3:])
    if executable == "git":
        return (
            len(words) >= 2
            and words[1] in _GIT_READ_ONLY_COMMANDS
            and "--ext-diff" not in words[2:]
            and not any(
                word == "-o"
                or (word.startswith("-o") and not word.startswith("--"))
                or word == "--output"
                or word.startswith("--output=")
                for word in words[2:]
            )
        )
    return False


def _code_fix_shell_has_glob_mutation(command: str) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not any(any(marker in word for marker in "*?[") for word in words):
        return False
    return not _code_fix_shell_is_read_only(command)


def _code_fix_shell_has_xargs(command: str) -> bool:
    if re.search(r"(?<![a-z0-9_./-])xargs(?=\s|$)", command, re.I):
        return True
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    if any(PurePosixPath(word).name == "xargs" for word in words):
        return True
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_xargs(nested_command):
            return True
    for nested in re.finditer(
        r"\$\(([^()]*)\)|`([^`]*)`|\(([^()]*)\)|\{([^{}]*)\}", command
    ):
        nested_command = (
            nested.group(1) or nested.group(2) or nested.group(3) or nested.group(4) or ""
        )
        if nested_command and _code_fix_shell_has_xargs(nested_command):
            return True
    return False


def _code_fix_shell_strip_assignments(words: list[str]) -> list[str]:
    while len(words) > 1 and re.fullmatch(r"[a-z_][a-z0-9_]*=.*", words[0], re.I):
        words = words[1:]
    return words


def _code_fix_shell_unwrap_simple_wrappers(words: list[str]) -> list[str]:
    words = _code_fix_shell_strip_assignments(words)
    while words and PurePosixPath(words[0]).name in {
        "builtin",
        "command",
        "env",
        "exec",
        "nohup",
        "sudo",
    }:
        wrapper = PurePosixPath(words.pop(0)).name
        if wrapper == "env":
            while words:
                option = words[0]
                if "=" in option and not option.startswith("-"):
                    words.pop(0)
                    continue
                if option == "--":
                    words.pop(0)
                    break
                if option in {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}:
                    words.pop(0)
                    if words:
                        words.pop(0)
                    continue
                if (
                    option.startswith("-u")
                    and not option.startswith("--")
                    or option.startswith("--unset=")
                    or option.startswith("-C")
                    and not option.startswith("--")
                    or option.startswith("--chdir=")
                    or option.startswith("-a")
                    and not option.startswith("--")
                    or option.startswith("--argv0=")
                ):
                    words.pop(0)
                    continue
                if option.startswith("-"):
                    words.pop(0)
                    continue
                break
        else:
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if wrapper == "exec" and (
                    option == "-a"
                    or option.startswith("-a")
                    and not option.startswith("--")
                ):
                    if option == "-a" and words:
                        words.pop(0)
        words = _code_fix_shell_strip_assignments(words)
    return words


def _code_fix_shell_has_eval(command: str) -> bool:
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        words = []
    if words and PurePosixPath(words[0]).name == "eval":
        return True
    return any(_code_fix_shell_has_eval(payload) for payload in _code_fix_shell_c_payloads(command))


def _code_fix_shell_has_trap(command: str) -> bool:
    """Reject exit handlers whose quoted body can mutate after the tool returns."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_trap(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(PurePosixPath(word).name == "trap" for word in words)


def _code_fix_shell_has_env_split_string(command: str) -> bool:
    """Reject GNU env's option that reparses one opaque string as a command."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_env_split_string(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    env_seen = False
    for word in words:
        if PurePosixPath(word).name == "env":
            env_seen = True
            continue
        if env_seen and (
            word == "-S"
            or word.startswith("-S")
            or word == "--split-string"
            or word.startswith("--split-string=")
        ):
            return True
    return False


def _code_fix_shell_has_opaque_runner(command: str) -> bool:
    """Reject tools that execute a quoted command outside this parser's grammar."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_opaque_runner(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(
        PurePosixPath(word).name in _OPAQUE_COMMAND_RUNNERS
        or _code_fix_is_dynamic_loader(word)
        for word in words
    )


def _code_fix_shell_has_startup_hook_assignment(command: str) -> bool:
    """Reject assignments that load code or select an untrusted executable."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_startup_hook_assignment(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    for word in words:
        if "=" not in word:
            continue
        name = word.split("=", 1)[0].rstrip("+").upper()
        if (
            name in _CODE_LOADING_ASSIGNMENTS
            or name.startswith("GIT_CONFIG_")
            or name.startswith(("LUA_CPATH_", "LUA_INIT_", "LUA_PATH_"))
            or (name.startswith("BASH_FUNC_") and name.endswith("%%"))
        ):
            return True
    return False


def _code_fix_python_module(words: list[str]) -> str | None:
    """Return a Python -m module, including attached ``-mMODULE`` syntax."""
    for index, word in enumerate(words[1:], start=1):
        if word == "-m":
            return words[index + 1] if index + 1 < len(words) else ""
        if word.startswith("-m") and not word.startswith("--"):
            return word[2:]
    return None


def _code_fix_shell_has_pytest_plugin_option(command: str) -> bool:
    """Reject pytest plugin imports hidden in otherwise read-only test commands."""
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_pytest_plugin_option(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not any(
        PurePosixPath(word).name in {"pytest", "py.test"} for word in words
    ) and "pytest" not in command:
        return False
    return any(
        word == "-p"
        or (word.startswith("-p") and not word.startswith("--"))
        or word == "-o"
        or (word.startswith("-o") and not word.startswith("--"))
        or word == "-c"
        or (word.startswith("-c") and not word.startswith("--"))
        or word == "--plugin"
        or word.startswith("--plugin=")
        or word == "--pyargs"
        or word == "--doctest-modules"
        or word == "--doctest-glob"
        or word.startswith("--doctest-glob=")
        or word == "--override-ini"
        or word.startswith("--override-ini=")
        or word == "--pdbcls"
        or word.startswith("--pdbcls=")
        or word == "--debug"
        or word.startswith("--debug=")
        or word == "--config-file"
        or word.startswith("--config-file=")
        or word == "--rootdir"
        or word.startswith("--rootdir=")
        or word == "--log-file"
        or word.startswith("--log-file=")
        for word in words
    )


def _code_fix_shell_has_python_script(
    command: str, workspace: Path | None = None
) -> bool:
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        words = []
    if words and _code_fix_is_python_executable(words[0], workspace):
        if any(word == "-c" or word.startswith("-c") for word in words[1:]):
            return True
        module = _code_fix_python_module(words)
        return module is not None and module not in {"pytest", "compileall"}
    return any(
        _code_fix_shell_has_python_script(payload, workspace)
        for payload in _code_fix_shell_c_payloads(command)
    )


def _code_fix_shell_has_script_invocation(
    command: str, workspace: Path | None = None
) -> bool:
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        words = []
    if not words:
        return False
    executable = PurePosixPath(words[0]).name
    executable_names = _code_fix_executable_names(words[0], workspace)
    shell_names = {"bash", "dash", "ksh", "sh", "zsh"}
    if executable not in shell_names and executable_names & shell_names:
        return True
    if executable in shell_names:
        for word in words[1:]:
            if word in {"-c", "-ec", "-lc"}:
                break
            if word == "--" or word.startswith("-"):
                continue
            return True
    if words[0] in {".", "source"}:
        return len(words) > 1
    if _code_fix_is_python_executable(words[0], workspace):
        if any(word == "-c" or word.startswith("-c") for word in words[1:]):
            return True
        module = _code_fix_python_module(words)
        if module is not None:
            return module not in {"pytest", "compileall"}
        return any(not word.startswith("-") for word in words[1:])
    if executable in {
        "lua",
        "luajit",
        "node",
        "nodejs",
        "perl",
        "php",
        "Rscript",
        "ruby",
        "tclsh",
        "wish",
    }:
        if executable in {"node", "nodejs"} and any(
            word == "--run" or word.startswith("--run=") for word in words[1:]
        ):
            return True
        if executable in {"node", "nodejs"} and any(
            word == "--env-file"
            or word.startswith("--env-file=")
            or word == "--env-file-if-exists"
            or word.startswith("--env-file-if-exists=")
            for word in words[1:]
        ):
            return True
        if executable in {"node", "nodejs"} and any(
            word in {
                "--experimental-default-config-file",
                "--experimental-config-file",
            }
            or word.startswith("--experimental-config-file=")
            for word in words[1:]
        ):
            return True
        if executable in {"node", "nodejs"} and any(
            word == "--snapshot-blob"
            or word.startswith("--snapshot-blob=")
            or word == "--build-snapshot"
            or word == "--build-snapshot-config"
            or word.startswith("--build-snapshot-config=")
            for word in words[1:]
        ):
            return True
        if executable in {"lua", "luajit"} and any(
            word == "-l" or word.startswith("-l") for word in words[1:]
        ):
            return True
        if executable in {"lua", "luajit"} and any(
            word in {"-e", "-E"}
            or word.startswith(("-e", "-E"))
            for word in words[1:]
        ):
            return True
        if executable == "perl" and any(
            word in {"-M", "-m", "-d", "-p", "-n", "-e", "-E"}
            or word.startswith(("-M", "-m", "-d", "-p", "-n", "-e", "-E"))
            for word in words[1:]
        ):
            return True
        if executable in {"node", "nodejs"} and any(
            word in {"-r", "--require", "--loader", "--import"}
            or word.startswith(
                ("-r", "--require=", "--loader=", "--import=", "--experimental-loader=")
            )
            or word in {"--experimental-loader"}
            for word in words[1:]
        ):
            return True
        if any(
            word in {"-e", "--eval", "-n", "-p", "-r"}
            or word.startswith(("--eval=", "-e="))
            for word in words[1:]
        ):
            return True
        return any(not word.startswith("-") for word in words[1:])
    if executable in {"awk", "gawk", "mawk", "nawk"}:
        return True
    return any(
        _code_fix_shell_has_script_invocation(payload, workspace)
        for payload in _code_fix_shell_c_payloads(command)
    )


def _code_fix_shell_word_is_workspace_root(word: str, workspace: Path) -> bool:
    for alias in ("$PWD", "${PWD}"):
        if word.startswith(alias):
            try:
                if (workspace / word[len(alias) :].lstrip("/")).resolve() == workspace:
                    return True
            except OSError:
                pass
    if word in {".", "./", "..", "../", "$PWD", "${PWD}"} or any(
        word.startswith(alias) and word[len(alias) :] in {"/", "/.", "/./"}
        for alias in ("$PWD", "${PWD}")
    ):
        return True
    try:
        target = (Path(word) if Path(word).is_absolute() else workspace / word).resolve()
    except OSError:
        return False
    return target == workspace


def _code_fix_shell_has_broad_workspace_mutation(command: str, state: AgentState) -> bool:
    try:
        raw_words = shlex.split(command)
        workspace = state.workspace.resolve()
    except (OSError, ValueError):
        return False
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_broad_workspace_mutation(nested_command, state):
            return True
    words = _code_fix_shell_unwrap_simple_wrappers(raw_words)
    executable = PurePosixPath(words[0]).name if words else ""
    if executable not in _BROAD_MUTATORS:
        return False
    if executable in {
        "cmake",
        "aria2c",
        "curl",
        "make",
        "m4",
        "ninja",
        "npm",
        "npx",
        "patch",
        "pip",
        "pip3",
        "scp",
        "sftp",
        "uv",
        "wget",
        "7z",
        "ar",
        "bsdtar",
        "cpio",
        "docker",
        "ex",
        "gdb",
        "jar",
        "java",
        "jshell",
        "jjs",
        "lldb",
        "nerdctl",
        "podman",
        "vim",
        "nvim",
    }:
        return True
    if executable == "go":
        return True
    if executable == "tar" and any(
        word in {"--extract", "--get"}
        or word.startswith("--extract=")
        or word.startswith("--get=")
        or word == "--checkpoint-action"
        or word.startswith("--checkpoint-action=")
        or (word.startswith("-") and not word.startswith("--") and "x" in word[1:])
        for word in words[1:]
    ):
        return True
    if executable == "unzip" and not any(
        word in {"-l", "--list", "-t", "--test"} for word in words[1:]
    ):
        return True
    attached_targets: list[str] = []
    if executable == "tar":
        attached_targets.extend(
            word.split("=", 1)[1]
            for word in words[1:]
            if word.startswith("--directory=")
        )
        attached_targets.extend(word[2:] for word in words[1:] if word.startswith("-C") and word != "-C")
    elif executable == "unzip":
        attached_targets.extend(
            word.split("=", 1)[1]
            for word in words[1:]
            if word.startswith("--extract-dir=")
        )
        attached_targets.extend(word[2:] for word in words[1:] if word.startswith("-d") and word != "-d")
    if any(_code_fix_shell_word_is_workspace_root(word, workspace) for word in attached_targets):
        return True
    for word in words[1:]:
        if word == "--" or word.startswith("-"):
            continue
        if _code_fix_shell_word_is_workspace_root(word, workspace):
            return True
    return False


def _code_fix_shell_has_git_mutation(command: str) -> bool:
    for nested in re.finditer(
        r"\$\(([^()]*)\)|`([^`]*)`|\(([^()]*)\)|\{([^{}]*)\}", command
    ):
        nested_command = (
            nested.group(1) or nested.group(2) or nested.group(3) or nested.group(4) or ""
        )
        if nested_command and _code_fix_shell_has_git_mutation(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    words = _code_fix_shell_strip_assignments(words)
    if not _code_fix_shell_is_read_only(command) and any(
        match.group(1).lower() not in _GIT_READ_ONLY_COMMANDS
        for match in _GIT_COMMAND_ANYWHERE.finditer(command)
    ):
        return True
    if any(
        match.group(1).lower() not in _GIT_READ_ONLY_COMMANDS
        for match in _GIT_MUTATION_AFTER_SEPARATOR.finditer(command)
    ):
        return True
    executable = PurePosixPath(words[0]).name if words else ""
    if executable == "git":
        return len(words) >= 2 and words[1] not in _GIT_READ_ONLY_COMMANDS
    if executable not in _SHELL_WRAPPERS:
        return False
    for index, word in enumerate(words[:-1]):
        if PurePosixPath(word).name == "git" and words[index + 1] not in _GIT_READ_ONLY_COMMANDS:
            return True
    if any(
        match.group(1) not in _GIT_READ_ONLY_COMMANDS
        for word in words
        for match in [re.search(r"(?<![a-z0-9_./-])git\s+([a-z][a-z0-9-]*)(?=\s|$)", word, re.I)]
        if match
    ):
        return True
    return False


def _code_fix_shell_has_git_external_diff(command: str) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_git_external_diff(nested_command):
            return True
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    for index, word in enumerate(words):
        if PurePosixPath(word).name != "git":
            continue
        tail = words[index + 1 :]
        if any(option in tail for option in {"--ext-diff", "--textconv", "--submodule=diff"}):
            return True
        if any(
            option == "--submodule" and next_option == "diff"
            for option, next_option in zip(tail, tail[1:])
        ):
            return True
    return False


def _code_fix_git_config_execution_modes(state: AgentState) -> tuple[bool, bool, bool]:
    """Read local Git config markers without invoking Git or any configured hook."""
    try:
        git_marker = state.workspace.resolve() / ".git"
    except OSError:
        return False, False, False
    config_roots: list[Path] = []
    try:
        if git_marker.is_file():
            marker_text = git_marker.read_text(encoding="utf-8", errors="ignore")
            gitdir_line = next(
                (line for line in marker_text.splitlines() if line.lower().startswith("gitdir:")),
                "",
            )
            if gitdir_line:
                gitdir = Path(gitdir_line.split(":", 1)[1].strip())
                worktree_gitdir = (
                    (git_marker.parent / gitdir).resolve()
                    if not gitdir.is_absolute()
                    else gitdir
                )
                config_roots.append(worktree_gitdir)
                commondir_file = worktree_gitdir / "commondir"
                if commondir_file.is_file():
                    common = Path(
                        commondir_file.read_text(encoding="utf-8", errors="ignore").strip()
                    )
                    config_roots.append(
                        (worktree_gitdir / common).resolve()
                        if not common.is_absolute()
                        else common
                    )
        elif git_marker.is_dir():
            config_roots.append(git_marker)
    except OSError:
        return False, False, False
    config_paths: set[Path] = set()
    for root in config_roots:
        config_paths.add(root / "config")
        config_paths.add(root / "config.worktree")
        try:
            config_paths.update(root.rglob("config"))
            config_paths.update(root.rglob("config.worktree"))
        except OSError:
            continue
    has_external = False
    has_textconv = False
    has_unsafe_config = False
    for config_path in config_paths:
        try:
            text = config_path.read_text(encoding="utf-8", errors="ignore")[:1_000_000]
        except OSError:
            continue
        has_external |= re.search(
            r"(?im)^\s*(?:external|command|clean|smudge|process)\s*=", text
        ) is not None
        has_textconv |= re.search(r"(?im)^\s*textconv\s*=", text) is not None
        has_unsafe_config |= re.search(
            r"(?im)^\s*(?:path|includeif|fsmonitor)\s*=", text
        ) is not None
    return has_external, has_textconv, has_unsafe_config


def _code_fix_shell_has_configured_git_execution(command: str, state: AgentState) -> bool:
    for nested_command in _code_fix_shell_c_payloads(command):
        if _code_fix_shell_has_configured_git_execution(nested_command, state):
            return True
    has_external, has_textconv, has_unsafe_config = _code_fix_git_config_execution_modes(state)
    if not has_external and not has_textconv and not has_unsafe_config:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    for index, word in enumerate(words):
        if PurePosixPath(word).name != "git" or index + 1 >= len(words):
            continue
        subcommand = words[index + 1]
        tail = words[index + 1 :]
        if has_unsafe_config:
            return True
        if subcommand == "diff":
            if has_external and "--no-ext-diff" not in tail:
                return True
            if has_textconv and "--no-textconv" not in tail:
                return True
        elif subcommand == "show" and has_textconv and "--no-textconv" not in tail:
            return True
        elif subcommand == "log" and has_textconv and any(
            option in tail for option in {"-p", "--patch"}
        ) and "--no-textconv" not in tail:
            return True
    return False


def _code_fix_shell_refusal(command: str, state: AgentState) -> str:
    """Reject an explicit shell mutation aimed at the evaluator, not source edits."""
    try:
        workspace_text = str(state.workspace.resolve()).lower()
    except OSError:
        workspace_text = ""
    command_for_path_scan = command.lower().replace(workspace_text, "") if workspace_text else command.lower()
    if (
        _code_fix_shell_has_compound_syntax(command)
        or _code_fix_shell_has_find_mutation(command)
        or _code_fix_shell_has_glob_mutation(command)
        or _code_fix_shell_has_resolved_unsafe_executable(command, state)
        or _code_fix_shell_has_unknown_local_script_runner(command, state)
        or _code_fix_shell_has_compiler_plugin(command)
        or _code_fix_shell_has_git_mutation(command)
        or _code_fix_shell_has_git_external_diff(command)
        or _code_fix_shell_has_configured_git_execution(command, state)
        or _code_fix_shell_has_xargs(command)
        or _code_fix_shell_has_broad_workspace_mutation(command, state)
        or _code_fix_shell_has_eval(command)
        or _code_fix_shell_has_trap(command)
        or _code_fix_shell_has_env_split_string(command)
        or _code_fix_shell_has_opaque_runner(command)
        or _code_fix_shell_has_startup_hook_assignment(command)
        or _code_fix_shell_has_python_script(command, state.workspace)
        or _code_fix_shell_has_script_invocation(command, state.workspace)
        or _code_fix_shell_has_pytest_plugin_option(command)
        or _code_fix_shell_has_evaluator_redirect(command, state)
        or _code_fix_shell_has_evaluator_operand(command, state)
    ):
        return (
            "code-fix refused: this shell command may mutate an evaluator/test or "
            "repository control path. Keep tests, fixtures, expected answers and "
            ".git metadata immutable; use a read-only single command or edit "
            "production source/config instead."
        )
    if (
        _code_fix_shell_is_read_only(command)
        or not _EVALUATOR_TOKEN.search(command_for_path_scan)
    ):
        return ""
    return (
        "code-fix refused: this shell command may mutate an evaluator/test or "
        "repository control path. Keep tests, fixtures, expected answers and .git "
        "metadata immutable; use a read-only test command or edit production "
        "source/config instead."
    )


def _execute(
    tool: str,
    args: dict[str, object],
    state: AgentState,
    *,
    playbook: Playbook,
) -> tools.ToolResult:
    """Run one action. Every failure comes back as a `ToolResult`, never as a raise."""
    secrets = state.secrets

    if tool == prompt.RUN_SHELL:
        command = str(args["command"])
        if playbook.name == "code-fix":
            refusal = _code_fix_shell_refusal(command, state)
            if refusal:
                return tools.ToolResult(name=tool, ok=False, detail=refusal)
        # A second of slack before the command so a file `cp` writes in the same coarse
        # filesystem tick still counts as touched (mtime >= since), never missed into a
        # false refusal.
        since = time.time() - 1.0
        result = tools.run_shell(
            command,
            cwd=state.workspace,
            timeout_s=state.budget.tool_timeout_s(),
            secrets=secrets,
        )
        if result.ok:
            state.read_paths.update(_read_paths_from_shell(command, state))
            if _shell_can_change(command):
                # A file the command created or modified (echo >, cp, tee, sed -i) is
                # known to the run: overwriting it later is not blind. pytest or a launch
                # is a mutating shell too and may mark a log or sqlite file touched --
                # harmless to the guard, only surprising in a trace.
                state.read_paths.update(_workspace_files_touched_since(state.workspace, since))
        return result
    if tool == prompt.READ_RANGE:
        target = Path(str(args["path"]))
        result = tools.read_range(target, start_line=int(args["start_line"]), secrets=secrets)
        if result.ok:
            try:
                state.read_paths.add(target.resolve())
            except OSError:
                pass
        return result
    if tool == prompt.LIST_DIR:
        return tools.list_dir(Path(str(args["path"])), secrets=secrets)
    if tool == prompt.SEARCH:
        return tools.search(str(args["pattern"]), Path(str(args["path"])), secrets=secrets)

    target = Path(str(args["path"]))
    if playbook.name == "code-fix" and tool in {prompt.WRITE_FILE, prompt.WRITE_JSON, prompt.REPLACE_TEXT}:
        refusal = _evaluator_path_refusal(target, state)
        if refusal:
            return tools.ToolResult(name=tool, ok=False, detail=refusal)
    if playbook.name == "code-fix" and tool == prompt.WRITE_FILE:
        refusal = _unread_source_refusal(target, state)
        if refusal:
            return tools.ToolResult(name=tool, ok=False, detail=refusal)
    if tool == prompt.WRITE_JSON:
        result = tools.write_json(
            target,
            args.get("data"),
            workspace=state.workspace,
            permitted=state.named_paths,
            secrets=secrets,
        )
    elif tool == prompt.REPLACE_TEXT:
        result = tools.replace_text(
            target,
            str(args["old"]),
            str(args["new"]),
            workspace=state.workspace,
            permitted=state.named_paths,
            secrets=secrets,
        )
    else:
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


def _verify_exact_write(
    state: AgentState,
    verify: VerifyFn,
    args: dict[str, object],
    *,
    playbook: Playbook,
) -> tuple[Path, VerifyResult] | None:
    """Check a successful write immediately when the instruction is an exact-file task.

    The ordinary verifier is intentionally not a post-write success gate: a report or
    source edit must still be finished by the model and checked according to its
    playbook. The conservative exact-file contract is different only for the
    unclassified/general one-step task: its path and bytes come from the instruction,
    so a successful `write_file` can be checked without another model turn. A failed
    check is only an observation; the model may correct the file and try again. A
    specialized playbook must still perform its investigation or repair procedure,
    even if its first artifact happens to look like an exact-file answer.
    """
    if playbook.name != "general" or result_contract.exact_file_requirement(state.instruction) is None:
        return None
    deliverable = Path(str(args.get("path", "")))
    verify_started = state.start_phase("verify")
    verdict = verify(state, deliverable=deliverable, command="")
    state.finish_phase("verify", verify_started)
    state.record(
        "verify",
        verdict.stage,
        verdict.detail or "passed",
        exit_code=0 if verdict.ok else 1,
    )
    return deliverable, verdict


def _source_changes(
    state: AgentState, playbook: Playbook
) -> result_progress.SourceChanges | None:
    """Return source progress only for code-fix, where artifacts are too broad."""
    if playbook.name != "code-fix":
        return None
    if state.source_snapshot is None:
        return result_progress.SourceChanges(
            (), False, "no production source snapshot was captured before the run"
        )
    return result_progress.changed_sources(state.workspace, state.source_snapshot)


def _error_code(result: tools.ToolResult) -> str:
    """Short stable label for a failure, so repeats of the same failure look alike."""
    if result.exit_code is not None:
        return f"exit:{result.exit_code}"
    return f"error:{result.detail.split(':')[0][:40]}"


def _shell_can_change(command: str) -> bool:
    """Conservatively decide whether a shell action invalidates old observations."""
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    if not words:
        return False
    if any(
        marker in command for marker in ("$((", "$(", "`", ";", "&&", "||", "|", ">", ">>", "<", "<<", "&")
    ):
        return True
    if any(word in {"-delete", "-exec", "-execdir"} for word in words):
        return True
    return PurePosixPath(words[0]).name not in READ_ONLY_SHELLS


def _endpoint_is_slow_or_thinking(reply: LlmReply | LlmError, llm: LlmPort) -> bool:
    """Whether an endpoint slow or thinking enough that the 90s call clamp hurts.

    Three loop-observable signals, matching the F4 design: the client detected a thinking
    endpoint (a reasoning field or a length cutoff, tracked inside `agent.llm`), the reply
    is the truncated-by-length error, or a call timed out with no payload -- the last is
    the only signal available on the very first call, which is where the 90s clamp bit.
    """
    if getattr(llm, "_thinking_endpoint", False):
        return True
    if isinstance(reply, LlmError):
        reason = reply.reason.lower()
        return "truncated by length" in reason or "timeout" in reason
    return False


def _advances_state_epoch(tool: str, args: dict[str, object]) -> bool:
    """Whether an executed action may have changed files or a local service."""
    if tool in {prompt.WRITE_FILE, prompt.REPLACE_TEXT}:
        return True
    return tool == prompt.RUN_SHELL and _shell_can_change(str(args.get("command", "")))


def _finalise(
    state: AgentState,
    verify: VerifyFn,
    stop_reason: str,
    summary: str,
    verdict: VerifyResult | None,
    observations: list[str],
    *,
    playbook: Playbook,
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
    source_changes = _source_changes(state, playbook)
    deliverable = None
    if source_changes is not None and source_changes.known and source_changes.paths:
        # A shell edit is a real source change but is not added to `artifacts`, while a
        # preceding README/report may be. Prefer the changed production path so the
        # final trace and injected verifier inspect the repair, not the notes.
        deliverable = state.workspace / source_changes.paths[-1]
    elif state.artifacts:
        deliverable = state.artifacts[-1]
    artifact_only_code_fix = (
        source_changes is not None and source_changes.known and not source_changes.paths
    )
    if deliverable is not None and stop_reason != "verify-failed" and not artifact_only_code_fix:
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
