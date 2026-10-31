"""Point an OMH memory record back at the Hermes message it came from.

OMH keeps one bounded summary line per memory and never a transcript. Hermes
already keeps every message verbatim in ``<HERMES_HOME>/state.db``, and its
``session_search`` tool opens a window around one message id. So a model
capture records only which session it was made in
(``source_ref = "hermes-session:<session_id>"``), and recall resolves that to
the person's message just before the capture: a handle the model can open to
quote the original words. No message text is read into or copied by OMH.

The pointer-not-copy split follows MemPalace's closet/drawer idea
(``MemPalace/mempalace@84582fb055a6429261f9c779da86c0561ec80adc``, MIT): the
summary is the closet, the verbatim store is the drawer. The mechanism is
reimplemented here against Hermes' own session store.

The read is read-only and bounded: a ``mode=ro`` open with a short busy
timeout, one indexed ``SELECT`` of an id, and no ``PRAGMA``. A missing,
corrupt or locked store gives no origin rather than an error.

Stdlib only and no ``omh`` import: Hermes loads this directory with its own
interpreter.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

# The bundle's guarded `strict_fromisoformat` parse (hour 24 refused), naive read as UTC.
from .memory_recall_support import _parse_utc_naive_as_utc
from .sqlite_readonly import connect_readonly

ORIGIN_REF_PREFIX = "hermes-session:"
# A Hermes session id as a tool call carries it; anything else gets no origin.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
# The capture happens later in the same turn as the person's message, so the
# message is at or before the capture stamp; the slack covers a stamp OMH
# truncates to the second while Hermes keeps sub-second time.
_CAPTURE_SLACK_SECONDS = 5.0
_BUSY_TIMEOUT_SECONDS = 0.2


def origin_source_ref(session_id: str) -> str:
    """The ``source_ref`` a model capture in ``session_id`` records, or ``""`` for an unusable id."""
    return ORIGIN_REF_PREFIX + session_id if _SAFE_SESSION_ID.fullmatch(session_id or "") else ""


def origin_session_id(source_ref: str) -> str:
    """The session id a ``hermes-session:`` ref names, or ``""`` when it names none."""
    if not source_ref.startswith(ORIGIN_REF_PREFIX):
        return ""
    session_id = source_ref[len(ORIGIN_REF_PREFIX) :]
    return session_id if _SAFE_SESSION_ID.fullmatch(session_id) else ""


def resolve_origin(hermes_home: Path, session_id: str, captured_at: str) -> dict | None:
    """``{"session_id", "around_message_id"}`` for the person's message before a capture, or None.

    The newest ``role='user'`` message of ``session_id`` stamped no later than
    ``captured_at`` plus the slack. None when the store is missing, the session
    or message is absent, the stamp does not parse, or the store refuses the
    read.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id or ""):
        return None
    moment = _parse_utc_naive_as_utc(str(captured_at or ""))
    if moment is None:
        return None
    cutoff = moment.timestamp() + _CAPTURE_SLACK_SECONDS
    path = Path(hermes_home) / "state.db"
    if not path.is_file():
        return None
    try:
        connection = connect_readonly(path, timeout=_BUSY_TIMEOUT_SECONDS)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        row = connection.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND timestamp <= ? ORDER BY id DESC LIMIT 1",
            (session_id, cutoff),
        ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int) or isinstance(row[0], bool):
        return None
    return {"session_id": session_id, "around_message_id": row[0]}
