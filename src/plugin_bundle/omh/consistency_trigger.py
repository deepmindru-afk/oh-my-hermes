"""Whether the round-end consistency check can pay off for the Python a turn edited.

The check (`hooks/verify_hooks.ROUND_END_CHECKLIST`) costs one more round of the turn, and it pays
off only when something outside the edited files has to change with the edit. This module decides,
deterministically and from the disk alone, whether such a thing exists:

1. ``snapshot_before_edit`` (``pre_tool_call``): before the turn's first ``patch`` / ``write_file`` of
   each ``.py`` / ``.pyi`` file, keep the file's text (``""`` for a file that does not exist yet).
2. ``round_end_trigger`` (``pre_verify``, attempt 0): diff each snapshot against the disk, find the
   definitions the changed lines touch (stdlib ``ast``), and scan the workspace's other Python files,
   the ones this turn did not edit, for one of four signals:

   - ``copy``: a touched top-level definition is also defined at the top level of another file;
   - ``renamed``: a top-level or class-level definition the edit removed is still named elsewhere;
   - ``signature``: a touched function's parameters or return annotation changed, and it is named
     elsewhere;
   - ``line_copy``: a non-trivial line the edit replaced or removed also appears elsewhere.

   No signal means the check is skipped. Anything this module cannot decide fires the check, as it
   fired before: a changed source file that is not Python, a changed file with no snapshot or that does
   not parse, a scan over its file count or time budget, or any error.

State is per process and per session, reset when a new turn id reaches ``pre_tool_call`` and consumed
at the turn's first stop. Nothing here raises into a hook.
"""

from __future__ import annotations

import ast
import difflib
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Final, NamedTuple

from . import lsp_nav
from .lsp_nav import LspNavError
from .tools.lsp_nav_tool import _workspace_root

PYTHON_SUFFIXES: Final = (".py", ".pyi")
MAX_SNAPSHOT_FILES: Final = 32
MAX_FILE_BYTES: Final = 512_000
MAX_SCAN_FILES: Final = 3000
SCAN_SECONDS: Final = 2.0
# A replaced line shorter than this (after collapsing whitespace) is too common to call a copy.
MIN_COPY_LINE_CHARS: Final = 30
_MAX_SESSIONS: Final = 64
_EDIT_TOOLS: Final = frozenset({"patch", "write_file"})
_V4A_HEADER: Final = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_V4A_MOVE: Final = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)
# Top-level names many unrelated modules define; one of them defined elsewhere is not a copy.
_COMMON_NAMES: Final = frozenset({"main", "log", "LOG", "logger", "LOGGER", "app"})

_LOG = logging.getLogger("omh.consistency_trigger")
_lock = threading.Lock()
# session -> {"turn": str, "root": str, "before": {realpath: str | None}}; None = too large to keep.
_sessions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


class Verdict(NamedTuple):
    fire: bool
    reason: str
    detail: str = ""


def snapshot_before_edit(kwargs: dict[str, Any], tool_input: object) -> None:
    """``pre_tool_call``: keep the pre-edit text of each Python file this call edits, once per turn."""
    try:
        tool_name = kwargs.get("tool_name")
        if tool_name not in _EDIT_TOOLS or not isinstance(tool_input, dict):
            return
        session_id = str(kwargs.get("session_id", "") or kwargs.get("task_id", "") or "")
        targets = [path for path in _edit_targets(str(tool_name), tool_input) if path.endswith(PYTHON_SUFFIXES)]
        if not session_id or not targets:
            return
        root = os.path.realpath(_workspace_root(kwargs.get("task_id")) or os.getcwd())
        turn = str(kwargs.get("turn_id", "") or "")
        with _lock:
            state = _sessions.get(session_id)
            if state is None or state["turn"] != turn:
                state = {"turn": turn, "root": root, "before": {}}
                _sessions.pop(session_id, None)
                _sessions[session_id] = state
                while len(_sessions) > _MAX_SESSIONS:
                    _sessions.popitem(last=False)
        for requested in targets:
            path = _resolve(requested, root)
            if path is None:
                continue
            with _lock:
                if path in state["before"] or len(state["before"]) >= MAX_SNAPSHOT_FILES:
                    continue
            text = _read(path, missing="")
            with _lock:
                state["before"].setdefault(path, text)
    except Exception as exc:  # noqa: BLE001 - never fail the edit; a missing snapshot fires the check
        _LOG.warning("consistency_trigger snapshot failed=%s", type(exc).__name__)


def round_end_trigger(session_id: str, code_paths: tuple[str, ...]) -> Verdict:
    """``pre_verify`` attempt 0: whether the consistency check should fire for this turn's changed source
    files (``code_paths``). Consumes the session's snapshots."""
    with _lock:
        state = _sessions.pop(session_id, None) if session_id else None
    started = time.monotonic()
    try:
        verdict = _decide(state, code_paths, started + SCAN_SECONDS)
    except Exception as exc:  # noqa: BLE001 - an undecidable turn fires the check, as before this module
        verdict = Verdict(True, "error", type(exc).__name__)
    _LOG.info("consistency_trigger session=%s fire=%s reason=%s detail=%s ms=%d", session_id, verdict.fire,
              verdict.reason, verdict.detail, (time.monotonic() - started) * 1000)
    return verdict


def discard_snapshots(session_id: object) -> None:
    """Drop the session's snapshots without reading them (the turn's first stop did not need a verdict)."""
    if isinstance(session_id, str) and session_id:
        with _lock:
            _sessions.pop(session_id, None)


def reset() -> None:
    """Forget every snapshot (tests)."""
    with _lock:
        _sessions.clear()


def _decide(state: dict[str, Any] | None, code_paths: tuple[str, ...], deadline: float) -> Verdict:
    if state is None:
        return Verdict(True, "no_snapshot")
    root = state["root"]
    edited: set[str] = set()
    for item in code_paths:
        path = os.path.realpath(item if os.path.isabs(item) else os.path.join(root, item))
        if not path.endswith(PYTHON_SUFFIXES):
            return Verdict(True, "not_python", os.path.basename(path))
        edited.add(path)
    needles = _Needles()
    for path in sorted(edited):
        before = state["before"].get(path, False)
        if before is False or before is None:
            return Verdict(True, "no_snapshot", os.path.basename(path))
        after = _read(path, missing="")
        if after is None:
            return Verdict(True, "too_large", os.path.basename(path))
        if not _collect(before, after, needles):
            return Verdict(True, "parse_error", os.path.basename(path))
    if needles.empty():
        return Verdict(False, "nothing_changed")
    return _scan(root, edited, needles, deadline)


class _Needles:
    def __init__(self) -> None:
        self.defined: set[str] = set()   # copy: defined at the top level elsewhere
        self.renamed: set[str] = set()   # renamed: named elsewhere
        self.signature: set[str] = set()  # signature: named elsewhere
        self.lines: set[str] = set()     # line_copy: appears as a line elsewhere

    def empty(self) -> bool:
        return not (self.defined or self.renamed or self.signature or self.lines)


def _collect(before: str, after: str, needles: _Needles) -> bool:
    """Add what this file's edit could have a copy or a reference of elsewhere; False when either side does not parse."""
    before_tree, after_tree = _parse(before), _parse(after)
    if before_tree is None or after_tree is None:
        return False
    old_lines, new_lines = before.splitlines(), after.splitlines()
    touched_lines: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:  # a pure insertion touches the lines on both sides of it
            touched_lines.update({i1, i1 + 1})
            continue
        touched_lines.update(range(i1 + 1, i2 + 1))
        for line in old_lines[i1:i2]:
            normalized = " ".join(line.split())
            if len(normalized) >= MIN_COPY_LINE_CHARS and not normalized.startswith(("import ", "from ")):
                needles.lines.add(normalized)
    old_defs, new_defs = _definitions(before_tree), _definitions(after_tree)
    for qualified, (start, end, node) in old_defs.items():
        name = qualified.rsplit(".", 1)[-1]
        if _dunder(name):
            continue
        if qualified not in new_defs:
            needles.renamed.add(name)
            continue
        if not touched_lines.intersection(range(start, end + 1)):
            continue
        top = qualified.split(".", 1)[0]
        if not _dunder(top) and top not in _COMMON_NAMES and not top.startswith("test"):
            needles.defined.add(top)
        new_node = new_defs[qualified][2]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _signature(node) != _signature(new_node):
            needles.signature.add(name)
    return True


def _scan(root: str, edited: set[str], needles: _Needles, deadline: float) -> Verdict:
    defined = {name: re.compile(rf"^(?:async[ \t]+def|def|class)[ \t]+{re.escape(name)}\b|^{re.escape(name)}[ \t]*(?::[^=\n]*)?=(?!=)",
                                re.MULTILINE) for name in sorted(needles.defined)}
    named = {name: re.compile(rf"\b{re.escape(name)}\b") for name in sorted(needles.renamed | needles.signature)}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in lsp_nav._SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if not filename.endswith(PYTHON_SUFFIXES):
                continue
            path = os.path.realpath(os.path.join(dirpath, filename))
            if path in edited:
                continue
            scanned += 1
            if scanned > MAX_SCAN_FILES or time.monotonic() >= deadline:
                return Verdict(True, "scan_budget", str(scanned))
            text = _read(path)
            if not text:
                continue
            where = os.path.relpath(path, root)
            for name, pattern in defined.items():
                if name in text and pattern.search(text):
                    return Verdict(True, "copy", f"{name} in {where}")
            for name, pattern in named.items():
                if name in text and pattern.search(text):
                    reason = "renamed" if name in needles.renamed else "signature"
                    return Verdict(True, reason, f"{name} in {where}")
            if needles.lines:
                lines = {" ".join(line.split()) for line in text.splitlines()}
                hit = next((line for line in sorted(needles.lines) if line in lines), None)
                if hit is not None:
                    return Verdict(True, "line_copy", where)
    return Verdict(False, "no_copy_or_reference", str(scanned))


def _definitions(tree: ast.AST) -> dict[str, tuple[int, int, ast.AST]]:
    """Module- and class-level functions, classes and assigned names: qualified name -> (first line, last line, node)."""
    found: dict[str, tuple[int, int, ast.AST]] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            start = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])])
            end = getattr(node, "end_lineno", None) or node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.setdefault(prefix + node.name, (start, end, node))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        found.setdefault(prefix + target.id, (start, end, node))

    visit(getattr(tree, "body", []), "")
    return found


def _signature(node: ast.AST) -> str:
    args = getattr(node, "args", None)
    returns = getattr(node, "returns", None)
    return f"{type(node).__name__}|{ast.dump(args) if args else ''}|{ast.dump(returns) if returns else ''}"


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _edit_targets(tool_name: str, args: dict[str, Any]) -> list[str]:
    if tool_name == "patch" and args.get("mode") == "patch":
        body = args.get("patch")
        if not isinstance(body, str):
            return []
        paths = [match.group(1).strip() for match in _V4A_HEADER.finditer(body)]
        for match in _V4A_MOVE.finditer(body):
            paths += [match.group(1).strip(), match.group(2).strip()]
        return paths
    path = args.get("path")
    return [path] if isinstance(path, str) and path else []


def _resolve(requested: str, root: str) -> str | None:
    try:
        return lsp_nav.resolve_path(requested, root)
    except LspNavError:
        return None


def _read(path: str, missing: str | None = None) -> str | None:
    """The file's text; ``missing`` when it does not exist; None when it is too large or unreadable."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError:
        return missing
    except OSError:
        return None


def _parse(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


__all__ = ["Verdict", "discard_snapshots", "reset", "round_end_trigger", "snapshot_before_edit"]
