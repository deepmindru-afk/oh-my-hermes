from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Final, TypeGuard

from .. import runtime_paths
from ..catch_log import PLAN_CONTINUED, record_catch
from ..host_observation import observe_plugin_hook_call
from ..todo_reconciliation import plan_continuation_parts
from .nudge_budget import plan_nudge_allowed

_UI_SUFFIXES: Final = frozenset({".css", ".html", ".jsx", ".less", ".sass", ".scss", ".svelte", ".tsx", ".vue"})
_DEPENDENCY_FILES: Final = frozenset(
    {
        "bun.lock",
        "cargo.lock",
        "cargo.toml",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "uv.lock",
        "yarn.lock",
    }
)
_OBSERVATION_KEYS: Final = ("host", "source", "evidence_ref", "evidence_refs", "omh_home", "hermes_home")
# Source files whose edit earns the round-end checklist. Deliberately partial:
# common source languages only. Docs, data, lock files and anything unlisted get
# no checklist, which is the cheap direction to be wrong in.
_CODE_SUFFIXES: Final = frozenset(
    {
        ".bash", ".c", ".cc", ".cjs", ".clj", ".cpp", ".cs", ".cxx", ".dart", ".erl", ".ex", ".exs", ".go",
        ".groovy", ".h", ".hpp", ".hs", ".java", ".jl", ".js", ".jsx", ".kt", ".kts", ".lua", ".m", ".mjs",
        ".ml", ".mm", ".php", ".pl", ".ps1", ".py", ".pyi", ".r", ".rb", ".rs", ".scala", ".sh", ".sql",
        ".svelte", ".swift", ".tf", ".ts", ".tsx", ".vue", ".zig", ".zsh",
    }
)
# Asked once per turn, at the first stop after a code edit. It carries one
# check only: a second copy or call site of the changed logic that had to change
# with it, the miss a model makes when it stops as soon as the target test
# passes. Scope and verification lines were measured too, showed no effect, and
# their "check again" wording made models re-run tests (bench README,
# 2026-10-30), so the note says not to.
#
# The host keeps the answer the model stopped with only as an interim message:
# whatever the model says after this note becomes the turn's final response,
# the one `--oneshot`, API and delegating callers receive. So the note ends by
# asking for that answer again, never for a report on the check.
ROUND_END_CHECKLIST: Final = (
    "OMH round-end consistency check, once per turn. Look for other copies or call sites of the logic "
    "you changed this turn that have to change with it, and fix any you find. Do not re-run tests or "
    "re-check anything else. Then restate your final answer exactly as you gave it, in the form the task "
    "asked for (updated only if you changed something more), with no report on this check: it replaces "
    "the answer you just gave. This note is guidance, not verification evidence."
)
# Delegated children answer a parent, often against an output schema; their
# final response is the parent's tool result and must stay the task's answer.
_NO_CHECKLIST_PLATFORMS: Final = frozenset({"subagent"})


def pre_verify(
    session_id: str = "",
    platform: str = "",
    model: str = "",
    coding: object = False,
    attempt: object = 0,
    final_response: str = "",
    changed_paths: object = None,
    **_kwargs: object,
) -> dict[str, str] | None:
    valid_coding = coding is True
    attempt_value = _attempt_value(attempt)
    paths = _normalized_paths(changed_paths)
    observation = {key: _kwargs[key] for key in _OBSERVATION_KEYS if key in _kwargs}
    observation.update(
        {
            "session_id": session_id,
            "coding": valid_coding,
            "attempt": attempt_value if attempt_value is not None else -1,
            "changed_path_count": len(paths),
            "changed_path_categories": _path_categories(paths),
            "session_present": bool(session_id),
            "platform_present": bool(platform),
            "model_present": bool(model),
            "final_response_present": bool(final_response),
        }
    )
    _ = observe_plugin_hook_call("pre_verify", observation)
    if not valid_coding or attempt_value is None or not paths:
        return None
    plan_message, stamp, open_items = _session_plan_reading(_kwargs, session_id)
    # The host offers `agent.max_verify_nudges` nudges per turn and enforces
    # that bound itself, so the plan line may spend more than the first -- but
    # only while the plan keeps being written. A nudge that produced no movement
    # does not repeat, which is what the idempotency guidance was protecting;
    # `nudge_budget` holds the one piece of per-turn state that tells the two
    # apart.
    if not plan_nudge_allowed(session_id=session_id, attempt=attempt_value, stamp=stamp):
        plan_message = ""
    # The served-surface gate stays one-shot. It asks for the check this turn's
    # edit just earned, and the edits are the same on a later attempt of the
    # same turn, so repeating it would restate a request the turn already has.
    surface_message = _served_surface_message(paths) if attempt_value == 0 else ""
    # The round-end check is one-shot for the same reason, and it is the
    # turn's only one: attempt 0 is the first stop after the edits, and the
    # host moves `attempt` past 0 once any nudge here continues the turn.
    checklist_message = (
        ROUND_END_CHECKLIST
        if attempt_value == 0 and str(platform) not in _NO_CHECKLIST_PLATFORMS and any(map(_is_code_path, paths))
        else ""
    )
    # Three obligations, one directive. The served-surface gate asks for the
    # check the edit just earned; the checklist asks the model to review its
    # own diff; the plan directive says the run is not finished. All can be
    # true of the same turn, and dropping one because another fired would
    # lose exactly the case each was written for.
    messages = [text for text in (surface_message, checklist_message, plan_message) if text]
    if not messages:
        return None
    # Recorded for the person once per turn. `attempt` is the only turn
    # boundary this hook is given (no turn id), and attempt 0 always passes
    # `plan_nudge_allowed`, so a plan line there is the turn's first; a later
    # attempt re-issuing it is the same catch with the plan still moving.
    if attempt_value == 0 and plan_message and open_items:
        record_catch(str(_kwargs.get("omh_home") or ""), session_id, PLAN_CONTINUED, open_items=open_items)
    return {"action": "continue", "message": "\n".join(messages)}


def _session_plan_reading(kwargs: dict[str, object], session_id: str) -> tuple[str, str, int]:
    """The open-plan directive for the session whose turn is ending, its stamp, and its open items.

    Homes bind the way ``pre_llm_call`` binds them. A binding failure is
    silence: a plugin that cannot read the plan has nothing to say about it
    and must not fail the turn it is decorating. It also has no stamp, so a
    turn whose homes stop binding mid-way spends no further budget.
    """
    try:
        omh_home = str(runtime_paths.plugin_home(kwargs.get("omh_home")))
        hermes_home = str(runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True))
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError):
        return "", "", 0
    return plan_continuation_parts(
        omh_home=omh_home, hermes_home=hermes_home, session_ref=session_id
    )


def _normalized_paths(value: object) -> tuple[str, ...]:
    if not _is_path_sequence(value):
        return ()
    return _normalized_string_paths(value)


def _is_path_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, (list, tuple))


def _normalized_string_paths(paths: Sequence[object]) -> tuple[str, ...]:
    return tuple(path.replace("\\", "/").lower() for path in paths if isinstance(path, str) and path.strip())


def _attempt_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _path_categories(paths: tuple[str, ...]) -> list[str]:
    categories: list[str] = []
    if any(_is_plugin_path(path) for path in paths):
        categories.append("plugin")
    if any(_is_ui_path(path) for path in paths):
        categories.append("ui")
    if any(PurePosixPath(path).name in _DEPENDENCY_FILES for path in paths):
        categories.append("dependency")
    if any(_is_ci_path(path) for path in paths):
        categories.append("ci")
    return categories or (["other"] if paths else [])


def _served_surface_message(paths: tuple[str, ...]) -> str:
    if any(_is_plugin_path(path) for path in paths):
        return (
            "OMH served-surface verification gate: this change affects a Hermes plugin surface. "
            "Run the smallest real plugin load and registration smoke available, then report the observed command and result. "
            "This nudge is guidance, not plugin-load or verification evidence."
        )
    if any(_is_ui_path(path) for path in paths):
        return (
            "OMH served-surface verification gate: this change affects a rendered surface. "
            "Open or render the changed UI at representative desktop and mobile sizes, inspect interaction and text layout, "
            "and report observed evidence. This nudge is guidance, not visual QA evidence."
        )
    if any(PurePosixPath(path).name in _DEPENDENCY_FILES for path in paths):
        return (
            "OMH served-surface verification gate: this change affects dependency or build metadata. "
            "Run the smallest installation or import smoke plus the relevant project check, then report the observed command and result. "
            "This nudge is guidance, not installation or verification evidence."
        )
    if any(_is_ci_path(path) for path in paths):
        return (
            "OMH served-surface verification gate: this change affects CI workflow configuration. "
            "Validate the workflow syntax and exercise the nearest local command path before finishing. "
            "This nudge is guidance, not CI or verification evidence."
        )
    return ""


def _is_code_path(path: str) -> bool:
    return PurePosixPath(path).suffix in _CODE_SUFFIXES


def _is_ui_path(path: str) -> bool:
    return PurePosixPath(path).suffix in _UI_SUFFIXES


def _is_plugin_path(path: str) -> bool:
    return "/plugin_bundle/" in f"/{path.lstrip('/')}" or path.endswith("plugin.yaml")


def _is_ci_path(path: str) -> bool:
    return "/.github/workflows/" in f"/{path.lstrip('/')}"
