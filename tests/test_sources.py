"""The local import index: what the entry point hands its work to.

N3. Thirty saved SQLi runs saw the handlers directory in their first listing and none
of them opened it, so these tests are about one thing -- does the brief name the file
the request actually reaches -- and about the two ways that could be cheating: knowing
a filename in advance, or learning it by running the tree under inspection.
"""

from __future__ import annotations

from pathlib import Path

from agent import limits
from agent.sources import local_sources, source_excerpts


def make_app(root: Path, *, entry: str = "main.py", handler: str = "routers/auth.py") -> None:
    (root / handler).parent.mkdir(parents=True, exist_ok=True)
    package = Path(handler).parent.as_posix()
    module = Path(handler).stem
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / entry).write_text(
        f"from {package} import {module}\n\napp = {module}.router\n", encoding="utf-8"
    )
    (root / handler).write_text(
        "def login(name):\n    return db.execute(f\"select * from u where n='{name}'\")\n",
        encoding="utf-8",
    )


def test_the_handler_behind_the_entry_point_is_named(tmp_path: Path) -> None:
    make_app(tmp_path)

    rows = local_sources(tmp_path)

    assert ("routers/auth.py", "main.py", "login") in rows


def test_the_index_does_not_know_any_of_these_names_in_advance(tmp_path: Path) -> None:
    # Same shape, every name changed. A router that only works on the public tree is
    # a hardcoded answer wearing an algorithm's clothes.
    make_app(tmp_path, entry="service.py", handler="views/session.py")

    rows = local_sources(tmp_path)

    assert ("views/session.py", "service.py", "login") in rows


def test_the_tree_under_inspection_is_never_executed(tmp_path: Path) -> None:
    # The workspace is the untrusted material of the task. Importing it to index it
    # would be running it, which is the one thing this must never do.
    marker = tmp_path / "executed.txt"
    (tmp_path / "helper.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text("import helper\n", encoding="utf-8")

    rows = local_sources(tmp_path)

    assert marker.exists() is False
    assert any(path == "helper.py" for path, _, _ in rows)


def test_a_vendored_dependency_is_not_the_application(tmp_path: Path) -> None:
    vendor = tmp_path / ".venv" / "lib" / "site-packages" / "fastapi"
    vendor.mkdir(parents=True)
    (vendor / "__init__.py").write_text("def get(path):\n    return path\n", encoding="utf-8")
    make_app(tmp_path)

    rows = local_sources(tmp_path)

    assert all(".venv" not in path for path, _, _ in rows)


def test_a_file_that_does_not_parse_costs_one_row_not_the_index(tmp_path: Path) -> None:
    make_app(tmp_path)
    (tmp_path / "broken.py").write_text("def (((\n", encoding="utf-8")

    rows = local_sources(tmp_path)

    assert ("routers/auth.py", "main.py", "login") in rows


def test_an_imported_module_that_does_not_parse_does_not_end_the_index(tmp_path: Path) -> None:
    # The entry point imports a module the tree cannot parse (a Python-2 sample, a
    # SyntaxError -- exactly the defect a fix task hands us). It is in the import index
    # but not among the parsed trees, so a bare `parsed[target]` raises KeyError before
    # the first LLM call. The module is skipped; the rest of the tree still indexes.
    (tmp_path / "main.py").write_text("import broken\nimport helper\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text('print "python 2"\n', encoding="utf-8")
    (tmp_path / "helper.py").write_text("def work():\n    return 1\n", encoding="utf-8")

    rows = local_sources(tmp_path)

    assert ("helper.py", "main.py", "work") in rows
    assert all(row[0] != "broken.py" for row in rows), "an unparsed module has no row"


def test_a_standalone_script_nothing_imports_is_not_reported(tmp_path: Path) -> None:
    (tmp_path / "notes.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert local_sources(tmp_path) == ()


def test_the_index_is_bounded(tmp_path: Path) -> None:
    names = [f"mod{index}" for index in range(limits.SOURCE_INDEX_MAX_ROWS + 10)]
    (tmp_path / "main.py").write_text(
        "".join(f"import {name}\n" for name in names), encoding="utf-8"
    )
    for name in names:
        (tmp_path / f"{name}.py").write_text(
            "".join(f"def f{index}():\n    pass\n" for index in range(20)), encoding="utf-8"
        )

    rows = local_sources(tmp_path)

    assert len(rows) == limits.SOURCE_INDEX_MAX_ROWS
    assert all(len(names.split(", ")) <= limits.SOURCE_INDEX_MAX_NAMES for _, _, names in rows)


def test_a_relative_import_resolves_inside_its_package(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("from . import handler\n", encoding="utf-8")
    (package / "handler.py").write_text("def handle():\n    pass\n", encoding="utf-8")

    rows = local_sources(tmp_path)

    assert ("app/handler.py", "app/main.py", "handle") in rows


def make_service(
    root: Path,
    *,
    entry: str = "service.py",
    handler: str = "views/session.py",
    function: str = "sign_in",
    body: str = '    return conn.fetchrow(f"select id from u where n=\'{name}\'")',
) -> None:
    """A neutral two-module application: an entry point that reaches one handler."""
    (root / handler).parent.mkdir(parents=True, exist_ok=True)
    package = Path(handler).parent.as_posix()
    module = Path(handler).stem
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / entry).write_text(f"from {package} import {module}\n", encoding="utf-8")
    (root / handler).write_text(
        f'@route("/in")\ndef {function}(name):\n{body}\n', encoding="utf-8"
    )


def test_the_excerpt_carries_the_reached_body_and_says_where_it_came_from(tmp_path: Path) -> None:
    # P2a: the index named `routers/auth.py` and the agent still never opened it. A
    # name it has to act on is a name it can skip; the text is the evidence itself.
    make_service(tmp_path)

    text = source_excerpts(tmp_path, max_chars=1600)

    assert "views/session.py:1-3" in text
    assert "def sign_in(name):" in text
    assert "conn.fetchrow" in text


def test_the_selection_knows_none_of_these_names_in_advance(tmp_path: Path) -> None:
    make_service(
        tmp_path, entry="wsgi.py", handler="api/orders.py", function="place",
        body='    return cursor.execute("insert into orders values (%s)", (name,))',
    )

    text = source_excerpts(tmp_path, max_chars=1600)

    assert "api/orders.py:1-3" in text
    assert "def place(name):" in text


def test_a_parameterized_call_is_offered_on_the_same_terms_as_an_interpolated_one(
    tmp_path: Path,
) -> None:
    # Selecting only what looks unsafe would be the audit's answer smuggled into the
    # brief, and would hide the safe handler a "no findings" report has to rest on.
    safe = tmp_path / "safe"
    unsafe = tmp_path / "unsafe"
    safe.mkdir()
    unsafe.mkdir()
    make_service(safe, body='    return conn.fetchrow("select id from u where n=$1", name)')
    make_service(unsafe)

    kept = source_excerpts(safe, max_chars=1600)
    other = source_excerpts(unsafe, max_chars=1600)

    assert "def sign_in(name):" in kept
    assert kept.count("views/session.py:") == other.count("views/session.py:")


def test_the_budget_holds_and_a_cut_says_so(tmp_path: Path) -> None:
    make_service(tmp_path, body="\n".join(f"    step_{n}()" for n in range(200)))

    text = source_excerpts(tmp_path, max_chars=400)

    assert len(text) <= 400
    assert "…" in text or "more" in text


def test_a_symlink_out_of_the_workspace_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def leak():\n    return 'canary-42'\n", encoding="utf-8")
    root = tmp_path / "app"
    root.mkdir()
    make_service(root)
    try:
        (root / "linked.py").symlink_to(outside / "secret.py")
    except OSError:  # pragma: no cover - platforms without symlink permission
        return

    text = source_excerpts(root, max_chars=1600)

    assert "canary-42" not in text


def test_an_unreadable_neighbour_does_not_end_the_scan(tmp_path: Path) -> None:
    make_service(tmp_path)
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    (tmp_path / "blob.py").write_bytes(b"\x00\x01\x02binary\n")
    (tmp_path / "huge.py").write_text("x = 1\n" * 300_000, encoding="utf-8")

    text = source_excerpts(tmp_path, max_chars=1600)

    assert "def sign_in(name):" in text


def test_an_import_cycle_terminates(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import b\n\ndef one():\n    return b.two()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n\ndef two():\n    return a.one()\n", encoding="utf-8")

    text = source_excerpts(tmp_path, max_chars=1600)

    assert isinstance(text, str)


def test_the_handler_that_reaches_out_outranks_the_health_check(tmp_path: Path) -> None:
    # Both are registered endpoints, so the decorator cannot separate them. Counting
    # the decorator's own `router.get(...)` as a call would.
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "views" / "session.py").write_text(
        '@router.get("/healthz")\ndef healthz():\n    return {"ok": True}\n\n\n'
        '@router.post("/in")\ndef sign_in(name):\n    return conn.fetchrow(f"select {name}")\n',
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text("from views import session\n", encoding="utf-8")

    text = source_excerpts(tmp_path, max_chars=1600)

    assert text.index("def sign_in") < text.index("def healthz")
