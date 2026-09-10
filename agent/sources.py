"""Which local modules the application is actually made of, without running any of it.

N3. Every one of the thirty saved SQLi runs saw `routers/` in its first `list_dir`
and not one of them ever opened it (`docs/research/night-public-analysis-2026-09-07.md`).
The vulnerable data flow was in the provided sources the whole time; the reports came
back empty because those sources never reached an observation. A tree listing names
directories, and naming a directory is not the same as telling the model that the
entry point it just read hands its requests to the files inside one.

So this module answers exactly one question -- starting from the modules nothing else
imports, which local modules are reached, and what does each of them define -- and it
answers it from the import graph rather than from a list of names anyone chose in
advance. Nothing here knows a filename, a route, a framework or a class of defect.

Reading, not importing. `ast.parse` never executes what it parses, which matters more
here than anywhere else in the agent: the tree under inspection is the untrusted
material of the task, and importing it to index it would be running it.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from agent import limits

SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "site-packages",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        "build",
        "dist",
    }
)
"""Directories the index does not descend into.

`site-packages`, `build` and `dist` are here for a reason the tree walk does not have:
a vendored dependency is a local module by every test this file applies, and an index
that leads with someone else's HTTP library has spent the brief on the one part of the
tree the task did not ship.
"""


def _python_files(root: Path, budget: int) -> list[Path]:
    found: list[Path] = []
    for current, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES)
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            found.append(Path(current) / name)
            if len(found) >= budget:
                return found
    return found


def _keys(relative: Path) -> list[str]:
    """Every dotted name this file could be imported as, longest first.

    `app/routers/auth.py` is `app.routers.auth` to something above it, `routers.auth`
    to `app/main.py`, and `auth` to its own sibling -- and which of the three appears
    depends on where the interpreter's path root was put, which is a runtime fact this
    module deliberately does not try to reconstruct. Registering all three and dropping
    any that turns out ambiguous costs one dictionary and gets every layout right.
    """
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return []
    return [".".join(parts[at:]) for at in range(len(parts))]


def _index(root: Path, files: list[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for path in files:
        for key in _keys(path.relative_to(root)):
            if key in index and index[key] != path:
                ambiguous.add(key)
            index.setdefault(key, path)
    for key in ambiguous:
        index.pop(key, None)
    return index


def _imported(tree: ast.Module, relative: Path) -> list[str]:
    """The dotted names one module imports, including `from pkg import module`.

    `from routers import auth, items` is the shape the public application uses, and
    reading only `node.module` there finds the package and misses every module in it --
    which is precisely the hop from the entry point to the handler.
    """
    package = list(relative.parent.parts)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                base = ".".join([*prefix, base] if base else prefix)
            if base:
                names.append(base)
            names.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def _defines(tree: ast.Module) -> list[str]:
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def local_sources(root: Path) -> tuple[tuple[str, str, str], ...]:
    """`(path, imported_by, what it defines)` for the local modules the tree reaches.

    Breadth-first from the modules nothing local imports, so the order is the order the
    application itself would be read in, and bounded twice over -- by how many files are
    parsed and by how many rows come back -- because this is resident context.

    A module nothing imports and that imports nothing is not reported: a standalone
    script says nothing about where the application's requests go, and the tree listing
    already named it.
    """
    if not root.is_dir():
        return ()

    files = _python_files(root, limits.SOURCE_INDEX_MAX_FILES)
    index = _index(root, files)
    parsed: dict[Path, ast.Module] = {}
    for path in files:
        try:
            parsed[path] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError, RecursionError):
            # A file we cannot parse is one row missing from the index, never a run
            # that ends: the tree is untrusted material and may hold anything.
            continue

    edges: dict[Path, list[Path]] = {}
    for path, tree in parsed.items():
        reached: list[Path] = []
        for name in _imported(tree, path.relative_to(root)):
            target = index.get(name)
            if target is not None and target != path and target not in reached:
                reached.append(target)
        edges[path] = reached

    imported_by: dict[Path, Path] = {}
    for path, reached in edges.items():
        for target in reached:
            imported_by.setdefault(target, path)

    roots = [path for path in parsed if path not in imported_by and edges.get(path)]
    queue = list(roots)
    seen = set(roots)
    rows: list[tuple[str, str, str]] = []
    while queue and len(rows) < limits.SOURCE_INDEX_MAX_ROWS:
        current = queue.pop(0)
        for target in edges.get(current, ()):
            if target in seen:
                continue
            seen.add(target)
            queue.append(target)
            names = _defines(parsed[target])[: limits.SOURCE_INDEX_MAX_NAMES]
            if not names and target.name == "__init__.py":
                # A package marker that defines nothing is a hop, not a place to read.
                continue
            rows.append(
                (
                    target.relative_to(root).as_posix(),
                    current.relative_to(root).as_posix(),
                    ", ".join(names),
                )
            )
            if len(rows) >= limits.SOURCE_INDEX_MAX_ROWS:
                break
    return tuple(rows)
