"""What the task text names, and what the run is therefore allowed to touch.

E009 measured the failure this module exists to stop: on a package whose image
`WORKDIR` is not `/app`, the run wrote its deliverable into the agent's own source
tree, verified it there and reported success against a verifier that scored zero.

The tests below are grouped by the two questions `agent/paths.py` keeps apart -- what
the task named, and which of those could be a workspace -- because the first fix
conflated them and review broke it on that seam.
"""

from __future__ import annotations

from pathlib import Path

from agent.paths import named_paths, permits, workspace_candidates, writable_paths


# --- what the task named ----------------------------------------------------


def test_paths_are_returned_in_the_order_the_task_names_them() -> None:
    found = named_paths("Read /app/one.py and /app/two.py, then write /out/report.json.")
    assert found == (Path("/app/one.py"), Path("/app/two.py"), Path("/out/report.json"))


def test_a_url_contributes_no_path() -> None:
    # The colon before a port ends the token, so the tail of a URL reads as an
    # absolute path. Network tasks name a URL in nearly every instruction.
    assert named_paths("Query http://127.0.0.1:8080/tmp/report for the value.") == ()
    assert named_paths("Query http://api/var/status and stop.") == ()


def test_sentence_punctuation_is_not_part_of_the_path() -> None:
    found = named_paths("Write `/srv/out.txt`, then check /var/lib/case/report.json.")
    assert found == (Path("/srv/out.txt"), Path("/var/lib/case/report.json"))


def test_a_repeated_path_is_named_once() -> None:
    assert named_paths("/app/x and /app/x again") == (Path("/app/x"),)


def test_truncation_never_invents_a_shorter_path() -> None:
    """A cut through the middle of a path would vote for its parent.

    Observed in review: a truncated `/srv/bootstrap` becomes `/srv`, which exists,
    outranks nothing and yet counts. The cut is taken at whitespace instead.
    """
    from agent import paths

    filler = "x " * (paths.SCAN_LIMIT_CHARS // 2)
    found = named_paths(f"{filler}/srv/bootstrap/marker.txt")
    assert all("srv" not in str(path) or str(path).endswith("marker.txt") for path in found)


# --- which of them could be a workspace -------------------------------------


def test_the_directory_a_deliverable_names_counts_through_its_parent(tmp_path: Path) -> None:
    task = tmp_path / "srv" / "bootstrap"
    task.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    assert workspace_candidates(f"Create {task}/farewell.marker", agent_dir) == [task]


def test_a_directory_two_levels_below_anything_that_exists_still_resolves(tmp_path: Path) -> None:
    # "Create the directory and write the report into it" is ordinary phrasing. Only
    # lifting one level would give this instruction no candidate at all.
    root = tmp_path / "cases" / "open"
    root.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    assert workspace_candidates(f"Write {root}/new/deeper/report.json", agent_dir) == [root]


def test_a_bare_top_level_directory_is_never_a_workspace() -> None:
    agent_dir = Path("/opt/harbor/local-agent")
    for top in ("/tmp", "/var", "/srv", "/opt", "/root", "/etc"):
        assert workspace_candidates(f"Leave it in {top}/summary.txt", agent_dir) == [], top


def test_the_operating_systems_own_trees_are_not_workspaces() -> None:
    agent_dir = Path("/opt/harbor/local-agent")
    instruction = "Run /usr/bin/python3 and /bin/sh; read /usr/share/doc/readme."
    assert workspace_candidates(instruction, agent_dir) == []


def test_a_project_directory_under_usr_is_still_a_workspace(tmp_path: Path) -> None:
    """`/usr/src/app` is the commonest non-`/app` WORKDIR convention there is.

    The first version of this rule banned `/usr` wholesale, which excluded it while
    leaving `/tmp` and `/root` selectable -- inverted against where tasks live.
    """
    project = tmp_path / "usr" / "src" / "app"
    project.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    assert workspace_candidates(f"Fix {project}/index.js", agent_dir) == [project]


def test_the_most_named_directory_wins(tmp_path: Path) -> None:
    # Ranking by mentions is right for a workspace: the directory a task names again
    # and again is the one it wants read. Where the answer goes is a separate question.
    inputs = tmp_path / "app" / "src"
    inputs.mkdir(parents=True)
    output = tmp_path / "out" / "reports"
    output.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    ranked = workspace_candidates(
        f"Audit {inputs}/a.py and {inputs}/b.py, then write {output}/report.json",
        agent_dir,
    )
    assert ranked[0] == inputs


def test_our_own_tree_is_never_a_workspace(tmp_path: Path) -> None:
    agent_dir = tmp_path / "opt" / "harbor" / "local-agent"
    (agent_dir / "agent").mkdir(parents=True)

    assert workspace_candidates(f"Edit {agent_dir}/agent/loop.py", agent_dir) == []
    assert workspace_candidates(f"Look in {tmp_path / 'opt'}/notes", agent_dir) == []


# --- the write policy -------------------------------------------------------


def test_a_path_the_task_named_is_writable_even_outside_the_workspace(tmp_path: Path) -> None:
    """The whole point. This is the E009 zero, in one assertion.

    The workspace guess was wrong, the task named the deliverable explicitly, and
    refusing the write turns a correct action into an artifact in the wrong place.
    """
    workspace = tmp_path / "agent"
    workspace.mkdir()
    target = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    target.parent.mkdir(parents=True)

    assert permits(target, workspace, (target,))


def test_anything_under_a_named_directory_is_writable(tmp_path: Path) -> None:
    workspace = tmp_path / "agent"
    workspace.mkdir()
    named = tmp_path / "var" / "lib" / "case"
    named.mkdir(parents=True)

    assert permits(named / "conclusion.env", workspace, (named,))


def test_a_path_nobody_named_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "agent"
    workspace.mkdir()
    named = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    named.parent.mkdir(parents=True)

    assert not permits(tmp_path / "elsewhere" / "loot.txt", workspace, (named,))


def test_a_sibling_of_a_named_file_is_refused(tmp_path: Path) -> None:
    # Naming one file does not open its directory: the task asked for that file.
    workspace = tmp_path / "agent"
    workspace.mkdir()
    named = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    named.parent.mkdir(parents=True)

    assert not permits(named.parent / "other.txt", workspace, (named,))


def test_the_workspace_itself_stays_writable(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    (workspace / "deep").mkdir(parents=True)

    assert permits(workspace / "deep" / "file.txt", workspace, ())


def test_a_real_home_directory_is_not_a_workspace() -> None:
    """Checked against a directory that exists, not one that happens not to.

    The first version of this test named `/home/analyst/case`, which no machine has,
    so it passed through the "must exist" rule and never touched the rule it claimed
    to test. Review caught it; on the same machine `/home/<user>` was selected.
    """
    home = Path("/home")
    if not home.is_dir():
        return  # not a Linux layout; the target image is, and CI on it covers this
    existing = [child for child in home.iterdir() if child.is_dir()]
    if not existing:
        return
    agent_dir = Path("/opt/harbor/local-agent")

    assert workspace_candidates(f"The case files are in {existing[0]}/case", agent_dir) == []


def test_a_named_system_path_is_not_writable() -> None:
    """The write policy needs its own guard, not the workspace rule's.

    Review demonstrated `permits(Path("/etc/ld.so.preload"), ...)` returning true for
    an instruction that merely named the file, on an agent that runs as root.
    """
    agent_dir = Path("/opt/harbor/local-agent")
    policy = writable_paths(
        "Write the loader list to /etc/ld.so.preload and read /usr/bin/python3.",
        agent_dir,
    )

    assert policy == ()


def test_the_task_text_cannot_open_the_agents_own_source(tmp_path: Path) -> None:
    agent_dir = tmp_path / "opt" / "harbor" / "local-agent"
    (agent_dir / "agent").mkdir(parents=True)
    (agent_dir / "agent" / "loop.py").write_text("x", encoding="utf-8")

    policy = writable_paths(f"Our code lives in {agent_dir}/agent/loop.py.", agent_dir)

    assert policy == ()


def test_a_path_the_task_names_is_still_in_the_policy(tmp_path: Path) -> None:
    # The filter must not empty the policy it exists to bound.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    target = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    target.parent.mkdir(parents=True)

    assert writable_paths(f"Create {target} containing Ciao.", agent_dir) == (target,)
