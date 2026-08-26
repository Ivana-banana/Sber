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
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

AGENT_DIR = "/opt/harbor/local-agent"
VERIFIER_DIR = "/opt/verifier"
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


@dataclass
class TaskSpec:
    """Everything the harness needs about one package, all read from the package."""

    name: str
    root: Path
    instruction: str
    workdir: str
    has_entrypoint: bool
    agent_timeout_s: float
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

    note: str = ""
    log: str = ""


@dataclass
class SuiteResult:
    label: str
    model: str
    base_url: str
    timeout_scale: float
    started: str
    rows: list[TaskResult] = field(default_factory=list)


def run(cmd: list[str], *, timeout: float = DOCKER_STEP_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
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
        cpus=float(environment.get("cpus", 1)),
        memory_mb=int(environment.get("memory_mb", 512)),
    )


def classify_failure(result: TaskResult, *, killed: bool) -> str:
    """Put every zero in exactly one bucket from CLAUDE.md's taxonomy.

    Order matters: a run that was killed is a `timeout` even if it also never reached
    a verifier, because the timeout is the thing that has to be fixed first.
    """
    if result.reward == 1:
        return "-"
    if result.agent_exit is None:
        return "startup"
    if killed:
        return "timeout"
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
        built = run(
            ["docker", "build", "-q", "-t", self.image_tag(spec), str(spec.root / "environment")]
        )
        if built.returncode != 0:
            return built.stderr.strip()[-400:]
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
        for source, target in (
            (self.unpacked, AGENT_DIR),
            (spec.root / "tests", VERIFIER_DIR),
        ):
            mk = run(["docker", "exec", container, "mkdir", "-p", target])
            if mk.returncode != 0:
                return f"mkdir {target}: {mk.stderr.strip()[-200:]}"
            copied = run(["docker", "cp", f"{source}/.", f"{container}:{target}"])
            if copied.returncode != 0:
                return f"cp {target}: {copied.stderr.strip()[-200:]}"
        return ""

    def exec_agent(self, container: str, spec: TaskSpec) -> tuple[subprocess.CompletedProcess[str] | None, float, bool]:
        budget = spec.agent_timeout_s * self.args.timeout_scale
        env = [
            "-e", f"LOCAL_AGENT_MODEL={self.args.model}",
            "-e", f"OPENAI_BASE_URL={self.args.base_url}",
            "-e", f"OPENAI_API_KEY={self.args.api_key}",
        ]
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
            ["docker", "exec", container, "bash", f"{VERIFIER_DIR}/test.sh"],
            timeout=180.0,
        )
        reward = run(["docker", "exec", container, "cat", REWARD_PATH])
        if reward.returncode != 0:
            return None, f"no reward file; test.sh said {checked.stderr.strip()[-200:]}"
        try:
            return int(reward.stdout.strip()), ""
        except ValueError:
            return None, f"unreadable reward {reward.stdout.strip()!r}"

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
                (self.logs / f"{spec.name}.r{repeat}.trace").write_text(
                    "\n".join(
                        f"--- call {item.get('call')} ---\n"
                        f"{item.get('content') or item.get('tool_calls')}"
                        for item in trace["exchanges"]
                    ),
                    encoding="utf-8",
                )

            reward, note = self.verify(container, spec)
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
    (out / "suite.json").write_text(
        json.dumps(asdict(suite), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "task\trepeat\treward\ttokens\tep_tokens\tep_calls\tep_late\twall_s\tsteps\tstop\tclaim\tfailure\tnote"
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
                    row.stop_reason, row.solved_claim, row.failure_mode, row.note,
                )
            )
        )
    (out / "results.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument(
        "--tell-agent-budget", action="store_true",
        help="also export AGENT_TIMEOUT_S; off by default because the runner never does",
    )
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument("--archive", default=str(ROOT / "build" / "submission.zip"))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    archive = Path(args.archive)
    if not archive.is_file():
        print(f"no archive at {archive}; run scripts/build_submission.py first", file=sys.stderr)
        return 2

    # Unpack with the same tool the organisers' documented path uses, so the exec bit
    # question stays visible here instead of being papered over by a python extract.
    unpacked = out / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir(parents=True)
    unzipped = run(["unzip", "-q", str(archive), "-d", str(unpacked)])
    if unzipped.returncode != 0:
        print(f"unzip failed: {unzipped.stderr}", file=sys.stderr)
        return 1

    names = sorted(p.name for p in Path(args.tasks_root).iterdir() if p.is_dir())
    if args.only:
        names = [n for n in names if n in set(args.only)]
    if not names:
        print("no tasks selected", file=sys.stderr)
        return 2

    suite = SuiteResult(
        label=args.label, model=args.model, base_url=args.base_url,
        timeout_scale=args.timeout_scale,
        started=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    runner = Runner(args, unpacked)

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

    solved = sum(1 for row in suite.rows if row.reward == 1)
    tokens = sum(
        row.endpoint_tokens if row.endpoint_tokens is not None else (row.tokens or 0)
        for row in suite.rows
    )
    wall = sum(row.wall_s for row in suite.rows)
    print(
        f"\n{args.label}: solved {solved}/{len(suite.rows)}  "
        f"tokens {tokens}  wall {wall:.0f}s"
    )
    print(f"report: {out / 'results.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
