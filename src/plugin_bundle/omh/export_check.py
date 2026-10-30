"""Dangling exports: names a module the turn edited exports or imports that no longer exist.

A rename that updates a module's import line but not its ``__all__`` leaves a string naming a
function that is gone. No test that imports the new name notices, and a type checker reports
nothing by default, so the model stops with the stale entry in place. ``round_end_note`` reads each
``.py`` file the turn edited with the standard library's ``ast`` and names, as ``path:line`` rows:

- an ``__all__`` entry the module neither defines nor imports (nor, in a package
  ``__init__.py``, ships as a submodule);
- a ``from module import name`` whose module is a workspace file that no longer binds ``name``.

Always on: it reads only the edited files and the workspace modules they import from, never runs a
language server or a process, and says nothing unless such a name exists. A module whose names it
cannot know (a star import, a module-level ``__getattr__``, ``globals()`` or ``exec``, a package that
edits its own ``__path__``, an ``__all__`` that is not a literal list) is skipped, as is any file that
does not parse.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
from typing import Final

MAX_FILES: Final = 50
MAX_FILE_BYTES: Final = 512_000
MAX_NOTE_ROWS: Final = 10
_ANCESTOR_LIMIT: Final = 10
_PROJECT_MARKERS: Final = (".git", "pyproject.toml", "setup.py", "setup.cfg")
_COMPILED_SUFFIXES: Final = (".so", ".pyd", ".pyi")
# Names every module has without binding them.
_IMPLICIT_NAMES: Final = frozenset({
    "__annotations__", "__builtins__", "__cached__", "__dict__", "__doc__", "__file__", "__loader__",
    "__name__", "__package__", "__path__", "__spec__",
})
# Calls that can bind module names this reader cannot see: `globals()` / `exec` writes, `setattr`
# on a module object, and `enum.global_enum`, which copies an enum's members into its module.
_DYNAMIC_BINDERS: Final = frozenset({"exec", "global_enum", "globals", "setattr"})

_LOG = logging.getLogger("omh.export_check")


class _Unknown(Exception):
    """The module binds names this reader cannot see; nothing is said about it."""


def round_end_note(changed_paths: object) -> str:
    """``pre_verify`` attempt 0: the note naming dangling exports in this turn's edited files, or ``""``."""
    try:
        rows: list[tuple[str, int, str]] = []
        trees: dict[str, ast.Module | None] = {}
        for path in _edited_python_files(changed_paths):
            rows.extend(_file_rows(path, trees))
        return format_note(rows)
    except Exception as exc:  # noqa: BLE001 - a broken check says nothing rather than fail the turn
        _LOG.warning("export_check round_end failed=%s", type(exc).__name__)
        return ""


def format_note(rows: list[tuple[str, int, str]]) -> str:
    """A header, at most ``MAX_NOTE_ROWS`` ``path:line: finding`` rows, an overflow count, one ask."""
    if not rows:
        return ""
    ordered = sorted(set(rows))
    shown = ordered[:MAX_NOTE_ROWS]
    lines = ["OMH export check: a file you edited names something that no longer exists:"]
    lines += [f"{path}:{line}: {finding}" for path, line, finding in shown]
    if len(shown) < len(ordered):
        lines.append(f"... and {len(ordered) - len(shown)} more")
    lines.append("Update or remove each stale name.")
    return "\n".join(lines)


def _edited_python_files(changed_paths: object) -> list[str]:
    if not isinstance(changed_paths, (list, tuple)):
        return []
    files = [os.path.realpath(item) for item in changed_paths
             if isinstance(item, str) and os.path.isabs(item) and item.endswith(".py")]
    return sorted({path for path in files if os.path.isfile(path)})[:MAX_FILES]


def _file_rows(path: str, trees: dict[str, ast.Module | None]) -> list[tuple[str, int, str]]:
    tree = _tree(path, trees)
    if tree is None:
        return []
    shown = _display(path)
    rows: list[tuple[str, int, str]] = []
    try:
        bound = _bound_names(tree)
        for name, line in _all_entries(tree):
            if name not in bound and not _is_submodule(path, name):
                rows.append((shown, line, f"`__all__` lists `{name}`, which this module no longer defines or imports"))
    except _Unknown:
        pass
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            rows.extend((shown, node.lineno, finding) for finding in _missing_imports(node, path, trees))
    return rows


def _missing_imports(node: ast.ImportFrom, importer: str, trees: dict[str, ast.Module | None]) -> list[str]:
    target = _module_file(node, importer)
    if target is None:
        return []
    tree = _tree(target, trees)
    if tree is None:
        return []
    try:
        bound = _bound_names(tree)
    except _Unknown:
        return []
    source = "." * node.level + (node.module or "")
    return [f"`from {source} import {alias.name}`: {_display(target)} no longer defines `{alias.name}`"
            for alias in node.names
            if alias.name != "*" and alias.name not in bound and not _is_submodule(target, alias.name)]


def _module_file(node: ast.ImportFrom, importer: str) -> str | None:
    """The workspace ``.py`` file ``node`` imports names out of, or None when it is not one."""
    parts = node.module.split(".") if node.module else []
    if node.level:
        base = os.path.dirname(importer)
        for _ in range(node.level - 1):
            base = os.path.dirname(base)
        return _existing_module(os.path.join(base, *parts)) if parts else _existing_module(base, package_only=True)
    # An absolute import starts at the directory above the importer's package chain (`src/` for
    # `src/app/api.py`, the script's own directory for a module outside any package). A standard
    # library name is the standard library's, even where a workspace file shares it.
    if not parts or parts[0] in sys.stdlib_module_names:
        return None
    directory = os.path.dirname(importer)
    for _ in range(_ANCESTOR_LIMIT):
        if not os.path.isfile(os.path.join(directory, "__init__.py")):
            return _existing_module(os.path.join(directory, *parts))
        directory = os.path.dirname(directory)
    return None


def _existing_module(stem: str, package_only: bool = False) -> str | None:
    candidates = [os.path.join(stem, "__init__.py")] if package_only else [stem + ".py", os.path.join(stem, "__init__.py")]
    return next((os.path.realpath(item) for item in candidates if os.path.isfile(item)), None)


def _is_submodule(module: str, name: str) -> bool:
    """A package ``__init__.py`` exports a submodule by name without importing it: a ``.py`` file, a
    package directory, or a compiled module (``name.*.so`` / ``.pyd``, or its ``.pyi`` stub)."""
    if os.path.basename(module) != "__init__.py":
        return False
    package = os.path.dirname(module)
    if os.path.isfile(os.path.join(package, name + ".py")) or os.path.isfile(os.path.join(package, name, "__init__.py")):
        return True
    try:
        entries = os.listdir(package)
    except OSError:
        return False
    return any(entry.startswith(name + ".") and entry.endswith(_COMPILED_SUFFIXES) for entry in entries)


def _tree(path: str, trees: dict[str, ast.Module | None]) -> ast.Module | None:
    """``path`` parsed once per check; None when it is too large, unreadable or does not parse."""
    if path not in trees:
        trees[path] = None
        try:
            if os.path.getsize(path) <= MAX_FILE_BYTES:
                with open(path, encoding="utf-8") as handle:
                    trees[path] = ast.parse(handle.read())
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            pass
    return trees[path]


def _display(path: str) -> str:
    """``path`` relative to its project root (the nearest ancestor holding a project marker)."""
    directory = os.path.dirname(path)
    for _ in range(_ANCESTOR_LIMIT * 3):
        if any(os.path.exists(os.path.join(directory, marker)) for marker in _PROJECT_MARKERS):
            return os.path.relpath(path, directory).replace(os.sep, "/")
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return path


def _all_entries(tree: ast.Module) -> list[tuple[str, int]]:
    """Every string ``__all__`` is given at module level, with its line; ``_Unknown`` when one is not a literal."""
    entries: list[tuple[str, int]] = []
    for node in _module_statements(tree.body):
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(_is_all(target) for target in node.targets):
            value = node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and _is_all(node.target):
            value = node.value
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Attribute) and _is_all(node.value.func.value)):
            if node.value.func.attr == "append" and len(node.value.args) == 1:
                value = ast.List(elts=list(node.value.args))
            elif node.value.func.attr == "extend" and len(node.value.args) == 1:
                value = node.value.args[0]
            else:
                raise _Unknown
        else:
            continue
        if not isinstance(value, (ast.List, ast.Tuple)):
            raise _Unknown
        for item in value.elts:
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                raise _Unknown
            entries.append((item.value, item.lineno))
    return entries


def _is_all(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def _module_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Module-level statements, including those inside ``if`` / ``try`` / ``with`` / loop / ``match`` blocks."""
    found: list[ast.stmt] = []
    for node in body:
        found.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(node, field, None)
            if isinstance(nested, list):
                found.extend(_module_statements(nested))
        for handler in getattr(node, "handlers", None) or ():
            found.extend(_module_statements(handler.body))
        for case in getattr(node, "cases", None) or ():
            found.extend(_module_statements(case.body))
    return found


def _bound_names(tree: ast.Module) -> set[str]:
    """Every name the module binds at module level; ``_Unknown`` when it can bind names unseen."""
    names: set[str] = set(_IMPLICIT_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, (ast.Name, ast.Attribute)) and _dotted_leaf(node) in _DYNAMIC_BINDERS:
            raise _Unknown
        elif isinstance(node, ast.Name) and node.id == "__path__" and not isinstance(node.ctx, ast.Load):
            # A package that edits its own search path has submodules this reader cannot list.
            raise _Unknown
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "__path__":
            raise _Unknown
    for node in _module_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                raise _Unknown
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            names.update(handler.name for handler in node.handlers if handler.name)
        elif type(node).__name__ == "TypeAlias":
            names.update(_target_names(node.name))  # type: ignore[attr-defined]
        for target in _statement_targets(node):
            names.update(_target_names(target))
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr):
                names.update(_target_names(child.target))
            elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                names.add(child.rest)
    if "__getattr__" in names:
        # PEP 562: the module answers any name it is asked for.
        raise _Unknown
    return names


def _dotted_leaf(node: ast.expr) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _statement_targets(node: ast.stmt) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.optional_vars for item in node.items if item.optional_vars is not None]
    return []


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _target_names(item)}
    return set()


__all__ = ["MAX_FILES", "MAX_NOTE_ROWS", "format_note", "round_end_note"]
