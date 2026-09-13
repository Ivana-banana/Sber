"""The map the agent gets for free, before it spends a single token.

Preflight exists because the alternative is paying a small model to discover, one
tool call at a time, facts that `os.scandir` answers instantly and correctly. Its
whole contract is: deterministic, no LLM, bounded, and never a crash on a workspace
that does not look the way we expected.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent import limits
from agent.preflight import inspect_environment

ENV = {
    "LOCAL_AGENT_MODEL": "small-local",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENAI_API_KEY": "sk-not-a-real-key-0123456789",
}


def make_task(root: Path) -> Path:
    workspace = root / "app"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "README.md").write_text("# task\n", encoding="utf-8")
    (workspace / "src" / "login.py").write_text("import sqlite3\n", encoding="utf-8")
    (workspace / "tests" / "test_login.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return workspace


def test_the_brief_names_the_workspace_and_its_entries(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)

    brief = inspect_environment(workspace, ENV)

    assert str(workspace) in brief.render()
    assert "README.md" in brief.render()
    assert "src/" in brief.render()


def test_the_brief_reports_which_interpreters_and_utilities_exist(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)

    brief = inspect_environment(workspace, ENV)

    assert "python3" in brief.render(), "the one dependency every playbook assumes"
    assert brief.available, "at least the running interpreter must be discoverable"


def test_the_brief_names_the_utilities_the_image_does_not_have(tmp_path: Path, monkeypatch) -> None:
    """Absence is the half of the toolchain answer the model cannot look up.

    The target image has no `sqlite3` and no `xxd`, measured twice by execution. A
    forensics playbook reaches for both by reflex, and a brief that only lists what
    exists leaves the model to discover that with a failed command and a recovery
    step. Saying it once is cheaper than paying for it every run.
    """
    workspace = make_task(tmp_path)
    monkeypatch.setattr(
        "agent.preflight.shutil.which",
        lambda name: None if name in {"sqlite3", "xxd"} else f"/usr/bin/{name}",
    )

    brief = inspect_environment(workspace, ENV)
    rendered = brief.render()

    assert "sqlite3" in brief.absent and "xxd" in brief.absent
    assert "absent: " in rendered
    assert "rg" in brief.available, "the image has ripgrep; a tree-wide grep is the worse plan"
    assert "patch" in brief.available and "diff" in brief.available


def test_the_brief_stays_small_enough_to_pay_for_on_every_call(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    for i in range(400):
        (workspace / f"evidence-{i:04d}.log").write_text("x", encoding="utf-8")

    brief = inspect_environment(workspace, ENV)
    rendered = brief.render()

    assert len(rendered) <= limits.BRIEF_MAX_CHARS
    assert "more" in rendered, "a cut list must say it was cut"


def test_the_brief_never_carries_the_endpoint_key(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)
    (workspace / "leak.txt").write_text(ENV["OPENAI_API_KEY"], encoding="utf-8")

    rendered = inspect_environment(workspace, ENV).render()

    assert ENV["OPENAI_API_KEY"] not in rendered


def test_a_workspace_that_does_not_exist_still_produces_a_brief(tmp_path: Path) -> None:
    brief = inspect_environment(tmp_path / "gone", ENV)

    assert brief.render()
    assert "missing" in brief.render()


def test_a_failing_source_index_does_not_stop_the_brief(tmp_path: Path, monkeypatch) -> None:
    # The import index is a convenience, not a precondition for starting. Whatever it
    # does on an untrusted tree, preflight must still return a brief rather than let a
    # traceback out before the first LLM call.
    workspace = make_task(tmp_path)

    def boom(*args: object, **kwargs: object) -> object:
        raise KeyError("index blew up on a tree it did not expect")

    monkeypatch.setattr("agent.preflight.local_sources", boom)
    monkeypatch.setattr("agent.preflight.source_excerpts", boom)

    brief = inspect_environment(workspace, ENV)

    assert brief.render()


def test_a_workspace_we_may_not_read_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    workspace = make_task(tmp_path)

    def deny(*args: object, **kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr("agent.preflight.os.scandir", deny)

    brief = inspect_environment(workspace, ENV)

    assert "unreadable" in brief.render()


def test_one_unreadable_subdirectory_does_not_blank_the_whole_tree(
    tmp_path: Path, monkeypatch
) -> None:
    # The walk used to return on the first PermissionError wherever it happened, so a
    # single root-owned or mode-000 subdirectory threw away every entry still queued
    # and labelled the whole workspace unreadable. The brief is the only free map the
    # model gets; losing it over one closed door is expensive and, worse, silent.
    workspace = make_task(tmp_path)
    closed = workspace / "private"
    closed.mkdir()
    (closed / "inside.txt").write_text("x", encoding="utf-8")
    real_scandir = os.scandir

    def selective(path: object, *args: object, **kwargs: object) -> object:
        if Path(str(path)) == closed:
            raise PermissionError("denied")
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("agent.preflight.os.scandir", selective)

    rendered = inspect_environment(workspace, ENV).render()

    assert "state: unreadable" not in rendered, "the workspace itself is readable"
    assert "README.md" in rendered, "entries queued behind the closed door survive"
    assert "src/" in rendered
    assert "private/ (unreadable)" in rendered, "and the model is told which door is shut"


def test_nested_directories_are_summarised_but_not_walked_forever(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    deep = workspace
    for level in range(12):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (deep / "buried.txt").write_text("x", encoding="utf-8")

    rendered = inspect_environment(workspace, ENV).render()

    assert "level0/" in rendered
    assert "level9" not in rendered, "depth is capped; the loop searches the rest on demand"


def test_an_absent_tool_with_a_verified_substitute_names_it(tmp_path: Path, monkeypatch) -> None:
    # Reporting `sqlite3: absent` only tells the model its first plan is dead. The
    # substitutes below were run inside secureintelligent/acp on 2026-08-26, so the
    # brief can hand it the second plan in the same breath instead of paying for a
    # failed call and a recovery step.
    workspace = make_task(tmp_path)
    monkeypatch.setattr("agent.preflight.shutil.which", lambda name: None)

    rendered = inspect_environment(workspace, ENV).render()

    assert "instead:" in rendered
    assert "od -A x -t x1z" in rendered, "xxd"
    assert "tcpdump -nn -r" in rendered, "tshark"
    assert "nc -z" in rendered, "nmap"
    assert "objdump -d" in rendered, "gdb"


def test_a_tool_that_is_present_is_never_offered_a_substitute(tmp_path: Path, monkeypatch) -> None:
    # The substitution line is priced on every request. On an image where the real
    # utilities exist it must disappear entirely, not degrade to advice nobody needs.
    workspace = make_task(tmp_path)
    monkeypatch.setattr("agent.preflight.shutil.which", lambda name: f"/usr/bin/{name}")

    rendered = inspect_environment(workspace, ENV).render()

    assert "absent:" not in rendered
    assert "instead:" not in rendered


def test_only_a_measured_substitute_is_offered(tmp_path: Path, monkeypatch) -> None:
    # A tool absent with no verified replacement is reported absent and nothing more.
    # Guessing a second plan is worse than admitting there is none.
    workspace = make_task(tmp_path)
    monkeypatch.setattr(
        "agent.preflight.shutil.which", lambda name: None if name == "jq" else f"/usr/bin/{name}"
    )

    rendered = inspect_environment(workspace, ENV).render()

    assert "absent: jq" in rendered
    assert "instead:" not in rendered


# --- Recon packet: listening sockets and instruction literals ------------------
#
# Added for R2 after the E060 artifact review. Two of the three near-misses there
# were the same mistake: `ctf-duplicate-parameter` and `ctf-signed-cookie` each wrote
# the exactly correct flag but never issued it from the service, and the trace shows
# the agent trying to start a server that was already listening four times in a row.
# A port table is one read of /proc; paying a 7B model to rediscover it is the
# expensive way to get it wrong.


def write_proc(root: Path, ipv4: str = "", ipv6: str = "") -> tuple[str, ...]:
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt\n"
    (root / "tcp").write_text(header + ipv4, encoding="utf-8")
    (root / "tcp6").write_text(header + ipv6, encoding="utf-8")
    return (str(root / "tcp"), str(root / "tcp6"))


def listen_line(local: str, state: str = "0A") -> str:
    return f"   0: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000\n"


def test_the_brief_names_the_local_services_already_listening(tmp_path: Path, monkeypatch) -> None:
    """`127.0.0.1:9139` is a fact about the machine, not something to deduce."""
    workspace = make_task(tmp_path)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(
        "agent.preflight.PROC_NET_TCP",
        write_proc(proc, ipv4=listen_line("0100007F:23B3")),
    )

    rendered = inspect_environment(workspace, ENV).render()

    assert "127.0.0.1:9139" in rendered


def test_a_connection_that_is_not_listening_is_not_reported(tmp_path: Path, monkeypatch) -> None:
    # State 01 is ESTABLISHED. Reporting it as a service invites the model to talk to
    # a socket that will not answer, which costs a step and a recovery.
    workspace = make_task(tmp_path)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(
        "agent.preflight.PROC_NET_TCP",
        write_proc(proc, ipv4=listen_line("0100007F:23B3", state="01")),
    )

    rendered = inspect_environment(workspace, ENV).render()

    assert "9139" not in rendered
    assert "listening:" not in rendered


def test_ipv6_sockets_are_decoded_and_not_printed_as_hex(tmp_path: Path, monkeypatch) -> None:
    workspace = make_task(tmp_path)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(
        "agent.preflight.PROC_NET_TCP",
        write_proc(proc, ipv6=listen_line("00000000000000000000000001000000:1F90")),
    )

    rendered = inspect_environment(workspace, ENV).render()

    assert "::1:8080" in rendered


def test_a_kernel_without_proc_net_is_reported_as_no_ports_not_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = make_task(tmp_path)
    monkeypatch.setattr("agent.preflight.PROC_NET_TCP", (str(tmp_path / "nope"),))

    rendered = inspect_environment(workspace, ENV).render()

    assert "listening:" not in rendered, "silence beats a line that says nothing"


def test_the_literals_the_task_quotes_are_located_in_the_tree(tmp_path: Path) -> None:
    """`grep -rn` for the task's own backticks, spent once instead of per step.

    `mut-audit-query-gate` says "include the actual `login_name` data flow in `proof`"
    and the checker requires exactly that. Handing the model the line number is the
    difference between reading the flow and inventing it.
    """
    workspace = make_task(tmp_path)
    (workspace / "src" / "login.py").write_text(
        "import sqlite3\n\n\ndef authorize(login_name):\n    return login_name\n", encoding="utf-8"
    )

    rendered = inspect_environment(
        workspace, ENV, instruction="Include the `login_name` data flow in `proof`."
    ).render()

    assert "src/login.py:4" in rendered


def test_a_literal_that_is_nowhere_in_the_tree_costs_nothing(tmp_path: Path) -> None:
    """A miss is silence. Naming it was a draft of this section and it was wrong twice.

    `ctf-duplicate-parameter` quotes `127.0.0.1:9139`, which the `listening:` line has
    already answered, so a "not found" line contradicts the brief one line above it;
    and an audit task quotes the field names of the report it is being asked to write,
    which are missing for the least interesting reason there is.
    """
    workspace = make_task(tmp_path)

    rendered = inspect_environment(
        workspace, ENV, instruction="Save the proof string to `/app/flag.txt`."
    ).render()

    assert "quoted:" not in rendered
    assert "/app/flag.txt" not in rendered


def test_a_task_with_no_backticks_adds_no_literal_section(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)

    rendered = inspect_environment(workspace, ENV, instruction="Fix the bug.").render()

    assert "quoted:" not in rendered


def test_binary_files_are_not_scanned_for_literals(tmp_path: Path) -> None:
    # A packet capture or a core dump will contain any short string by accident, and
    # decoding one as text is both a wrong hit and a wasted read.
    workspace = make_task(tmp_path)
    (workspace / "capture.pcap").write_bytes(b"\x00\x01needle\x00" * 64)

    rendered = inspect_environment(
        workspace, ENV, instruction="Find the `needle` in the capture."
    ).render()

    assert "quoted:" not in rendered, "the only file holding the string is binary"
    assert "capture.pcap:" not in rendered, "and no line number is claimed inside it"


def test_literal_scan_skips_a_fifo_without_blocking(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)
    os.mkfifo(workspace / "waiting.pipe")
    code = (
        "from pathlib import Path; "
        "from agent.preflight import _locate; "
        f"assert _locate(Path({str(workspace)!r}), ['needle']) == []"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        timeout=2,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_literal_scan_does_not_follow_file_symlinks_outside_workspace(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n", encoding="utf-8")
    (workspace / "outside-link.txt").symlink_to(outside)

    rendered = inspect_environment(workspace, ENV, instruction="Find `needle`.").render()

    assert "outside-link.txt:" not in rendered


def test_the_recon_sections_stay_inside_the_resident_budget(tmp_path: Path, monkeypatch) -> None:
    """Everything here is charged on every request, so bounds are the contract."""
    workspace = tmp_path / "app"
    (workspace / "src").mkdir(parents=True)
    for i in range(200):
        (workspace / "src" / f"mod{i:03d}.py").write_text("needle = 1\n" * 40, encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(
        "agent.preflight.PROC_NET_TCP",
        write_proc(proc, ipv4="".join(listen_line(f"0100007F:{port:04X}") for port in range(1, 60))),
    )
    instruction = " ".join(f"`needle{i}` and `needle`" for i in range(40))

    rendered = inspect_environment(workspace, ENV, instruction=instruction).render()

    assert len(rendered) <= limits.BRIEF_MAX_CHARS


def test_the_workspace_path_is_not_looked_up_as_a_literal(tmp_path: Path) -> None:
    # Every task names its own workdir in backticks, and a search for it finds the
    # `WORKDIR` and `COPY` lines of the image's Dockerfile and nothing else. The first
    # line of the brief already says where the workspace is.
    workspace = make_task(tmp_path)
    (workspace / "Dockerfile").write_text(f"FROM python\nWORKDIR {workspace}\n", encoding="utf-8")

    rendered = inspect_environment(
        workspace, ENV, instruction=f"You are working under `{workspace}`. Fix it."
    ).render()

    assert "quoted:" not in rendered


def test_the_brief_names_the_module_the_entry_point_reaches(tmp_path: Path) -> None:
    # N3: the tree above lists the directory; this says which file inside it the
    # request actually reaches, which is the hop thirty saved runs never made.
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "routers" / "auth.py").write_text("def login():\n    pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from routers import auth\n", encoding="utf-8")

    brief = inspect_environment(tmp_path, {}, "audit this application")

    assert ("routers/auth.py", "main.py", "login") in brief.sources
    assert "routers/auth.py ← main.py: login" in brief.render()


def test_the_brief_carries_the_body_behind_the_module_it_names(tmp_path: Path) -> None:
    # P2b: naming `routers/auth.py` left the model one action short of the evidence,
    # and in the P2a pilot it did not take that action.
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "routers" / "auth.py").write_text(
        '@router.post("/login")\ndef login(name):\n    return conn.fetchrow(f"select {name}")\n',
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("from routers import auth\n", encoding="utf-8")

    rendered = inspect_environment(tmp_path, {}, "audit this application").render()

    assert "routers/auth.py:1-3" in rendered
    assert "conn.fetchrow" in rendered


def test_a_large_tree_does_not_swallow_the_excerpts(tmp_path: Path) -> None:
    # The map is capped; the excerpts have their own budget. Sharing one cap would let
    # a wide directory listing decide whether the source reaches the model at all.
    for index in range(40):
        (tmp_path / f"data_{index}_with_a_long_name.txt").write_text("x", encoding="utf-8")
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "views" / "session.py").write_text(
        '@app.get("/s")\ndef start(name):\n    return conn.fetchrow(f"select {name}")\n',
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text("from views import session\n", encoding="utf-8")

    rendered = inspect_environment(tmp_path, {}, "audit this application").render()

    assert "views/session.py:1-3" in rendered
