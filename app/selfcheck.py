"""Is the code on this box actually one build?

WHY THIS EXISTS.

Deploys here are a zip of the files that changed, copied over the tree. That is
fine right up to the moment one of them is missed - and then the box is running
half of one build and half of another, which is a state no test can catch,
because every test runs against a tree where all the files are present.

It happened. `app/main.py` came from build 114 and `app/delivery.py` from
before 111, so the board raised

    ImportError: cannot import name 'out_of_sync_why' from 'app.delivery'

on every load. The error was accurate and it took a screenshot and a round trip
to see it, and it would have been a blank white "Internal Server Error" a day
earlier.

The check is the one the failure suggests: read every `from .x import a, b` in
this package and confirm `x` really has `a` and `b`. Most of them are inside
functions, so nothing runs them until somebody opens the page they are on -
which is exactly why a missed file can sit there looking fine.

It runs once at startup, and what it finds goes at the top of every page.
"""
from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path

log = logging.getLogger("report-qa")

_HERE = Path(__file__).resolve().parent
_PKG = __package__ or "app"

# Names that are not module attributes at import time and never will be.
_SKIP = {"annotations"}


def _module_of(path: Path) -> str:
    rel = path.relative_to(_HERE).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([_PKG, *parts]) if parts else _PKG


def _target(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    """Resolve `from ..x import y` the way Python does.

    One dot means the module's own PACKAGE, not the module - which is the
    thing to get right here, or every relative import in the app looks broken
    and the check cries wolf on a tree that is perfectly fine.
    """
    parts = mod.split(".")
    if not is_pkg:
        parts = parts[:-1]
    up = node.level - 1
    if up:
        parts = parts[:len(parts) - up] or parts[:1]
    return ".".join(parts + ([node.module] if node.module else []))


def stale_imports() -> list[str]:
    """Every relative import in this package that would fail if it ran.

    Returns one plain sentence per problem, naming the file that would break
    and the file that is out of date - which is the file to go and copy.
    """
    out: list[str] = []
    for path in sorted(_HERE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        mod = _module_of(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:            # unreadable is worse
            out.append(f"{path.name} could not be read: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            target = _target(mod, path.name == "__init__.py", node)
            try:
                obj = importlib.import_module(target)
            except Exception as exc:                     # noqa: BLE001
                out.append(f"{path.name} line {node.lineno} imports from "
                           f"{target}, which will not import: {exc}")
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name in _SKIP:
                    continue
                if not hasattr(obj, alias.name):
                    # `from . import brand` is a MODULE, not an attribute, and
                    # it is not an attribute of its package until something
                    # imports it. Ask for it as a module before calling it
                    # missing.
                    try:
                        importlib.import_module(f"{target}.{alias.name}")
                        continue
                    except Exception:                    # noqa: BLE001
                        pass
                    out.append(
                        f"{path.name} line {node.lineno} wants "
                        f"{alias.name!r} from {target}, which does not have "
                        f"it - {target.split('.')[-1]}.py on this box is from "
                        f"an older build than {path.name}")
    return out


_RESULT: list[str] | None = None


def check(force: bool = False) -> list[str]:
    """Cached: the files do not change while the process is running."""
    global _RESULT
    if _RESULT is None or force:
        _RESULT = stale_imports()
        for line in _RESULT:
            log.error("HALF-DEPLOYED: %s", line)
    return _RESULT
