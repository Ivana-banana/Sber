"""Run the built submission against local task packages and record the metrics.

Development tool. `agent/` never imports it and `build_submission.py` never packs it.

What it is for. Every experiment before this one (E001-E008) measured startup,
packaging, loop mechanics or fixture validity. None of them measured whether the agent
*solves* anything, because none of them had a model: the endpoint was absent,
unreachable, or a script with recorded answers. This harness closes that gap by
running the real archive against a real model and reading the task's own verifier.

The launch contract is copied from `docs/research/harbor-launch-contract.md`, because
a baseline measured under a friendlier contract than the real one is worse than no
baseline:

* the agent is copied into `/opt/harbor/local-agent` and executed with that as cwd,
  not with the task directory as cwd;
* exactly three variables are exported -- `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL`,
  `OPENAI_API_KEY`. Harbor passes the task timeout to nobody, so by default neither do
  we, and the agent runs on its own internal deadline while an external kill enforces
  the real one;
* the task's own image, `WORKDIR`, entrypoint service, cpu and memory limits come from
  the package, never from a table in this file. A harness that hardcodes which tasks
  start a server would stop being an independent check the moment a package changes.

`--timeout-scale` exists because this machine runs a 7B model on CPU. A run killed
because the hardware is slow and a run killed because the agent went in circles are
different failures, and reporting them as one number would be a lie. Scale 1.0 is the
contract; anything above it is a separate, separately labelled measurement.

It moves the external kill and nothing else. The agent is still told no budget, so it
still assumes the outer bound and still writes its early deliverable on the same
schedule -- the only thing that varies is how many steps fit before the kill, which is
what a faster endpoint would change too.
"""

from __future__ import annotations

import argparse
import ast
import json
import hashlib
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.llm import HttpLlm, LlmSettings
from agent.prompt import ACTION_SCHEMA

AGENT_DIR = "/opt/harbor/local-agent"
TESTS_DIR = "/tests"
"""Where a package's `tests/` has to land inside the task container.

Not a preference: `find-sqli-login`, `fix-sqli-login` and `fix-sqli-search` run
`pytest /tests` by absolute path from their own `test.sh`. Anywhere else and pytest
collects nothing, exits 4 and the script writes reward 0 -- so a correct report
scored exactly like an empty one, and half the public set could not have produced a
one no matter what the agent did (E: see the 2026-09-10 card).
"""
REWARD_PATH = "/logs/verifier/reward.txt"

# How long the harness itself waits for a docker step that is not the agent. Generous,
# because a first-time image build pulls layers; still finite, because a hung build
# must fail the row rather than the night.
DOCKER_STEP_TIMEOUT_S = 900.0
# Slack added to the agent's own kill so `timeout` inside the container is what stops
# the run, and this process only steps in if the container itself wedged.
OUTER_SLACK_S = 30.0

DONE_LINE = re.compile(
    r"agent done: solved=(?P<solved>\w+) stop=(?P<stop>\S+) stage=(?P<stage>\S+) "
    r"deliverable=(?P<deliverable>\S+) steps=(?P<steps>\d+)/(?P<max_steps>\d+) "
    r"recovery=(?P<recovery>\d+) tokens=(?P<tokens>\d+)"
)
READY_LINE = re.compile(r"agent ready: workspace=(?P<workspace>\S+) \((?P<why>[^)]*)\)")
def requested_write(message: dict[str, object]) -> bool:
    """Count decodable write requests, never execution or filesystem changes."""
    message = dict(message)
    historical = message.get("tool_calls")
    if isinstance(historical, str):
        # Traces written before the proxy stored this field structurally hold
        # `str(message["tool_calls"])`, and an absent list came out as "". Such a
        # field is evidence of nothing either way, so an unreadable one is dropped
        # rather than vetoing `content`, which may still carry the action. The length
        # bound keeps `literal_eval` off a truncated megabyte of prose.
        message["tool_calls"] = None
        if 0 < len(historical) <= 2000:
            try:
                message["tool_calls"] = ast.literal_eval(historical)
            except (ValueError, SyntaxError):
                message["tool_calls"] = None
    client = HttpLlm(LlmSettings(model="recorded", base_url="", api_key=""))
    text, _ = client._read_reply({"choices": [{"message": message}]}, ACTION_SCHEMA)
    return text is not None and json.loads(text).get("tool") == "write_file"


PHASE_LINE = re.compile(
    r"agent phase: step=(?P<step>\d+) phase=(?P<phase>\w+) "
    r"status=(?P<status>start|end) duration_s=(?P<duration>[0-9.]+)"
)
PHASE_ORDER = ("inspect", "plan", "act", "verify")


@dataclass
class PhaseBreakdown:
    """Where one run's wall clock actually went, read back from its own log.

    E018 needed this by hand: eighteen greps to tell "the model was generating" from
    "the agent took too many steps" from "the verifier ate the reserve". The numbers
    were already in the log; only the harvesting was missing.
    """

    seconds: dict[str, float] = field(default_factory=dict)
    """Completed time per phase. A phase killed mid-flight contributes nothing."""

    completed: dict[str, int] = field(default_factory=dict)
    """How many times each phase finished, so a total can be read as a per-call cost."""

    open_phase: str = ""
    """`name@step` of the phase that never ended -- what held the deadline at the kill."""

    def render(self) -> str:
        parts = [
            f"{name}={self.seconds[name]:.3f}/{self.completed[name]}"
            for name in PHASE_ORDER
            if name in self.seconds
        ]
        if self.open_phase:
            parts.append(f"open={self.open_phase}")
        return " ".join(parts) or "-"

    @classmethod
    def parse(cls, rendered: str) -> PhaseBreakdown:
        """Read `render` back, so a summary can aggregate the table, not the logs.

        The placeholder `-` and any token this format does not own are skipped: a
        results table written by an older harness must stay readable.
        """
        breakdown = cls()
        for token in rendered.split():
            name, _, value = token.partition("=")
            if name == "open":
                breakdown.open_phase = value
            elif value:
                seconds, _, count = value.partition("/")
                breakdown.seconds[name] = float(seconds)
                breakdown.completed[name] = int(count)
        return breakdown


def phase_breakdown(output: str) -> PhaseBreakdown:
    """Sum the agent's phase markers. Phases are sequential, so nesting cannot occur.

    A log written before the telemetry existed, or one truncated mid-line by a kill,
    yields an empty breakdown rather than an error: the harness has to keep reading
    every other field of a run whose diagnostics are incomplete.
    """
    breakdown = PhaseBreakdown()
    for match in PHASE_LINE.finditer(output):
        name = match["phase"]
        if match["status"] == "start":
            breakdown.open_phase = f"{name}@{match['step']}"
            continue
        breakdown.open_phase = ""
        breakdown.seconds[name] = round(
            breakdown.seconds.get(name, 0.0) + float(match["duration"]), 3
        )
        breakdown.completed[name] = breakdown.completed.get(name, 0) + 1
    return breakdown


@dataclass
class TaskSpec:
    """Everything the harness needs about one package, all read from the package."""

    name: str
    root: Path
    instruction: str
    workdir: str
    has_entrypoint: bool
    agent_timeout_s: float
    verifier_timeout_s: float
    build_timeout_s: float
    cpus: float
    memory_mb: int


@dataclass
class TaskResult:
    task: str
    repeat: int
    reward: int | None
    agent_exit: int | None
    wall_s: float
    tokens: int | None
    steps: int | None
    stop_reason: str
    solved_claim: str
    workspace: str
    failure_mode: str
    endpoint_tokens: int | None = None
    """What the endpoint counter saw. Present even when the run was killed before it
    could report its own total, which is most of them."""

    endpoint_requests: int | None = None
    endpoint_late: int | None = None
    """Replies that arrived after their task was killed. Non-zero means the previous
    task outlived its deadline at the endpoint, not that this one misbehaved."""

    wrote: str = "-"
    """Whether the run ever asked for a `write_file`.

    A separate gate from `reward`, and E037 is why. That run kept solved at 2/18 while
    the tasks leaving any deliverable fell from 14 of 18 to 3: the two solved tasks are
    short ones and survived a collapse that took eleven others. Solved alone did not
    show it.
    """

    phases: str = "-"
    """Rendered `PhaseBreakdown`: where the wall clock went, per task and per repeat."""

    note: str = ""
    log: str = ""
    timed_out: bool = False

    @property
    def eligible_success(self) -> bool:
        return (self.reward == 1 and self.agent_exit == 0
                and not self.timed_out and self.stop_reason != "killed")


@dataclass
class SuiteResult:
    label: str
    model: str
    base_url: str
    timeout_scale: float
    started: str
    rows: list[TaskResult] = field(default_factory=list)
    expected_tasks: list[str] = field(default_factory=list)
    repeats: int = 1

    @property
    def complete(self) -> bool:
        expected = {(name, repeat) for name in self.expected_tasks
                    for repeat in range(1, self.repeats + 1)}
        actual = [(row.task, row.repeat) for row in self.rows]
        return bool(expected) and len(actual) == len(expected) and set(actual) == expected


def run(
    cmd: list[str], *, timeout: float = DOCKER_STEP_TIMEOUT_S, text: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=text, timeout=timeout, check=False
    )


def task_names(root: Path) -> list[str]:
    """Every package under `root`, and nothing else.

    The public snapshot keeps `insecure-api-app` next to the six task packages: it is
    the application the SQLi tasks are built from, not a task, and it has no
    `task.toml`. Treating every subdirectory as a package made the whole suite die on
    it with a FileNotFoundError, taking the results of the tasks that had already run
    down with it.
    """
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file()
    )


def read_spec(pkg: Path) -> TaskSpec:
    """Derive the run parameters from the package rather than from a lookup table."""
    config = tomllib.loads((pkg / "task.toml").read_text(encoding="utf-8"))
    environment = config.get("environment", {})
    dockerfile = (pkg / "environment" / "Dockerfile").read_text(encoding="utf-8")

    workdir = "/"
    has_entrypoint = False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("WORKDIR "):
            workdir = stripped.split(None, 1)[1].strip()
        if stripped.upper().startswith("ENTRYPOINT"):
            has_entrypoint = True

    return TaskSpec(
        name=pkg.name,
        root=pkg,
        instruction=(pkg / "instruction.md").read_text(encoding="utf-8").strip(),
        workdir=workdir,
        has_entrypoint=has_entrypoint,
        agent_timeout_s=float(config.get("agent", {}).get("timeout_sec", 180.0)),
        # The verifier's own budget, not a fixed 180 s: the fix-sqli packages give
        # theirs 240 s because test.sh brings up PostgreSQL and uvicorn and waits on
        # them, so a hardcoded 180 s recorded a slow-but-correct verifier as reward=None.
        verifier_timeout_s=float(config.get("verifier", {}).get("timeout_sec", 180.0)),
        build_timeout_s=float(environment.get("build_timeout_sec", 600.0)),
        cpus=float(environment.get("cpus", 1)),
        memory_mb=int(environment.get("memory_mb", 512)),
    )


def classify_failure(result: TaskResult, *, killed: bool) -> str:
    """Put every zero in exactly one bucket from CLAUDE.md's taxonomy.

    Order matters: a run that was killed is a `timeout` even if it also never reached
    a verifier, because the timeout is the thing that has to be fixed first.
    """
    if killed:
        return "timeout"
    if result.eligible_success:
        return "-"
    if result.agent_exit is None:
        return "startup"
    if result.stop_reason in {"llm-unusable", "no-endpoint"}:
        return "endpoint"
    if result.stop_reason in {"steps", "budget", "stuck", "blocked"}:
        return "reasoning"
    if result.solved_claim == "True":
        # The agent finished, verified itself green, and the task's verifier disagrees.
        # That is the most expensive class we have: the run believed it was done.
        return "verification"
    return "reasoning"


class Runner:
    def __init__(self, args: argparse.Namespace, unpacked: Path) -> None:
        self.args = args
        self.unpacked = unpacked
        self.logs = Path(args.out) / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def counter_call(self, path: str, method: str) -> dict[str, object] | None:
        """Talk to the counting proxy through `docker exec`, not over the network.

        The proxy lives on an `--internal` network and the host firewall drops traffic
        from docker bridges to the host, so the reliable channel to it is the one that
        does not use IP routing at all.
        """
        if not self.args.counter_container:
            return None
        script = (
            "import json,urllib.request;"
            f"r=urllib.request.Request('http://127.0.0.1:8000{path}',method='{method}');"
            "print(urllib.request.urlopen(r,timeout=30).read().decode())"
        )
        answered = run(
            ["docker", "exec", self.args.counter_container, "python3", "-c", script],
            timeout=60.0,
        )
        if answered.returncode != 0:
            return None
        try:
            return json.loads(answered.stdout)
        except ValueError:
            return None

    def image_tag(self, spec: TaskSpec) -> str:
        return f"sber-eval/{spec.name}:latest"

    def build(self, spec: TaskSpec) -> str | None:
        if not self.args.reuse_images:
            built = run(
                ["docker", "build", "--network=host", "-q", "-t", self.image_tag(spec),
                 str(spec.root / "environment")], timeout=spec.build_timeout_s,
            )
            if built.returncode != 0:
                return built.stderr.strip()[-400:]
        inspected = run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image_tag(spec)])
        if inspected.returncode != 0:
            return "local image unavailable: " + inspected.stderr.strip()[-300:]
        manifest_path = Path(self.args.out) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["images"][spec.name] = inspected.stdout.strip()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return None

    def start_container(self, spec: TaskSpec) -> tuple[str | None, str]:
        """Start the task environment the way the package declares it.

        A package whose Dockerfile has an ENTRYPOINT is a package that starts its own
        service, and overriding the command would quietly turn a service task into a
        task whose service never came up -- a zero that looks like the agent's fault.
        """
        cmd = [
            "docker", "run", "-d", "--rm",
            "--network", self.args.network,
            f"--cpus={spec.cpus}",
            f"--memory={spec.memory_mb}m",
            self.image_tag(spec),
        ]
        if not spec.has_entrypoint:
            cmd += ["tail", "-f", "/dev/null"]
        started = run(cmd)
        if started.returncode != 0:
            return None, started.stderr.strip()[-400:]
        return started.stdout.strip(), ""

    def install(self, container: str, spec: TaskSpec) -> str:
        """Put the agent archive in place. The package's tests deliberately stay out.

        `tests/` carries the answers -- `incident-log-forensics` ships the exact
        expected report next to its script -- so installing it before the agent phase
        leaves the answer key inside the container the agent is free to explore, and a
        run that read it would be scored a solve for copying it.
        """
        mk = run(["docker", "exec", container, "mkdir", "-p", AGENT_DIR])
        if mk.returncode != 0:
            return f"mkdir {AGENT_DIR}: {mk.stderr.strip()[-200:]}"
        copied = run(["docker", "cp", f"{self.unpacked}/.", f"{container}:{AGENT_DIR}"])
        if copied.returncode != 0:
            return f"cp {AGENT_DIR}: {copied.stderr.strip()[-200:]}"
        return ""

    def install_verifier(self, container: str, spec: TaskSpec) -> str:
        """Deliver the package's tests, after the agent phase and only then."""
        mk = run(["docker", "exec", container, "mkdir", "-p", TESTS_DIR])
        if mk.returncode != 0:
            return f"mkdir {TESTS_DIR}: {mk.stderr.strip()[-200:]}"
        copied = run(["docker", "cp", f"{spec.root / "tests"}/.", f"{container}:{TESTS_DIR}"])
        if copied.returncode != 0:
            return f"cp {TESTS_DIR}: {copied.stderr.strip()[-200:]}"
        return ""

    def exec_agent(self, container: str, spec: TaskSpec) -> tuple[subprocess.CompletedProcess[str] | None, float, bool]:
        budget = spec.agent_timeout_s * self.args.timeout_scale
        env = [
            "-e", f"LOCAL_AGENT_MODEL={self.args.model}",
            "-e", f"OPENAI_BASE_URL={self.args.base_url}",
            "-e", f"OPENAI_API_KEY={self.args.api_key}",
        ]
        if self.args.controller:
            # The treatment arm of the macro-agent comparison. Set here and not baked
            # into a second archive on purpose: both arms then run the same bytes with
            # the same sha256, and the controller is the only thing that differs.
            env += ["-e", f"AGENT_CONTROLLER={self.args.controller}"]
        if self.args.tell_agent_budget:
            # Off by default, and off in every scaled run: Harbor tells the agent
            # nothing about the deadline, so telling it here would change two things at
            # once. `--timeout-scale` is meant to vary how much time the run actually
            # gets, with the agent behaving exactly as it does on the real endpoint --
            # assuming the outer bound and producing its early deliverable on schedule.
            env += ["-e", f"AGENT_TIMEOUT_S={budget:.0f}"]

        cmd = [
            "docker", "exec", "-w", AGENT_DIR, *env, container,
            "timeout", f"{budget:.0f}", "./run.sh", spec.instruction,
        ]
        started = time.monotonic()
        try:
            proc = run(cmd, timeout=budget + OUTER_SLACK_S)
        except subprocess.TimeoutExpired:
            return None, time.monotonic() - started, True
        wall = time.monotonic() - started
        # 124 is `timeout`'s own kill signal; 137 is SIGKILL reaching the process.
        return proc, wall, proc.returncode in (124, 137)

    def verify(self, container: str, spec: TaskSpec) -> tuple[int | None, str]:
        run(["docker", "exec", container, "mkdir", "-p", "/logs/verifier"])
        checked = run(
            ["docker", "exec", container, "bash", f"{TESTS_DIR}/test.sh"],
            timeout=spec.verifier_timeout_s,
        )
        reward = run(["docker", "exec", container, "cat", REWARD_PATH])
        if reward.returncode != 0:
            return None, f"no reward file; test.sh said {checked.stderr.strip()[-200:]}"
        raw = reward.stdout.strip()
        try:
            # `float` first so a verifier that writes `1.0` is not read as unparseable:
            # int('1.0') raises, and losing a real reward to its formatting is a false 0.
            return int(float(raw)), ""
        except ValueError:
            return None, f"unreadable reward {raw!r}"

    def save_verifier_logs(self, container: str, spec: TaskSpec, repeat: int) -> None:
        for name in ("verifier.log", "pytest_output.txt", "app.log"):
            script = (
                "from pathlib import Path; import sys\n"
                f"p = Path('/logs/verifier/{name}')\n"
                "try:\n"
                "    with p.open('rb') as f:\n"
                "        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 65536))\n"
                "        sys.stdout.buffer.write(f.read(65536))\n"
                "except OSError as error:\n"
                "    print('unavailable: ' + type(error).__name__)\n"
            )
            try:
                saved = run(
                    ["docker", "exec", container, "python3", "-c", script],
                    timeout=30,
                    text=False,
                )
                content = saved.stdout if saved.returncode == 0 else b"unavailable: extraction failed"
            except subprocess.TimeoutExpired:
                content = b"unavailable: extraction timeout"
            (self.logs / f"{spec.name}.r{repeat}.{name}").write_bytes(content[-65536:])

    def post_mortem(self, container: str, spec: TaskSpec, deliverable: str, repeat: int) -> None:
        """Keep what a failed run left behind, before the container goes away."""
        listing = run(
            ["docker", "exec", container, "sh", "-c",
             f"ls -la {spec.workdir} 2>&1; echo '--- agent dir ---'; ls -la {AGENT_DIR} 2>&1"],
            timeout=60.0,
        )
        parts = [f"# workspace listing ({spec.workdir})", listing.stdout or listing.stderr]
        if deliverable and deliverable != "none":
            shown = run(
                ["docker", "exec", container, "sh", "-c", f"cat {deliverable} 2>&1 | head -c 4000"],
                timeout=60.0,
            )
            parts += [f"\n# claimed deliverable: {deliverable}", shown.stdout or shown.stderr]
        (self.logs / f"{spec.name}.r{repeat}.post").write_text(
            "\n".join(parts), encoding="utf-8"
        )

    def one(self, spec: TaskSpec, repeat: int) -> TaskResult:
        base = TaskResult(
            task=spec.name, repeat=repeat, reward=None, agent_exit=None, wall_s=0.0,
            tokens=None, steps=None, stop_reason="-", solved_claim="-", workspace="-",
            failure_mode="startup",
        )

        failed = self.build(spec)
        if failed:
            base.note = f"build failed: {failed}"
            return base

        container, error = self.start_container(spec)
        if container is None:
            base.note = f"run failed: {error}"
            return base

        try:
            error = self.install(container, spec)
            if error:
                base.note = f"install failed: {error}"
                return base

            self.counter_call("/__reset", "POST")
            proc, wall, killed = self.exec_agent(container, spec)
            counters = self.counter_call("/__counters", "GET")
            if counters:
                base.endpoint_tokens = counters.get("total_tokens")  # type: ignore[assignment]
                base.endpoint_requests = counters.get("requests")  # type: ignore[assignment]
                base.endpoint_late = counters.get("late_responses")  # type: ignore[assignment]
            base.wall_s = round(wall, 1)
            output = "" if proc is None else (proc.stdout or "") + (proc.stderr or "")
            log_file = self.logs / f"{spec.name}.r{repeat}.log"
            log_file.write_text(output, encoding="utf-8")
            base.log = str(log_file.relative_to(Path(self.args.out)))
            base.agent_exit = None if proc is None else proc.returncode
            base.timed_out = killed
            base.phases = phase_breakdown(output).render()

            done = DONE_LINE.search(output)
            deliverable = ""
            if done:
                base.tokens = int(done["tokens"])
                base.steps = int(done["steps"])
                base.stop_reason = done["stop"]
                base.solved_claim = done["solved"]
                deliverable = done["deliverable"]
            elif killed:
                base.stop_reason = "killed"
            ready = READY_LINE.search(output)
            if ready:
                base.workspace = ready["workspace"]

            trace = self.counter_call("/__exchanges", "GET")
            if trace and trace.get("exchanges"):
                base.wrote = (
                    "yes"
                    if any(
                        requested_write(item)
                        for item in trace["exchanges"]
                    )
                    else "no"
                )
                (self.logs / f"{spec.name}.r{repeat}.trace").write_text(
                    "\n".join(
                        f"--- call {item.get('call')} ---\n"
                        f"{item.get('content') or item.get('tool_calls')}"
                        for item in trace["exchanges"]
                    ),
                    encoding="utf-8",
                )

            # The per-call series and the sampled request bodies. Saved unconditionally
            # because the question they answer -- is the server truncating, or is the
            # agent not sending the history at all -- cannot be asked after the fact.
            calls = self.counter_call("/__calls", "GET")
            if calls and calls.get("calls"):
                (self.logs / f"{spec.name}.r{repeat}.calls").write_text(
                    json.dumps(calls, indent=1, ensure_ascii=False), encoding="utf-8"
                )

            installed = self.install_verifier(container, spec)
            try:
                if installed:
                    raise RuntimeError(installed)
                reward, note = self.verify(container, spec)
            except RuntimeError as error:
                reward, note = None, f"verifier install failed: {error}"
            except subprocess.TimeoutExpired:
                reward, note = None, "verifier timeout"
            finally:
                self.save_verifier_logs(container, spec, repeat)
            base.reward = reward
            if reward != 1:
                # The container is about to be destroyed, and a zero cannot be
                # diagnosed from a reward file alone: what the run actually wrote is
                # the evidence. Kept only for failures, so a green suite leaves no
                # clutter behind.
                self.post_mortem(container, spec, deliverable, repeat)
            if note:
                base.note = note
            base.failure_mode = classify_failure(base, killed=killed)
            return base
        finally:
            run(["docker", "rm", "-f", container], timeout=120.0)


def write_report(out: Path, suite: SuiteResult) -> None:
    data = asdict(suite)
    data["complete"] = suite.complete
    for saved, row in zip(data["rows"], suite.rows):
        saved["timed_out"] = row.timed_out or row.agent_exit in (124, 137) or row.stop_reason == "killed"
        saved["eligible_success"] = row.eligible_success
    (out / "suite.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "task\trepeat\treward\ttokens\tep_tokens\tep_calls\tep_late\twall_s\tsteps\tstop\t"
        "claim\twrote\tfailure\tphases\tnote\tagent_exit\ttimed_out\teligible_success"
    ]
    for row in suite.rows:
        lines.append(
            "\t".join(
                str(value)
                for value in (
                    row.task, row.repeat,
                    "?" if row.reward is None else row.reward,
                    "?" if row.tokens is None else row.tokens,
                    "?" if row.endpoint_tokens is None else row.endpoint_tokens,
                    "?" if row.endpoint_requests is None else row.endpoint_requests,
                    "?" if row.endpoint_late is None else row.endpoint_late,
                    row.wall_s,
                    "?" if row.steps is None else row.steps,
                    row.stop_reason, row.solved_claim, row.wrote, row.failure_mode,
                    row.phases, row.note, row.agent_exit,
                    row.timed_out or row.agent_exit in (124, 137) or row.stop_reason == "killed",
                    row.eligible_success,
                )
            )
        )
    (out / "results.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reserve_output(out: Path) -> None:
    """Claim an empty directory exclusively before any extraction or run."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise FileExistsError(f"output directory is not empty: {out}")
    # Exclusive creation also arbitrates two processes that both observed it empty.
    with (out / ".run-claimed").open("x"):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-root", default=str(ROOT / "local_task_extra" / "tasks"))
    parser.add_argument("--only", nargs="*", default=None, help="task names to run")
    parser.add_argument("--out", default=str(ROOT / "build" / "eval"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default="not-a-real-key")
    parser.add_argument("--network", default="sber-eval")
    parser.add_argument(
        "--counter-container", default="",
        help="name of a running counting_proxy container; empty disables the counter",
    )
    parser.add_argument("--reuse-images", action="store_true",
                        help="use existing local task images without building or pulling")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument(
        "--tell-agent-budget", action="store_true",
        help="also export AGENT_TIMEOUT_S; off by default because the runner never does",
    )
    parser.add_argument(
        "--controller",
        default="",
        choices=("", "loop", "macro"),
        help="AGENT_CONTROLLER for the run; empty leaves the archive's own default",
    )
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument("--archive", default=str(ROOT / "build" / "submission.zip"))
    args = parser.parse_args()

    if args.repeats < 1 or args.timeout_scale <= 0:
        parser.error("repeats and timeout-scale must be positive")
    out = Path(args.out)
    try:
        reserve_output(out)
    except FileExistsError as error:
        print(str(error), file=sys.stderr)
        return 2

    archive = Path(args.archive)
    if not archive.is_file():
        print(f"no archive at {archive}; run scripts/build_submission.py first", file=sys.stderr)
        return 2

    # Unpack with the same tool the organisers' documented path uses, so the exec bit
    # question stays visible here instead of being papered over by a python extract.
    unpacked = out / "unpacked"
    unpacked.mkdir(parents=True)
    unzipped = run(["unzip", "-q", str(archive), "-d", str(unpacked)])
    if unzipped.returncode != 0:
        print(f"unzip failed: {unzipped.stderr}", file=sys.stderr)
        return 1

    names = task_names(Path(args.tasks_root))
    if args.only:
        names = [n for n in names if n in set(args.only)]
    if not names:
        print("no tasks selected", file=sys.stderr)
        return 2

    suite = SuiteResult(
        label=args.label, model=args.model, base_url=args.base_url,
        timeout_scale=args.timeout_scale,
        started=time.strftime("%Y-%m-%d %H:%M:%S"), expected_tasks=names, repeats=args.repeats,
    )
    runner = Runner(args, unpacked)
    manifest = {
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "expected_tasks": names, "repeats": args.repeats, "model": args.model,
        "timeout_scale": args.timeout_scale, "controller": args.controller or "archive default",
        "tell_agent_budget": args.tell_agent_budget, "reuse_images": args.reuse_images,
        "sampling": "archive settings; request samples retained in logs", "images": {},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_report(out, suite)

    for name in names:
        spec = read_spec(Path(args.tasks_root) / name)
        for repeat in range(1, args.repeats + 1):
            row = runner.one(spec, repeat)
            suite.rows.append(row)
            print(
                f"{row.task:<32} r{row.repeat} reward={row.reward} "
                f"tokens={row.tokens} wall={row.wall_s}s stop={row.stop_reason} "
                f"failure={row.failure_mode} {row.note}",
                flush=True,
            )
            write_report(out, suite)

    eligible = sum(row.eligible_success for row in suite.rows)
    solved = sum(1 for row in suite.rows if row.reward == 1)
    tokens = sum(
        row.endpoint_tokens if row.endpoint_tokens is not None else (row.tokens or 0)
        for row in suite.rows
    )
    wall = sum(row.wall_s for row in suite.rows)
    print(
        f"\n{args.label}: solved {solved}/{len(suite.rows)}  "
        f"eligible {eligible}/{len(suite.rows)}  complete={suite.complete}  tokens {tokens}  wall {wall:.0f}s"
    )
    print(f"report: {out / 'results.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
