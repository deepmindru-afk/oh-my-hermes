"""Model-matched execution guidance for a main session, separate from child blocks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .calibration import _calibration_family
from .delegation_protocol import CHILD_PLATFORM

MAIN_SESSION_EXECUTION_CALIBRATIONS: Final[dict[str, str]] = {
    "kimi": (
        "Kimi execution calibration: Decide minor choices (names, defaults, equivalent "
        "approaches) yourself and note them; ask only before irreversible or "
        "externally visible actions, or when a missing fact changes the outcome. "
        "Once the deciding fact is in view, pick one approach and reopen it only on "
        "contradicting evidence. When the next step is obvious, call the tool "
        "instead of describing it. Explain or check means answer or report; build, "
        "fix, or change means do the work, never edits nobody asked for. Stop "
        "searching when sources agree; repeat no identical call more than twice."
    ),
}

MAIN_CALIBRATION_SECTION_ID: Final[str] = "omh.main-calibration"
# The host refuses oversized sections rather than truncating them.
MAIN_CALIBRATION_SECTION_MAX_CHARS: Final[int] = max(
    len(text) for text in MAIN_SESSION_EXECUTION_CALIBRATIONS.values()
)


def main_calibration_section(session_info: object) -> str:
    """Render a per-family execution block for the main session, once at session start.

    Hermes renders plugin sections once per session, so this costs nothing per
    turn. Children get their own block from ``child_calibration``; this one
    renders nothing for them. Only Kimi has an entry, until an A/B shows another
    family needs one. The model comes from session metadata, never parent config.
    """
    if not isinstance(session_info, Mapping) or session_info.get("platform") == CHILD_PLATFORM:
        return ""
    model = str(session_info.get("model", "") or "")
    return MAIN_SESSION_EXECUTION_CALIBRATIONS.get(_calibration_family(model), "")
