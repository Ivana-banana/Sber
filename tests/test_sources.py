"""The local import index: what the entry point hands its work to.

N3. Thirty saved SQLi runs saw the handlers directory in their first listing and none
of them opened it, so these tests are about one thing -- does the brief name the file
the request actually reaches -- and about the two ways that could be cheating: knowing
a filename in advance, or learning it by running the tree under inspection.
"""

from __future__ import annotations

from pathlib import Path

from agent import limits
from agent.sources import local_sources


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
