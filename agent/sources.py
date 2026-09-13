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
            path = Path(current) / name
            try:
                if path.stat().st_size > limits.SOURCE_READ_MAX_BYTES:
                    # A file larger than we would ever read into the brief is generated
                    # data, not the application's structure. Parsing it cost seconds and
                    # hundreds of MB of RSS -- an OOM in a small microVM -- and named no
                    # handler. Skipping it here keeps it out of the index, the parse and
                    # the excerpts in one pass.
                    continue
            except OSError:
                continue
            found.append(path)
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
            tree = parsed.get(target)
            if tree is None:
                # An imported module that did not parse is in the import index but not
                # among the parsed trees. It is a row we cannot describe and has no
                # outgoing edges to follow, so it is skipped rather than looked up --
                # a bare `parsed[target]` here raised KeyError and killed the run
                # before the first LLM call whenever a fix task's defect was a
                # SyntaxError or the tree held a Python-2 sample.
                continue
            queue.append(target)
            names = _defines(tree)[: limits.SOURCE_INDEX_MAX_NAMES]
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


EXCERPT_HEADER = "source excerpts (from the modules above; read the files for the rest):"
EXCERPT_CUT = "  … cut here; read the file for the rest"


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-level functions and one level of methods. Deeper nesting is a helper."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(node)
        elif isinstance(node, ast.ClassDef):
            found.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return found


def _call_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    """Lines where this function's own body calls something through an attribute.

    `conn.fetchrow(...)`, `cursor.execute(...)`, `subprocess.run(...)`: the shape of
    work handed to something outside the function, whatever that something is. It says
    nothing about whether the call is safe -- that judgement is the task's, not ours.

    The body only. `@router.get("/healthz")` is an attribute call too, so counting the
    decorator would give every registered endpoint this mark and rank a three-line
    health check level with the handler that reaches a database.
    """
    return sorted(
        inner.lineno
        for statement in node.body
        for inner in ast.walk(statement)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
    )


def _source_text(path: Path, root: Path) -> str | None:
    """The file's text, or None if it is not ours to read.

    A symlink is refused rather than followed: the workspace is untrusted material, and
    a link pointing out of it would put a file the task never shipped into the prompt.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        if path.stat().st_size > limits.SOURCE_READ_MAX_BYTES:
            return None
        blob = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in blob[: limits.BINARY_SNIFF_BYTES]:
        return None
    return blob.decode("utf-8", errors="replace")


def source_excerpts(root: Path, *, max_chars: int) -> str:
    """The bodies behind the index, attributed and bounded.

    P2a measured what the index alone buys: the agent read a file for the first time,
    read the entry point four times, and never opened the handler the index had named
    for it. A name is one more action away from the evidence, and an action the model
    can skip; the text is the evidence.

    Ranked by two facts that hold in any framework -- the function is decorated, so
    something registered it as an entry, and it calls out through an attribute, so the
    work leaves it -- never by what the call looks like. Selecting only the calls that
    look dangerous would put the audit's answer in the brief and would hide the safe
    handler on which a report of no findings has to rest.
    """
    if not root.is_dir() or max_chars <= 0:
        return ""

    rows = local_sources(root)
    order = [reached for reached, _, _ in rows]
    order += [importer for _, importer, _ in rows if importer not in order]

    candidates: list[tuple[int, int, str, int, int, list[str], list[int]]] = []
    for rank, relative in enumerate(order):
        text = _source_text(root / relative, root)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            continue
        lines = text.splitlines()
        for node in _functions(tree):
            calls = _call_lines(node)
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            end = node.end_lineno or node.lineno
            score = (2 if node.decorator_list else 0) + (1 if calls else 0)
            candidates.append((-score, rank, relative, start, end, lines, calls))

    candidates.sort(key=lambda item: (item[0], item[1], item[3]))

    parts = [EXCERPT_HEADER]
    used = len(EXCERPT_HEADER)
    for _, _, relative, start, end, lines, calls in candidates:
        body = "\n".join(lines[start - 1 : end])
        block = f"{relative}:{start}-{end}\n{body}"
        if used + 1 + len(block) <= max_chars:
            parts.append(block)
            used += 1 + len(block)
            continue
        # Too big for what is left: show a window around the call the function makes,
        # say where it was cut, and stop. A silently trimmed body would read as the
        # whole function and could hide the line that answers the task.
        room = max_chars - used - len(EXCERPT_CUT) - 2
        anchor = calls[0] if calls else start
        head = f"{relative}:{anchor}-{end}\n"
        if room > len(head) + 40:
            window: list[str] = []
            for line in lines[anchor - 1 : end]:
                if len(head) + sum(len(item) + 1 for item in [*window, line]) > room:
                    break
                window.append(line)
            if window:
                parts.append(head + "\n".join(window))
        parts.append(EXCERPT_CUT)
        break

    return "\n".join(parts) if len(parts) > 1 else ""
