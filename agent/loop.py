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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import json
import dataclasses
import os
import re
import shlex
import stat
import time

from agent import detectors, limits, prompt, result_contract, result_progress, sources, tools
from agent.playbooks import Playbook
from agent.ports import LlmError, LlmPort, LlmReply
from agent.preflight import EnvironmentBrief
from agent.redact import redact
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

BATCH_READ_TOOLS = frozenset({prompt.READ_RANGE, prompt.LIST_DIR, prompt.SEARCH})
"""Tools an extra call of the same reply is allowed to run inside one step.

The three that cannot change a file or a service, and nothing else. `run_shell` is
absent even though most of what a model runs through it only reads: whether a command
reads or writes is a guess here (`READ_ONLY_SHELLS` says as much about itself), and a
guess is not a basis for running an unasked-for second action in the same step.
`write_file`, `write_json`, `replace_text` and `finish` are absent because they are
exactly what the one-action-per-step rule exists for.
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
    # Consecutive calls that failed in a way that says the endpoint cannot be used. A
    # call that merely timed out is counted separately -- see `_llm_failure_kind`.
    llm_failures = 0
    # Whether the NEXT request is a focused one: a narrowed history plus one line asking
    # for an action and no reasoning (`prompt.FOCUS_NOTICE`). Set only by a reply that
    # reasoned past the raised output ceiling without emitting anything, and only when
    # the call that did it was not itself focused.
    focus_next = False
    # Index in `observations` of the newest entry that is a tool result rather than
    # something this loop said to itself. Only the narrowing of a focused request reads
    # it, and it reads it so that the narrowing cannot drop the last observed fact: the
    # difference between asking the model to act on what it saw and asking it to invent.
    last_evidence: int | None = None
    # Successful tool results carrying content. Zero means the run has observed nothing,
    # so anything it writes would be invented rather than reported.
    grounded = 0
    blocks_in_a_row = 0
    # Per-run counters for the stale-service gate: how many scans the run has paid for
    # and how many `finish` calls it has refused over one. Locals, like every other
    # per-run counter here -- nothing about this belongs in `AgentState`, which outlives
    # the loop and is read by the reporter.
    stale_service_checks = 0
    stale_service_refusals = 0
    vacuous_verify_refusals = 0
    last_verdict: VerifyResult | None = None
    stop_reason = ""
    summary = ""

    while state.budget.may_act() or _extend_steps(state):
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
            if not grounded:
                # P-B1: the same hint fired on a run that had not read a single file, and
                # the model answered it 6/6 times with fabricated values -- content that
                # changes every run, so no detector catches it. "Your best attempt" is
                # only meaningful once something has been observed; before that the
                # cheapest correct action is to look, and saying so costs the same step.
                checkpoint = (
                    "[бюджет] запас шагов и времени на исходе, а на диске нет "
                    "проверяемого результата и ни одного успешного наблюдения. "
                    "Если ответ целиком задан в условии (точное содержимое файла), "
                    "запиши его через write_file сейчас. Иначе не выдумывай значения: "
                    "сначала одним действием посмотри источники, которые называет "
                    "задание (list_dir, read_range, search), и пиши ответ по тому, что "
                    "реально прочитал."
                )
            elif playbook.name == "code-audit":
                checkpoint += (
                    " Лучший вариант — то, что подтверждают твои наблюдения: перенеси в "
                    "отчёт прочитанные строки и файлы. В evidence приведи саму строку "
                    "кода из наблюдения, с путём и номером, а не пересказ. Пустой "
                    "список findings — это утверждение, что прочитанный код безопасен."
                )
            elif playbook.name == "code-fix":
                # Replaces the sentence above instead of extending it, which is the one
                # place this branch differs from the audit tail. For a code-fix the
                # deliverable *is* the edit, and "write your best attempt with
                # write_file now" points at the wrong tool: the measured episode had
                # read the vulnerable handler eleven times and never issued
                # `replace_text`. Anchor and restart are the two things 5 of 5 failed
                # edits got wrong (`EDIT_ECHO_CONTEXT_LINES`, code-fix playbook).
                checkpoint = (
                    "[бюджет] запас шагов и времени на исходе, а правки на диске нет. "
                    "Лучший вариант здесь — сама правка production-кода: если уязвимое "
                    "выражение уже найдено, примени к нему replace_text сейчас, взяв якорем "
                    "всё выражение целиком вместе с вызовом, который использует значение; "
                    "если ещё не найдено — найди его одним search и правь. README, отчёт или "
                    "тест сами по себе не являются исправлением. Потом перезапусти сервис так, "
                    "как это делает его entrypoint, и повтори репро-проверку; не перечитывай "
                    "уже прочитанные файлы."
                )
            observations.append(checkpoint)

        plan_started = state.start_phase("plan")
        # Anchored, not sliding: the base moves only when the kept history stops
        # fitting its budget, so every other request extends the previous one and the
        # server pays for the new tail alone.
        history_base = prompt.history_base(observations, history_base)
        # Focus is consumed here, before the reply is read: whatever comes back, the next
        # request is an ordinary one unless a fresh runaway asks for another. That is what
        # makes a focus loop impossible rather than merely unlikely.
        focused, focus_next = focus_next, False
        base = history_base
        if focused:
            # Narrowed for this call only -- `history_base` keeps its anchor, so the next
            # ordinary request is the one that would have been sent anyway.
            base = prompt.focus_base(observations, history_base, last_evidence)
            state.record("budget", "focus", "focused retry after a truncated reasoning reply")
        reply = llm.complete_structured(
            prompt.build_messages(system, observations, base, focus=focused),
            schema=prompt.ACTION_SCHEMA,
            # The same ceiling on a focused call as on any other. The raised output
            # budget is `agent.llm`'s and is sticky, and the model needs the room: what
            # ran away was the prompt, not the space to answer in. Asking for less would
            # truncate the answer we are trying to get; asking for more buys a longer
            # runaway.
            max_output_tokens=limits.LLM_MAX_OUTPUT_TOKENS,
            timeout_s=max(1.0, state.budget.llm_timeout_s()),
        )
        state.finish_phase("plan", plan_started)

        if not state.budget.allow_long_llm and _endpoint_is_slow_or_thinking(reply, llm):
            # A thinking endpoint: lift the 90s pre-checkpoint clamp and raise the per-call
            # ceiling, so the next call gets the time a reasoning reply needs instead of
            # being truncated mid-thought. The call that just timed out is already lost --
            # this saves the rest of the run (F4). Set on the first signal, whether that
            # signal arrived as a reply or as a timeout.
            state.budget.allow_long_llm = True
            state.record("budget", "allow-long-llm", "thinking endpoint; lifting the call clamp")

        if isinstance(reply, LlmError):
            kind = _llm_failure_kind(reply)
            state.budget.add_usage(reply)  # a failed attempt was still generated and billed
            state.record("llm", "error", reply.reason)
            observations.append(f"[step {step}] endpoint error: {reply.reason}")
            if kind == HARD_FAILURE:
                llm_failures += 1
                if llm_failures >= limits.MAX_CONSECUTIVE_LLM_FAILURES:
                    stop_reason = "llm-unusable"
                    summary = f"endpoint unusable after {llm_failures} failed calls: {reply.reason}"
                    break
                if reply.truncated_reasoning and not focused:
                    # The model reasoned to the ceiling and emitted nothing. Repeating the
                    # request buys the same non-answer -- measured four times in a row on
                    # the 35B stand -- so the next one is narrowed instead.
                    #
                    # Counted as the hard failure it is, and only once: the focused call
                    # replaces the second of the three logical calls the counter allows,
                    # it does not add a fourth. Worst case is therefore exactly today's
                    # worst case -- runaway, focused runaway, one more failure,
                    # `llm-unusable` -- which on that stand is 4 endpoint calls of ~70 s
                    # once the endpoint is already known to reason (the first logical call
                    # pays two, the raised retry and the thinking-off retry; the rest one
                    # each), or 5 from cold. ~280-350 s, unchanged either way. What the
                    # focus buys for those seconds is a request the model has not already
                    # failed to answer; the second of the three is also the cheapest of
                    # them, since its history is `limits.FOCUS_RELAY_KEEP` long.
                    #
                    # Decided here, audited where the request is actually built: the clock
                    # or the step cap can end the run in between, and a trace must not
                    # claim a retry nobody sent.
                    focus_next = True
            else:
                # A call that ran out of wall clock with nothing delivered says the
                # endpoint is slow, not broken, and the two must not end the run the same
                # way. `F4-30b-paired` measured the cost of conflating them: two
                # consecutive timeouts on a model that was still generating demoted the
                # run to `llm-unusable` -- a verdict about the endpoint -- while the
                # honest account is that the budget ran out. The clock, the step cap and
                # the shrinking per-call ceiling already bound this branch, so it cannot
                # spin: each timeout costs real seconds of the same budget that ends it.
                state.record("budget", "llm-slow", f"timed out without a payload: {reply.reason}")
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
            if (
                refusal
                and result_contract.exact_file_requirement(state.instruction) is None
                and vacuous_verify_refusals < limits.VACUOUS_VERIFY_MAX_REFUSALS
            ):
                vacuous_verify_refusals += 1
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

            if stale_service_checks < limits.SERVICE_SCAN_MAX_CHECKS:
                # Could this check have exercised the change at all? A process older
                # than the edit is running the code as it was before it. Observation
                # only: nothing here restarts, signals or starts anything.
                looked = _stale_service(state, source_changes)
                if looked is not None:
                    stale_service_checks += 1
                    kind, detail, hint = looked
                    if kind:
                        state.record("verify", kind, detail)
                    if (
                        hint
                        and stale_service_refusals < limits.SERVICE_SCAN_MAX_REFUSALS
                        and state.budget.spendable_s() >= limits.SERVICE_RESTART_MIN_SPARE_S
                    ):
                        stale_service_refusals += 1
                        observations.append(f"[step {step}] {hint}")
                        continue
                    if hint:
                        # Too late to act on it: a refusal now would end the run inside
                        # the reserve with the model's check never run. Audited, not
                        # enforced.
                        state.record("verify", "stale-service-late", "no spare time for a restart")

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
        if os.environ.get(limits.TRACE_TOOLS_ENV_NAME):
            # Diagnostic only, off unless explicitly enabled: the loop otherwise prints
            # phases but never which tool ran with what result, so a live zero cannot be
            # told from the outside. Output is already redacted by the tool. Never on in
            # a submission -- the runner sets none of our variables.
            #
            # A local name, not `summary`: this used to reuse the run's own summary
            # variable, so switching the trace on rewrote the sentence the Outcome would
            # later report. A diagnostic that changes the result is not a diagnostic.
            traced = (result.output or result.detail or "").replace("\n", " ")[:160]
            print(f"agent tool: step={step} {tool} ok={result.ok} {traced}", flush=True)
        body = result.output or result.detail
        if (
            _advances_state_epoch(tool, args)
            and (result.ok or tool == prompt.RUN_SHELL)
            # A copy of the previous action with the same observation mutated nothing
            # new. Keeping the epoch is what lets the repeat detectors count it: with a
            # fresh epoch per write, nineteen identical writes looked like nineteen
            # first writes (F4-7b-regression-4080).
            and not state.repeats_last_action(tool, args, body)
        ):
            state.advance_state_epoch()
        state.record(
            "tool",
            tool,
            result.detail or "ok",
            duration_s=result.duration_s,
            exit_code=result.exit_code,
        )
        state.observe(
            tool,
            args,
            body,
            ok=result.ok,
            error_code="" if result.ok else _error_code(result),
        )
        if result.ok and body.strip():
            # What the checkpoint hint below needs to know: whether this run has seen
            # anything at all. An action that succeeded and printed something is the
            # difference between reporting and inventing.
            grounded += 1
        echo = ""
        if result.ok and tool == prompt.REPLACE_TEXT:
            # The observation the edit was missing. Appended to the text the model reads
            # and not to `body`, so the detectors keep fingerprinting exactly what they
            # fingerprinted before.
            echo = _edit_echo(Path(str(args["path"])), str(args.get("new", "")), result, state)
        observations.append(
            f"[step {step}] {tool} -> {'ok' if result.ok else 'FAIL'}\n{body}{echo}".rstrip()
        )
        # Evidence, whether or not the action succeeded: a failed tool result is what the
        # model has to react to. Anything a focused request adds after this index -- the
        # batch's own entry, a refusal, a red verifier, a checkpoint hint -- travels with
        # it, because all of those are one-liners and this is the observation that costs.
        last_evidence = len(observations) - 1

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

        if reply.extra_actions:
            # The rest of the reply. Read-only, bounded, and inside the step the
            # primary action already paid for -- see `_batch_reads`.
            grounded += _batch_reads(
                state,
                reply.extra_actions,
                observations,
                step=step,
                playbook=playbook,
                visible_from=base,
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


def _extend_steps(state: AgentState) -> bool:
    """At the step cap, buy steps once with time the run would otherwise not spend.

    Measured on the Qwen3.6-35B-A3B stand (16.09): a call costs 2-4 s, so an episode
    exhausts `MAX_STEPS` in 90-130 s and stops with 400+ s of a 600 s budget unused.
    Both zeros of the six-repeat candidate run ended that way, and one of them had
    located the vulnerable handler and simply not edited it yet. On a slow endpoint the
    cap and the clock arrive together, so the threshold below is never met and this
    changes nothing -- which is the point: it is a fast-path fix and must stay one.

    Two questions, neither of which the model answers. The budget says whether there is
    time worth spending (`Budget.may_extend_steps`), and the action history says whether
    the run is still moving (`AgentState.shows_progress`). A run circling over files it
    has already read fails the second and gets nothing: the extension exists to let a
    run that is close finish, not to pay for more laps of the same loop.
    """
    if not state.budget.may_extend_steps() or not state.shows_progress():
        return False
    granted = state.budget.extend_steps()
    if not granted:
        return False
    state.record(
        "budget",
        "steps-extended",
        f"+{granted} steps at the cap: {state.budget.spendable_s():.0f}s of "
        f"{state.budget.total_s:.0f}s still spendable and the run is still moving",
    )
    return True


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
and `write_file`'s content, because `content: ""` is a legitimate request for an empty
file; a write_file with no content key at all is refused separately, see below.
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

    if tool == prompt.WRITE_FILE and "content" not in fields:
        # `content: ""` is a request for an empty file and is honoured. A write_file
        # with no content key at all is a reply that lost its payload -- seen after a
        # truncated or schema-constrained turn (REVIEW-public-q36: the report file was
        # written empty and the run scored 0) -- and an empty deliverable is never what
        # the model meant.
        return None, (
            "write_file needs `content` (the exact text to write); the action had no "
            "content key, so nothing was written. Send it again with `content`, or "
            'with `content: ""` if an empty file is really what you want.'
        )

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


_CURL_WRITING_LONG = (
    "--output", "--remote-name", "--remote-header-name", "--output-dir", "--dump-header",
    "--cookie-jar", "--config", "--trace", "--libcurl", "--stderr", "--etag-save",
    "--hsts", "--alt-svc", "--create-dirs", "--upload-file",
)
_CURL_WRITING_SHORT = frozenset("oODcKTJ")


def _curl_can_write(arguments: Sequence[str]) -> bool:
    """Whether this curl invocation can put bytes into a local file.

    A plain HTTP probe of the service writes only to stdout, and after a restart it is
    the direct way to see that the port answers. Anything that names an output, a
    header/cookie dump, a config file, an upload or a `file:` URL is still refused, as
    is a `%output{}` write-out; so is an option cluster we cannot read letter by letter.
    """
    for word in arguments:
        lowered = word.lower()
        if "file:" in lowered or "%output{" in lowered:
            return True
        if word.startswith("--"):
            if word.split("=", 1)[0].startswith(_CURL_WRITING_LONG):
                return True
        elif word.startswith("-") and len(word) > 1:
            if _CURL_WRITING_SHORT.intersection(word[1:]):
                return True
    return False


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
    if executable == "curl" and raw_words[0] == "curl" and not _curl_can_write(words[1:]):
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
    if _background_server(command):
        return ""
    parts = _code_fix_shell_parts(command, state)
    if parts is not None and parts != [command]:
        refusals = [_code_fix_shell_refusal(part, state) for part in parts]
        return next((refusal for refusal in refusals if refusal), "")
    if _curl_probe(command) or _python_probe(command, state):
        return ""
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


_FD_DUPLICATION = re.compile(r"(?:(?<=\s)|^)\d*>&\d+(?=\s|$)")
# What may read a pipe: filters that treat stdin as data. A shell, python or anything
# else on the right of `|` would run the stream as a program the check never saw.
_PIPE_FILTERS = frozenset({
    "awk", "base64", "cat", "column", "cut", "egrep", "fgrep", "grep", "head", "jq", "less",
    "md5sum", "more", "nl", "od", "rev", "sed", "sha256sum", "sort", "strings", "tail", "tee",
    "tr", "uniq", "wc", "xxd",
})


def _code_fix_shell_parts(command: str, state: AgentState) -> list[str] | None:
    """The simple commands of `a | b`, `a && b`, `a || b` or `a; b`, each checked alone.

    A composition writes nowhere its parts do not, so a pipe into `head` or a leading
    `cd <workspace> &&` is no reason to refuse. None -- keep the whole-command check --
    for anything a part-by-part check cannot see through: substitution, a subshell or
    group, stdin redirection, a newline, a lone `&`, or a `cd` anywhere but first and
    into the workspace itself (relative paths in later parts would resolve elsewhere).
    `2>&1`-style fd duplication writes no file and is dropped from each part. A part
    reading a pipe must be a plain filter (`_PIPE_FILTERS`).
    """
    parts: list[str] = []
    piped: list[bool] = [False]
    start = index = 0
    quote = ""
    while index < len(command):
        char = command[index]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = ""
            elif char == "`" or command.startswith("$(", index):
                return None
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char in "`\n<(){}" or command.startswith("$(", index):
            return None
        elif char in ";|&" and not command.startswith((">&", "&>"), index - (char == "&")):
            width = 2 if command.startswith(("&&", "||"), index) else 1
            if char == "&" and width == 1:
                return None
            parts.append(command[start:index])
            piped.append(char == "|" and width == 1)
            index += width
            start = index
            continue
        index += 1
    if quote:
        return None
    parts = [_FD_DUPLICATION.sub("", part).strip() for part in [*parts, command[start:]]]
    if not all(parts):
        return None
    try:
        words = [shlex.split(part) for part in parts]
    except ValueError:
        return None
    if any(part_words[0] == "cd" for part_words in words[1:]):
        return None
    if any(
        reads_pipe and PurePosixPath(part_words[0]).name not in _PIPE_FILTERS
        for reads_pipe, part_words in zip(piped, words)
    ):
        return None
    if words[0][0] == "cd":
        try:
            into_workspace = len(words[0]) == 2 and (
                (state.workspace / words[0][1]).resolve() == state.workspace.resolve()
            )
        except OSError:
            into_workspace = False
        if not into_workspace or len(parts) == 1:
            return None
        parts = parts[1:]
    return parts


def _curl_probe(command: str) -> bool:
    """A lone curl that writes only to stdout: its URL is not a path, whatever it spells."""
    if any(_code_fix_shell_operator_flags(command)):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return bool(words) and words[0] == "curl" and not _curl_can_write(words[1:])


_PYTHON_SPAWN = re.compile(
    r"""(?x)
    \bsubprocess\b | \bctypes\b | \bpty\b | \bbuiltins\b
  | \bos\.(?:system|popen|exec\w*|spawn\w*|fork|kill|killpg)\b
  | \bshutil\.rmtree\b
  | \b(?:exec|eval|compile|__import__)\s*\(
    """
)
"""What a Python probe may not spell up front: spawning another program, hiding a call
behind `exec`/`eval`, or a recursive delete. Everything else -- reads, `os.path`,
`str.replace`, `getattr`, sqlite, zipfile -- is decided by effect, not by text: the
probe runs under `_guard_protected_paths`, which snapshots the evaluator, `.git` and
`/logs` before the command and restores the evaluator after it if anything changed. A
text denylist cannot be made tight (a variable file mode, `chr()`-built names, a
side-effecting import all pass any regex), so it is kept to the cases the snapshot
cannot undo.
"""

_PROTECTED_SNAPSHOT_MAX_FILES = 400
_PROTECTED_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
_EXTRA_PROTECTED_ROOTS = (Path("/logs"),)


def _protected_files(state: AgentState) -> list[Path] | None:
    """Every file a probe must leave alone; None when there are too many to snapshot."""
    found: list[Path] = []
    total = 0
    roots = [state.workspace, *[root for root in _EXTRA_PROTECTED_ROOTS if root.is_dir()]]
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        for current, dirs, files in os.walk(root):
            here = Path(current)
            dirs[:] = [d for d in dirs if d not in _INODE_SCAN_SKIP]
            for name in files:
                path = here / name
                if root in [r.resolve() for r in _EXTRA_PROTECTED_ROOTS if r.is_dir()] or (
                    _is_evaluator_path(path, state) or _is_repository_control_path(path, state)
                ):
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        total += path.stat().st_size
                    except OSError:
                        continue
                    found.append(path)
                    if len(found) > _PROTECTED_SNAPSHOT_MAX_FILES or total > _PROTECTED_SNAPSHOT_MAX_BYTES:
                        return None
    return found


def _protected_snapshot(state: AgentState) -> dict[Path, bytes] | None:
    files = _protected_files(state)
    if files is None:
        return None
    snapshot: dict[Path, bytes] = {}
    for path in files:
        try:
            snapshot[path] = path.read_bytes()
        except OSError:
            return None
    return snapshot


def _restore_protected(snapshot: dict[Path, bytes], state: AgentState) -> list[str]:
    """Put the snapshot back; return what had changed (empty when nothing did)."""
    changed: list[str] = []
    for path, content in snapshot.items():
        try:
            same = path.is_file() and not path.is_symlink() and path.read_bytes() == content
        except OSError:
            same = False
        if same:
            continue
        changed.append(str(path))
        try:
            if path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError:
            pass
    after = _protected_files(state) or []
    for path in after:
        if path not in snapshot:
            changed.append(str(path))
            if not _is_repository_control_path(path, state):
                try:
                    path.unlink()
                except OSError:
                    pass
    return changed


_PYTHON_PROBE_MAX_BYTES = 65536


_WORKSPACE_CD = re.compile(r"^\s*cd\s+(?P<dir>[^\s;&|<>$`(){}\\'\"]+)\s*&&\s*(?P<rest>.+)$", re.DOTALL)


def _strip_workspace_cd(command: str, state: AgentState) -> str:
    """`cd <dir inside the workspace> && <rest>` -> `<rest>`, else the command unchanged.

    The model prefixes most probes with `cd /app &&`; the directory changes nothing the
    policy cares about when it lies inside the workspace, and a multi-line `-c` payload
    defeats the generic part splitter, so the probe strips the prefix itself.
    """
    match = _WORKSPACE_CD.match(command)
    if not match:
        return command
    try:
        root = state.workspace.resolve()
        target = Path(match["dir"])
        (target if target.is_absolute() else root / target).resolve().relative_to(root)
    except (OSError, ValueError):
        return command
    return match["rest"]


def _python_probe(command: str, state: AgentState) -> bool:
    """A lone `python -c <code>` or `python <script inside the workspace>` that only reads.

    The code-fix policy refused every Python one-liner and every local script outright,
    so on a fix task without a test suite the model's own check -- import the patched
    module, call it, assert -- was refused on every attempt (EXTRA-taxonomy: fix tasks
    spent 6-12 refusals per episode). The policy's job is to keep the evaluator
    immutable, not to forbid reading the program under repair. A snippet or script that
    spawns nothing and names no evaluator path goes through like a curl probe does; what
    it does to the evaluator is checked by effect afterwards (`_guard_protected_paths`)
    and undone, and one violation ends the privilege for the run.
    """
    command = _strip_workspace_cd(command, state)
    if any(_code_fix_shell_operator_flags(command)):
        return False
    try:
        words = _code_fix_shell_unwrap_simple_wrappers(shlex.split(command))
    except ValueError:
        return False
    if len(words) < 2 or not _code_fix_is_python_executable(words[0], state.workspace):
        return False
    options = [word for word in words[1:] if word.startswith("-")]
    if any(option not in {"-c", "-B", "-u", "-I", "-S", "-E", "-s"} for option in options):
        return False
    operands = [word for word in words[1:] if not word.startswith("-")]
    if len(operands) != 1:
        return False
    if "-c" in words:
        if words[words.index("-c") + 1 :] != [operands[0]]:
            return False
        text = operands[0]
    else:
        script = Path(operands[0])
        try:
            root = state.workspace.resolve()
            target = (script if script.is_absolute() else root / script).resolve()
            target.relative_to(root)
            if _is_evaluator_path(target, state) or not target.is_file():
                return False
            if target.stat().st_size > _PYTHON_PROBE_MAX_BYTES:
                return False
            text = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return False
    if state.python_probes_disabled or _PYTHON_SPAWN.search(text):
        return False
    for token in re.findall(r"[\w./-]+", text):
        if "/" in token or token.endswith(".py"):
            try:
                if _is_evaluator_path(Path(token), state) or _is_repository_control_path(Path(token), state):
                    return False
            except (OSError, ValueError):
                return False
    return True


_SERVER_EXECUTABLES = frozenset({"uvicorn", "gunicorn", "hypercorn", "daphne", "waitress-serve"})
_PYTHON = re.compile(r"python(3(\.\d+)?)?$")
_BACKGROUND_SERVER = re.compile(
    r"^nohup ([^;&|<>$`(){}\\\n'\"]+?) *> */tmp/[\w.-]+ +2>&1( +</dev/null)? *&$"
)


def _is_server(words: Sequence[str]) -> bool:
    """Whether an argv starts a known long-running Python web server.

    Deliberately short: these are the launchers whose command line says "serve until
    killed" with no doubt, directly or through `python -m`. Anything else is not
    guessed at.
    """
    if not words:
        return False
    name = PurePosixPath(words[0]).name
    rest = list(words[1:])
    if _PYTHON.match(name) and len(rest) >= 2 and rest[0] == "-m":
        name, rest = rest[1], rest[2:]
    if name in _SERVER_EXECUTABLES:
        return True
    return name == "flask" and bool(rest) and rest[0] == "run"


def _foreground_server(command: str) -> bool:
    """A single simple command that runs a server in the foreground.

    It never returns: `run_shell` waits out its whole timeout (up to 120 s) and then
    kills it, so the model pays two minutes for a service that is gone afterwards.
    """
    if _code_fix_shell_operator_flags(command)[0]:
        return False
    try:
        return _is_server(shlex.split(command))
    except ValueError:
        return False


def _background_server(command: str) -> bool:
    """`nohup SERVER >/tmp/LOG 2>&1 [</dev/null] &` and nothing more.

    The one compound shape code-fix lets through: a known server argv with no shell
    syntax in it, its output in a file under /tmp, backgrounded alone. It cannot write
    into the workspace, and without it a model told to restart a service has no command
    that is both allowed and not killed by the tool timeout.
    """
    found = _BACKGROUND_SERVER.match(command.strip())
    if not found:
        return False
    try:
        return _is_server(shlex.split(found.group(1)))
    except ValueError:
        return False


_LONE_AMPERSAND = re.compile(r"(?<![&>|<])&(?![&>])")
_STDOUT_REDIRECT = re.compile(r"(?:^|[^0-9&])>(?!&)|&>|1>(?!&)")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


def _background_holds_pipe(command: str) -> bool:
    """Whether a job this command puts in the background still writes to our pipe.

    `run_shell` reads the command's output until every writer closes it, so `app &` or
    `app & sleep 2; curl ...` holds the call for the whole tool timeout (up to 120 s),
    and the timeout then reaps the session -- the service the model just started dies
    with it. Measured in the target image (dash) and on bash alike. A job whose segment
    sends stdout to a file (`>log 2>&1`) does not hold the pipe; `2>&1` alone does and is left alone. Quoted text
    is blanked first, so `echo 'a & b'` is not a job.
    """
    segments = _LONE_AMPERSAND.split(_QUOTED.sub("''", command))
    if len(segments) < 2:
        return False
    return any(not _STDOUT_REDIRECT.search(segment) for segment in segments[:-1])


_READ_FOOTER = "\n[read_range source="


def _number_read_lines(output: str, start_line: int) -> str:
    """Prefix each line of a `read_range` body with its number: `6: RELAY_API_TOKEN = …`.

    Only the code-audit playbook gets this. An audit report cites a location, and the
    plain body left the model to count lines itself: on the extra set every
    file:line it wrote was off by one (`client.py:7` and `:5` for line 6,
    EXTRA-taxonomy). A fix task keeps the plain body, because there the model copies
    text back into `replace_text` and a prefix would break the anchor. The footer and
    the window-cut marker stay as they are.
    """
    cut = output.rfind(_READ_FOOTER)
    body, footer = (output, "") if cut < 0 else (output[:cut], output[cut:])
    numbered: list[str] = []
    number = start_line
    for line in body.split("\n"):
        if line.startswith("… [window cut at line "):
            numbered.append(line)
            continue
        numbered.append(f"{number}: {line}")
        number += 1
    if body.endswith("\n") and numbered and numbered[-1] == f"{number - 1}: ":
        numbered.pop()
        return "\n".join(numbered) + "\n" + footer
    return "\n".join(numbered) + footer


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
        if _foreground_server(command):
            return tools.ToolResult(
                name=tool,
                ok=False,
                detail=(
                    "refused: a server in the foreground never exits, so this tool would "
                    "wait out its timeout and then kill it. Start it in the background: "
                    f"`nohup {command} >/tmp/server.log 2>&1 &`, then check the port."
                ),
            )
        if _background_holds_pipe(command):
            return tools.ToolResult(
                name=tool,
                ok=False,
                detail=(
                    "refused: a background job (`&`) without redirected output keeps this "
                    "tool waiting until its timeout, and the timeout then kills the job. "
                    "Start it as `nohup CMD >/tmp/CMD.log 2>&1 &`, then check it separately."
                ),
            )
        guarded: dict[Path, bytes] | None = None
        if playbook.name == "code-fix" and command.strip() not in state.relaunch_commands:
            refusal = _code_fix_shell_refusal(command, state)
            if refusal:
                return tools.ToolResult(name=tool, ok=False, detail=refusal)
            if _python_probe(command, state):
                guarded = _protected_snapshot(state)
                if guarded is None:
                    return tools.ToolResult(
                        name=tool,
                        ok=False,
                        detail=(
                            "code-fix refused: too many evaluator files to guard a Python "
                            "probe here; use pytest or a read-only shell command instead."
                        ),
                    )
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
        if guarded is not None:
            changed = _restore_protected(guarded, state)
            if changed:
                state.python_probes_disabled = True
                shown = ", ".join(redact(item, secrets) for item in changed[:5])
                return tools.ToolResult(
                    name=tool,
                    ok=False,
                    detail=(
                        f"code-fix refused: the probe changed evaluator/repository files "
                        f"({shown}); they were restored, and Python probes are refused for "
                        "the rest of this run. Keep tests, fixtures, expected answers and "
                        ".git metadata immutable; edit production source instead."
                    ),
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
        start_line = int(args["start_line"])
        result = tools.read_range(target, start_line=start_line, secrets=secrets)
        if result.ok:
            try:
                state.read_paths.add(target.resolve())
            except OSError:
                pass
            if playbook.name == "code-audit":
                result = dataclasses.replace(
                    result, output=_number_read_lines(result.output, start_line)
                )
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


_STEP_PREFIX = re.compile(r"\[step (\d+)\]")


def _already_observed(
    state: AgentState, tool: str, args: str, visible_steps: frozenset[int]
) -> int | None:
    """The earlier step whose identical call the model can still read, or None.

    Only a result still inside the request the model just answered counts
    (`visible_steps`): after a re-lay the earlier read is gone from its view, and
    skipping the repeat then left it with neither the content nor a word about why.

    Inside one epoch nothing the run did changed a file or a service, so a repeat of a
    read returns what the history already carries. That covers the primary action of
    this very step -- its record is written before the batch starts -- and every read
    of every earlier step back to the last mutation, which is exactly the window
    `agent.detectors` already treats as one environment.

    Cheaper than `detectors.blocked` and strictly stricter than it: `blocked` refuses
    the third identical call, this skips the second, because an extra is an action
    nobody asked to pay for and re-reading a file for the same bytes is the failure
    this batch exists to stop, not one it may reproduce inside a step.
    """
    for record in reversed(state.actions):
        if (
            record.state_epoch == state.state_epoch
            and record.tool == tool
            and record.args_digest == args
            and record.step in visible_steps
        ):
            return record.step
    return None


def _batch_reads(
    state: AgentState,
    extras: tuple[str, ...],
    observations: list[str],
    *,
    step: int,
    playbook: Playbook,
    visible_from: int = 0,
) -> int:
    """Run the read-only tail of one reply inside the step its first action used.

    Why: on an endpoint fast enough that a call costs seconds, the step cap and not
    the clock is what ends a run, and a reasoning model answers most code-fix steps
    with three to six `read_range` calls at once. Keeping only the first made the model
    re-request the same batch every step -- 49 of 51 calls of one measured episode were
    reads, no edit was ever made, all 30 steps went, and 10 of 10 runs on that stand
    stopped at `steps` or `stuck` (JOURNAL 16.09, the code-fix traces under
    `build/afk-q36-treatment/logs`).

    What it must not become is several state changes for the price of one step. Only
    `BATCH_READ_TOOLS` run here, so a batch can never advance the state epoch; the
    first extra that is anything else stops the batch and is left un-executed, and the
    model re-asks for it as the next step's primary action, where the write policy, the
    finish gate and the epoch bookkeeping all apply unchanged. The reply costs one step
    and one LLM call either way -- that is the whole point, and it is also why nothing
    here touches the token budget.

    Two things the batch deliberately does not do (critic, 16.09). It does not write
    its extras into the detector window: `state.actions` holds `ACTION_HISTORY` = 6
    records, and four reads per step would push the primary actions out of it, blinding
    the repeat and stuck detectors to exactly the loop this exists to break. Extras are
    audited (`tool` events, the `batch` note) and fingerprinted for duplicates inside
    the batch, nothing more. And it does not add one history entry per extra: the whole
    batch is one observation of at most `BATCH_OBSERVATION_MAX_CHARS`, so it takes one
    of the `OBSERVATION_RELAY_KEEP` slots and the primary's own observation survives.

    Returns how many of the executed extras came back with content, which is what the
    early-deliverable checkpoint counts as grounding.
    """
    share = limits.BATCH_OBSERVATION_MAX_CHARS // limits.BATCH_READONLY_MAX
    executed = 0
    grounded = 0
    seen: set[tuple[str, str]] = set()
    parts: list[str] = []
    visible_steps = frozenset(
        int(found.group(1))
        for text in observations[visible_from:]
        if (found := _STEP_PREFIX.match(text))
    )

    for index, raw in enumerate(extras[: limits.BATCH_READONLY_MAX], start=2):
        if state.budget.spendable_s() <= 0:
            # The step's own time is gone. Whatever is left of the batch is not worth
            # spending the verifier reserve on.
            state.record("budget", "batch-stop", "no time left for the rest of the batch")
            break

        act, problem = _parse(raw)
        if problem:
            # `agent.llm` validated these against the action schema, so this is the
            # second line of defence and the entry is skipped rather than acted on.
            state.record("llm", "batch-malformed", problem)
            continue

        tool = str(act.get("tool", ""))
        if tool not in BATCH_READ_TOOLS:
            state.record(
                "budget",
                "batch-stop",
                f"{tool or 'unnamed tool'} is not read-only; leaving it for its own step",
            )
            break

        args, problem = _arguments(act, state)
        if problem or args is None:
            state.record("llm", "batch-bad-action", problem)
            continue

        digest = detectors.args_digest(args)
        if (tool, digest) in seen:
            state.record("budget", "batch-repeat", f"{tool} already ran in this batch")
            continue
        earlier = _already_observed(state, tool, digest, visible_steps)
        if earlier is not None:
            state.record("budget", "batch-repeat", f"{tool} already ran in this epoch")
            if earlier != step:
                # This step's own primary sits right above the batch; an older one is
                # worth naming, or the model reads the silence as "not run".
                parts.append(
                    f"[step {step}.{index}] {tool}: same call as [step {earlier}] above, not repeated"
                )
            continue
        seen.add((tool, digest))

        act_started = state.start_phase("act")
        try:
            result = _execute(tool, args, state, playbook=playbook)
        except Exception as error:
            # `_execute` is documented never to raise and the primary path trusts that.
            # The asymmetry here is deliberate: an extra is work the run did not have
            # to be given, and losing a whole task to a defect in a bonus action would
            # be the worst trade in the file. The exception type only -- its message
            # may quote file content.
            result = tools.ToolResult(
                name=tool, ok=False, detail=f"batch action failed: {type(error).__name__}"
            )
        state.finish_phase("act", act_started)

        if os.environ.get(limits.TRACE_TOOLS_ENV_NAME):
            # Same line as the primary action's, with the sub-step marker, so a live
            # trace shows a batch as what it is rather than as a step that vanished.
            traced = (result.output or result.detail or "").replace("\n", " ")[:160]
            print(f"agent tool: step={step}.{index} {tool} ok={result.ok} {traced}", flush=True)

        body = result.output or result.detail
        state.record(
            "tool",
            tool,
            result.detail or "ok",
            duration_s=result.duration_s,
            exit_code=result.exit_code,
        )
        executed += 1
        if result.ok and body.strip():
            grounded += 1

        header = f"[step {step}.{index}] {tool} -> {'ok' if result.ok else 'FAIL'}\n"
        room = max(0, share - len(header))
        head = (room * 2) // 3
        clipped, _ = tools.clip(body, head=head, tail=room - head)
        # Hard-cut after `clip`: its truncation marker can add a few characters back,
        # and the bound on what one batch may add is the point of the share.
        parts.append(f"{header}{clipped[:room]}".rstrip())

    if parts:
        observations.append("\n".join(parts)[: limits.BATCH_OBSERVATION_MAX_CHARS])
    state.record("budget", "batch", f"executed {executed} of {len(extras)} extra calls")
    return grounded


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


# --- The check that could not have seen the change ----------------------------
#
# Measured over three code-fix episodes on the 30B stand: in every one the model edited
# the handler, ran the project's own test suite, and the suite passed -- against a
# server the container had started at entry, without reload, which was still importing
# the pre-edit module. Two of those three edits were wrong (a duplicated call line, a
# stale zero-argument call left beside the new parametrized query) and the run reported
# itself solved anyway; the grader, which restarts the service before scoring, gave
# zero. No exit code could have separated those runs from a real repair, and the task's
# own AGENTS.md warns about the stale process.
#
# So the finish gate asks one question the exit code cannot answer: could this check
# have exercised the change at all? A process that started *before* the file changed is
# running the old code, and every check that goes through it is measuring the old code.
#
# The gate observes and refuses; it never acts. It does not restart anything, and it
# refuses only once (`limits.SERVICE_SCAN_MAX_REFUSALS`): the model may look at the
# process and decide it does not matter, and a run that finished over the warning is
# visible afterwards through the `stale-service` audit note rather than through a
# second refusal it cannot get past.


def _existed_before_run(state: AgentState, path: Path) -> bool:
    """Whether this path was already on disk when the run started.

    Positive evidence only, from the two snapshots the loop already keeps. A file the
    run *created* is a report, a note or a patch -- nothing a process started before the
    run can be executing. A file the run *edited* is the case this gate exists for.
    """
    snapshot = state.source_snapshot
    if snapshot is not None:
        try:
            relative = path.relative_to(state.workspace.resolve()).as_posix()
        except (OSError, ValueError):
            relative = ""
        if relative and relative in snapshot.files:
            return True
    return bool(state.baseline.get(str(path)))


def _modified_production_files(
    state: AgentState, changes: result_progress.SourceChanges | None
) -> list[Path]:
    """Existing production files this run changed, from both oracles the loop keeps.

    Neither alone is complete: `state.artifacts` knows every file written through a tool
    but nothing about a `sed -i` from the shell, and the source scan sees any edit under
    the workspace but is bounded and may be incomplete.

    `changes` is the scan the caller already paid for, not a second walk of the same
    tree: `_source_changes` runs a few lines earlier in the same finish gate, and
    re-reading the workspace to answer the same question would double a cost that is
    charged against the verifier reserve. `None` -- the playbook that does not track
    source progress -- simply leaves the artifacts oracle to answer alone.
    """
    found: dict[Path, None] = {}

    def keep(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        found.setdefault(resolved, None)

    snapshot = state.source_snapshot
    if changes is not None and changes.known and snapshot is not None:
        for relative in changes.paths:
            if relative in snapshot.files:  # edited, not created by this run
                keep(state.workspace / relative)

    for artifact in state.artifacts:
        if _existed_before_run(state, artifact):
            keep(artifact)

    return list(found)


def _wall_clock(moment: float) -> str:
    """A wall-clock instant as the model reads it in a log: local time, seconds."""
    return time.strftime("%H:%M:%S", time.localtime(moment))


def _workspace_relative(path: Path, workspace: Path) -> str:
    """The short name of a path for a message, never an absolute host path.

    Single-lined on the way out: a file name may legally contain a newline, and this
    text is rendered one event per line and read back by the eval parsers.
    """
    try:
        name = path.relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        name = path.name
    return name.replace("\r", " ").replace("\n", " ")


def _stale_service(
    state: AgentState, changes: result_progress.SourceChanges | None = None
) -> tuple[str, str, str] | None:
    """`(audit name, audit detail, hint for the model)` about pre-change processes.

    `None` means no scan happened at all -- this run has changed nothing a process could
    be executing, which is the ordinary case on an audit or a forensics task. The caller
    counts scans against a budget and must not spend one on a question with no subject.

    Otherwise three outcomes, and the empty one is the common one:

    * `("", "", "")`        -- every process that could be running the change was
      started after it. Nothing is recorded: an audit line per finish saying a machine
      looked fine would be noise in the one place a reader looks for trouble.
    * `("stale-service-unknown", reason, "")` -- the scan could not be completed (no
      procfs, a permission refusal, its own time budget). Nothing is refused, because
      "we could not look" is not evidence, and the note says the check ran blind.
    * `("stale-service", detail, hint)` -- a process older than the edit is running it.

    Never raises. A scan that fails inside the finish gate would cost the whole task,
    so every failure becomes the unknown outcome above.
    """
    try:
        modified = _modified_production_files(state, changes)
        changed_at: dict[Path, float] = {}
        for path in tools.production_sources(modified):
            try:
                changed_at[path] = path.stat().st_mtime
            except OSError:
                continue
        if not changed_at:
            return None

        scan = tools.serving_processes(list(changed_at), secrets=state.secrets)
        timeless = 0
        stale: list[tuple[tools.ServingProcess, Path, float]] = []
        for process in scan.processes:
            if process.started_at <= 0.0:
                # procfs named the process but not when it started, so nothing can be
                # concluded about what it loaded. Counted, not guessed at: it is the
                # same blind spot as an unreadable process table and is reported as one.
                timeless += 1
                continue
            newer = [
                (path, moment)
                for path, moment in changed_at.items()
                if moment - process.started_at > limits.SERVICE_SCAN_START_MARGIN_S
            ]
            if newer:
                path, moment = min(newer, key=lambda item: item[1])
                stale.append((process, path, moment))

        if not stale:
            if timeless:
                return (
                    "stale-service-unknown",
                    f"{timeless} process(es) run the changed code; procfs gave no start time",
                    "",
                )
            if not scan.known:
                return "stale-service-unknown", scan.reason or "process scan was incomplete", ""
            return "", "", ""

        process, path, moment = stale[0]
        relative = _workspace_relative(path, state.workspace)
        ports = ", ".join(f":{port}" for port in process.ports)
        listens = f", слушает {ports}" if ports else ""
        others = f" Ещё процессов в том же состоянии: {len(stale) - 1}." if len(stale) > 1 else ""
        cwd_note = f" (`{process.cwd}`)" if process.cwd else ""
        relaunch = ""
        if process.command:
            where = f"cd {shlex.quote(str(process.cwd))} && " if process.cwd else ""
            # Braces background the service alone: `a && b &` backgrounds a subshell that
            # bash keeps holding our output pipe with, and the tool then waits out its
            # whole timeout and kills the service it just started. dash does not, bash does.
            ready = f"{where}{{ nohup {process.command} >/tmp/service-restart.log 2>&1 </dev/null & }}"
            state.relaunch_commands.add(ready)
            relaunch = (
                f" Готовая команда: `{ready}` — после неё проверь, что порт снова "
                "отвечает, прежде чем гонять тесты."
            )
        hint = (
            f"[verify] сервис `{process.label}` (pid {process.pid}{listens}) запущен в "
            f"{_wall_clock(process.started_at)}, раньше твоей правки {relative} в "
            f"{_wall_clock(moment)}: любая проверка через него меряет старый код.{others} "
            f"Останови только этот процесс: `kill {process.pid}` (ничего другого не "
            "убивай, никаких pkill/killall), затем запусти ту же команду заново в фоне "
            f"из его каталога{cwd_note} так, как это делает entrypoint проекта, дождись, "
            "пока порт снова отвечает, повтори проверку и только потом finish."
            f"{relaunch}"
        )
        detail = (
            f"pid {process.pid} `{process.label}` started {_wall_clock(process.started_at)} "
            f"before {relative} changed at {_wall_clock(moment)}"
            f"{'; ports ' + ports if ports else ''}; {process.reason}"
        )
        return "stale-service", detail, hint
    except Exception as exc:  # noqa: BLE001 -- a raise here would cost the task
        return "stale-service-unknown", f"process scan failed: {type(exc).__name__}", ""


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


SLOW_CALL = "slow"
HARD_FAILURE = "hard"
"""The two things a failed call can mean, and they end the run differently.

`slow` is a call that ran out of wall clock having been handed nothing: the endpoint is
generating and we stopped listening. `hard` is everything else -- a refused connection, a
reply that never parsed, an unusable protocol.
"""


def _call_timed_out(error: LlmError) -> bool:
    """Whether a failed call is our deadline expiring rather than a broken endpoint.

    "with no payload" is literal: an error that carries output tokens was handed part of
    a reply and billed for it, and that is a different failure from silence.
    """
    # The typed flag, never the reason text: that text can quote the endpoint's own
    # body, and a 504 "upstream request timeout" or a 400 mentioning a timeout parameter
    # is a broken endpoint answering instantly, not our clock running out.
    return error.timed_out and error.output_tokens == 0


def _llm_failure_kind(error: LlmError) -> str:
    """Classify a failed call for the consecutive-failure counters."""
    return SLOW_CALL if _call_timed_out(error) else HARD_FAILURE


def _endpoint_is_slow_or_thinking(reply: LlmReply | LlmError, llm: LlmPort) -> bool:
    """Whether the endpoint reasons before answering, so the 90s call clamp only hurts.

    Two loop-observable signals, and the narrowing of the second one is the point.

    The client owns the reply-shaped evidence -- a reasoning field, a cutoff inside a
    think block, a cutoff that spent the whole budget with nothing visible -- and exposes
    it as one sticky flag (`agent.llm._thinking_evidence`). A bare length cutoff is no
    longer part of it: on the fast 7B that fired on a degenerate generation with an
    ordinary answer and cost 250 s for the same zero (`F4-7b-regression-4080`).

    The second signal is a call that timed out with no payload, and it exists because it
    is the only one available on the very first call of a run -- which is exactly where
    the clamp bit hardest (`F4-30b-paired`: 12/12 and 6/6 episodes losing 185-280 s before
    their first usable reply).
    """
    if getattr(llm, "_thinking_endpoint", False):
        return True
    return isinstance(reply, LlmError) and _call_timed_out(reply)


_EDIT_LINE = re.compile(r"at line (\d+)")
"""Where `agent.tools.replace_text` says the edit landed. Read opportunistically: if the
wording ever changes, the echo falls back to locating the replacement itself."""


def _edit_echo(
    path: Path, new: str, result: tools.ToolResult, state: AgentState
) -> str:
    """The edited region with a few lines of context, for the model to read back.

    An anchored edit is the one action whose result the model could not see. It got
    `replaced 1 occurrence at line N` and nothing else, which says the anchor matched --
    never what the file now says around it. In 5 of 5 failed fix-sqli edits the anchor
    covered the statement that built the query but not the call that used it, so the
    repaired line ended up beside a stale or duplicated call; in 4 of 4 successful edits
    the anchor included that call. The difference is one line away from the edit and was
    invisible for the rest of the run.

    So the observation carries the neighbourhood, bounded by `EDIT_ECHO_MAX_CHARS` and
    `EDIT_ECHO_CONTEXT_LINES` and redacted like any other observation. Best effort
    throughout: a file that vanished, changed underneath us or is not text costs the echo
    and never the step -- the edit itself has already succeeded.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""

    lines = text.splitlines()
    if not lines:
        return ""

    first: int | None = None
    match = _EDIT_LINE.search(result.output or "")
    if match:
        first = int(match.group(1)) - 1
    elif new:
        found = text.find(new)
        if found >= 0:
            first = text.count("\n", 0, found)
    if first is None or not 0 <= first < len(lines):
        return ""

    last = min(len(lines) - 1, first + max(0, new.count("\n")))
    start = max(0, first - limits.EDIT_ECHO_CONTEXT_LINES)
    end = min(len(lines), last + 1 + limits.EDIT_ECHO_CONTEXT_LINES)

    window = "\n".join(lines[start:end])
    if len(window) > limits.EDIT_ECHO_MAX_CHARS:
        window = window[: limits.EDIT_ECHO_MAX_CHARS] + "\n… [echo cut]"
    return redact(
        f"\n[после правки, строки {start + 1}-{end} файла {path}]\n{window}", state.secrets
    )


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

    summary = summary or (verdict.detail if verdict else "")
    if state.budget.steps_extended:
        # The `stop_reason` vocabulary is read by the eval parsers and does not move: a
        # run that spent the extension and still hit the cap is `steps`, exactly as it
        # was before. That the cap it hit was the raised one belongs in the free text,
        # where a results row shows it as a note instead of as a new failure class.
        note = f"{stop_reason} (extended once, +{state.budget.steps_extension_granted} steps)"
        summary = f"{note}: {summary}" if summary else note

    return Outcome(
        solved=False,
        stop_reason=stop_reason,
        deliverable=deliverable,
        summary=summary,
        verdict=verdict,
        observations=tuple(observations),
    )
