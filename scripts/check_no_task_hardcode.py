#!/usr/bin/env python3
"""Fail when agent code mentions a public task by name, path, deliverable or answer.

CLAUDE.md forbids hardcoding the public tasks. Docstrings and comments are allowed to
mention them (they document experiments); code strings and identifiers are not. The
check walks the AST of every module under agent/ and inspects string constants that
are not docstrings.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNALS = re.compile(
    r"incident-log-forensics|fix-sqli-(login|search)|find-sqli-login|hello-file|bye-file"
    r"|security_report\.json|collector_note|insecure-api-app",
    re.IGNORECASE,
)


def docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def main() -> int:
    hits: list[str] = []
    for path in sorted((ROOT / "agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docs = docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if SIGNALS.search(node.value):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:80]!r}")
    if hits:
        print("\n".join(hits))
        print(f"task hardcode found: {len(hits)}")
        return 1
    print("no task hardcode in agent code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
