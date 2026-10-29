"""Cross-file impact check: new language-server errors in the files that import what a turn edited.

Hermes checks diagnostics on the file an edit wrote and nothing else, so a changed
signature, return shape or keyword leaves its callers broken without a word. Opt in
with env ``OMH_IMPACT_CHECK`` or the profile setting
``plugins.entries.omh.settings.impact_check``; off, every entry point returns at once.

1. ``baseline_before_edit`` (``pre_tool_call``): on a turn's first ``patch`` /
   ``write_file`` of an existing ``.py`` file, find up to ``MAX_IMPORTERS`` workspace
   files that import it (stdlib ``ast`` scan) and record their current errors.
2. ``round_end_note`` (``pre_verify``, attempt 0): re-check those importers against the
   disk and return a short ``path:line: message`` note when an error appeared that the
   baseline did not have; ``""`` when nothing is new or nothing can be said.

Errors are compared as (code, message) counts per file, not by position, so an error
that only moved when the model edited that importer is not reported as new. A file the
server gave no verdict for is skipped, never reported clean or broken. State is
per-process and per session, reset when a new turn id reaches ``pre_tool_call``.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import threading
import time
from collections import Counter
from typing import Any, Final

from . import lsp_bridge, lsp_nav, runtime_paths
from .lsp_bridge import BridgeError
from .lsp_nav import LspNavError
from .tools.lsp_nav_tool import _lsp_tools_enabled, _workspace_root

IMPACT_ENV: Final = "OMH_IMPACT_CHECK"
IMPACT_SETTING: Final = "impact_check"
MAX_IMPORTERS: Final = 30
MAX_SCAN_FILES: Final = 2000
MAX_SCAN_BYTES: Final = 512_000
MAX_NOTE_LINES: Final = 15
BASELINE_TIMEOUT: Final = 20.0
ROUND_END_TIMEOUT: Final = 12.0
_MAX_SESSIONS: Final = 64
_MESSAGE_CHARS: Final = 200
_EDIT_TOOLS: Final = frozenset({"patch", "write_file"})
_V4A_HEADER: Final = re.compile(r"^\*\*\*\s*(?:Update|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_ON: Final = ("1", "true", "on", "yes")
_SEVERITY_ERROR: Final = 1

_LOG = logging.getLogger("omh.impact_check")
_lock = threading.Lock()
# session -> {"turn": str, "root": str, "edited": set[str], "baseline": {importer: Counter | None}}
_sessions: dict[str, dict[str, Any]] = {}


def enabled() -> bool:
    """True when env ``OMH_IMPACT_CHECK`` or the profile setting ``impact_check`` is on; read failures are off."""
    if os.environ.get(IMPACT_ENV, "").strip().lower() in _ON:
        return True
    try:
        text = (runtime_paths.default_hermes_home() / "config.yaml").read_text(encoding="utf-8")
        value = runtime_paths.plugin_setting_text(text, IMPACT_SETTING)
    except Exception:  # noqa: BLE001 - fail closed: an unreadable config leaves the check off
        return False
    return value is not None and value.strip().lower() in _ON


def edit_targets(tool_name: object, args: object) -> list[str]:
    """The ``.py`` paths a ``patch`` / ``write_file`` call names (``path``, or V4A ``*** Update File:`` headers)."""
    if tool_name not in _EDIT_TOOLS or not isinstance(args, dict):
        return []
    paths: list[str] = []
    if tool_name == "patch" and args.get("mode") == "patch":
        body = args.get("patch")
        if isinstance(body, str):
            paths = [match.group(1).strip() for match in _V4A_HEADER.finditer(body)]
    elif isinstance(args.get("path"), str):
        paths = [args["path"]]
    return [path for path in paths if path.endswith(".py")]


def module_names(path: str, root: str) -> set[str]:
    """Every dotted name ``path`` could be imported as from ``root`` or one of its sub-directories."""
    parts = os.path.relpath(path, root).replace(os.sep, "/")[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[index:]) for index in range(len(parts)) if parts[index:]}


def _relative_base(importer: str, node: ast.ImportFrom) -> str:
    base = os.path.dirname(importer)
    for _ in range(node.level - 1):
        base = os.path.dirname(base)
    return os.path.join(base, *node.module.split(".")) if node.module else base


def _is_module(path: str, target: str) -> bool:
    return target in (path + ".py", os.path.join(path, "__init__.py"))


def _imports_from(node: ast.ImportFrom, importer: str, target: str, names: set[str]) -> str:
    """``"module"`` when ``node`` imports names out of ``target``, ``"submodule"`` when it imports
    ``target`` itself as a member of its package, ``""`` otherwise."""
    if node.level:
        base = _relative_base(importer, node)
        if _is_module(base, target):
            return "module"
        return "submodule" if any(_is_module(os.path.join(base, alias.name), target) for alias in node.names) else ""
    if node.module in names:
        return "module"
    return "submodule" if any(f"{node.module}.{alias.name}" in names for alias in node.names) else ""


def imports_module(tree: ast.AST, importer: str, target: str, names: set[str], only: set[str] | None = None) -> bool:
    """Whether ``tree`` (the file ``importer``) imports ``target`` (known as ``names``).

    ``only`` narrows a package ``__init__`` target to the names it re-exports: ``from pkg import x``
    counts only for such an ``x``, so importing an unrelated member of the package does not.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in names for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            kind = _imports_from(node, importer, target, names)
            if kind == "submodule" and only is None:
                return True
            if kind == "module" and (only is None or any(alias.name in only for alias in node.names)):
                return True
    return False


def _reexports(tree: ast.AST, init: str, target: str, names: set[str]) -> set[str]:
    """The names the package ``init`` binds from ``target`` (``from .pricing import fee as f`` -> ``f``)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _imports_from(node, init, target, names):
            bound.update(alias.asname or alias.name for alias in node.names)
    return bound


def find_importers(root: str, target: str, cap: int = MAX_IMPORTERS) -> list[str]:
    """Up to ``cap`` workspace ``.py`` files importing ``target``, plus importers of a package
    ``__init__.py`` that re-exports it (one level). Sorted walk, bounded by ``MAX_SCAN_FILES``."""
    files = lsp_nav.workspace_files(root, ".py", target, MAX_SCAN_FILES)[1:]
    texts: dict[str, str | None] = {}
    trees: dict[str, ast.AST | None] = {}
    found: list[str] = []
    queue: list[tuple[str, set[str] | None]] = [(target, None)]
    seen = {target}
    while queue and len(found) < cap:
        current, only = queue.pop(0)
        names = module_names(current, root)
        leaves = {name.rsplit(".", 1)[-1] for name in names}
        for path in files:
            if path in seen or len(found) >= cap:
                continue
            if path not in texts:
                texts[path] = _read(path)
            text = texts[path]
            # Cheap pre-filter: a relative `from . import x` need not spell the module's name.
            if text is None or "import" not in text or not (any(leaf in text for leaf in leaves) or "from ." in text):
                continue
            if path not in trees:
                trees[path] = _parse(text)
            tree = trees[path]
            if tree is not None and imports_module(tree, path, current, names, only):
                seen.add(path)
                found.append(path)
                if os.path.basename(path) == "__init__.py" and only is None:
                    exported = _reexports(tree, path, current, names)
                    if exported:
                        queue.append((path, exported))
    return found


def _read(path: str) -> str | None:
    try:
        if os.path.getsize(path) > MAX_SCAN_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _parse(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _errors(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in diagnostics if isinstance(item, dict) and (item.get("severity") or 1) == _SEVERITY_ERROR]


def _key(item: dict[str, Any]) -> tuple[str, str]:
    code = item.get("code")
    return ("" if code is None else str(code), str(item.get("message") or "").strip())


def _session_state(session_id: str, turn_id: str, root: str) -> dict[str, Any]:
    with _lock:
        state = _sessions.get(session_id)
        if state is None or state["turn"] != turn_id or state["root"] != root:
            state = {"turn": turn_id, "root": root, "edited": set(), "baseline": {}}
            _sessions.pop(session_id, None)
            _sessions[session_id] = state
            while len(_sessions) > _MAX_SESSIONS:
                del _sessions[next(iter(_sessions))]
        return state


def baseline_before_edit(kwargs: dict[str, Any], tool_input: object) -> None:
    """``pre_tool_call``: record the importers' errors before the turn's first edit of each ``.py`` file."""
    tool_name = kwargs.get("tool_name")
    if tool_name not in _EDIT_TOOLS:
        return
    targets = edit_targets(tool_name, tool_input)
    if not targets or not enabled():
        return
    session_id = str(kwargs.get("session_id", "") or kwargs.get("task_id", "") or "")
    if not session_id:
        return
    started = time.monotonic()
    try:
        root = os.path.realpath(_workspace_root(kwargs.get("task_id")) or os.getcwd())
        state = _session_state(session_id, str(kwargs.get("turn_id", "") or ""), root)
        pending: list[str] = []
        sync: list[str] = []
        for requested in targets:
            try:
                path = lsp_nav.resolve_path(requested, root)
            except LspNavError:
                continue
            if path in state["edited"] or not os.path.isfile(path):
                continue
            state["edited"].add(path)
            if not lsp_bridge.in_git_worktree(path):
                continue
            sync.append(path)
            room = MAX_IMPORTERS - len(state["baseline"]) - len(pending)
            pending.extend(item for item in find_importers(root, path, max(room, 0))
                           if item not in state["baseline"] and item not in pending)
        if not pending:
            return
        current = lsp_bridge.diagnostics(pending, sync=sync, timeout=BASELINE_TIMEOUT)
        for path in pending:
            found = current.get(path)
            state["baseline"][path] = None if found is None else Counter(_key(item) for item in _errors(found))
        _LOG.info("impact_check baseline session=%s importers=%d unknown=%d ms=%d", session_id, len(pending),
                  sum(1 for path in pending if current.get(path) is None), (time.monotonic() - started) * 1000)
    except BridgeError as exc:
        _LOG.info("impact_check baseline session=%s skipped=%s", session_id, exc.code)
    except Exception as exc:  # noqa: BLE001 - never fail the edit it is watching
        _LOG.warning("impact_check baseline session=%s failed=%s", session_id, type(exc).__name__)


def round_end_note(session_id: str, changed_paths: object) -> str:
    """``pre_verify`` attempt 0: the note naming new importer errors, or ``""``. Consumes the session's baseline."""
    with _lock:
        state = _sessions.pop(session_id, None) if session_id else None
    if not state:
        return ""
    known = {path: counts for path, counts in state["baseline"].items() if counts is not None and os.path.isfile(path)}
    if not known or not enabled():
        return ""
    started = time.monotonic()
    root = state["root"]
    sync = []
    for item in changed_paths if isinstance(changed_paths, (list, tuple)) else ():
        if isinstance(item, str) and item.endswith(".py"):
            sync.append(os.path.realpath(item if os.path.isabs(item) else os.path.join(root, item)))
    try:
        current = lsp_bridge.diagnostics(list(known), sync=sync, timeout=ROUND_END_TIMEOUT)
    except BridgeError as exc:
        _LOG.info("impact_check round_end session=%s skipped=%s", session_id, exc.code)
        return ""
    except Exception as exc:  # noqa: BLE001 - a broken check says nothing rather than fail the turn
        _LOG.warning("impact_check round_end session=%s failed=%s", session_id, type(exc).__name__)
        return ""
    new: list[tuple[str, int, str]] = []
    for path, baseline in known.items():
        found = current.get(path)
        if found is None:
            continue
        remaining = Counter(baseline)
        ordered = sorted(_errors(found), key=lambda item: ((item.get("range") or {}).get("start") or {}).get("line", 0))
        for item in ordered:
            key = _key(item)
            if remaining[key] > 0:
                remaining[key] -= 1
                continue
            line = int(((item.get("range") or {}).get("start") or {}).get("line", 0)) + 1
            new.append((os.path.relpath(path, root), line, key[1].splitlines()[0][:_MESSAGE_CHARS] if key[1] else "error"))
    _LOG.info("impact_check round_end session=%s importers=%d new_errors=%d files=%d ms=%d", session_id, len(known),
              len(new), len({row[0] for row in new}), (time.monotonic() - started) * 1000)
    return format_note(new, recheck_tool=_lsp_tools_enabled())


def format_note(new: list[tuple[str, int, str]], recheck_tool: bool) -> str:
    """At most ``MAX_NOTE_LINES`` lines: a header, ``path:line: message`` rows, an overflow count, a closing ask."""
    if not new:
        return ""
    rows = sorted(new)
    room = MAX_NOTE_LINES - 2
    shown = rows if len(rows) <= room else rows[: room - 1]
    lines = ["OMH impact check: your edits left new errors in files that import the code you changed "
             "(language server, checked against the disk now):"]
    lines += [f"{path}:{line}: {message}" for path, line, message in shown]
    if len(shown) < len(rows):
        lines.append(f"... and {len(rows) - len(shown)} more")
    then = "re-check them with `lsp_diagnostics`" if recheck_tool else "re-run the checks that cover them"
    lines.append(f"Update these callers to match your change, or confirm the break is intended; then {then}.")
    return "\n".join(lines)


__all__ = [
    "IMPACT_ENV",
    "IMPACT_SETTING",
    "MAX_IMPORTERS",
    "MAX_NOTE_LINES",
    "baseline_before_edit",
    "edit_targets",
    "enabled",
    "find_importers",
    "format_note",
    "imports_module",
    "module_names",
    "round_end_note",
]
