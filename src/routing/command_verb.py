"""A request's command verb, and the verbs that make it a code edit: one list for every reader.

The direct code-edit guard in `policy` and the workflow-intent classifier in
`intent` both ask whether a request commands an edit. `policy` imports
`intent`, so the words live here, in a module both can import without a
cycle. Stdlib only.
"""

from __future__ import annotations

import re

from ..plugin_bundle.omh.request_lead import after_courtesy

DIRECT_CODING_EDIT_VERBS = frozenset(
    {"add", "adjust", "build", "change", "delete", "edit", "fix", "implement", "make", "patch", "remove", "rename", "set", "tweak", "update"}
)

# The word before a verb that still makes it the command: "use X to fix",
# "help me fix", or a second command joined by "and"/"then".
_COMMAND_INTRODUCERS = frozenset({"to", "me", "and", "then"})


# The words of a message from its command verb on, after the courtesy openers
# and the address to the assistant ("please", "can you", "omh", "hermes,") that
# every reader of a request's opening strips (`request_lead.COURTESY_OPENERS`).
# Used only to NARROW guards: an edit verb that is not the command verb does
# not make a request an edit.
def command_words(normalized_query: str) -> list[str]:
    return re.findall(r"[a-z0-9#][a-z0-9#+.-]*", after_courtesy(normalized_query.strip()))


def commanded_words(words: list[str]) -> set[str]:
    """The command verb, a verb introduced as the task, or a second command joined by "and"/"then"."""
    return set(words[:1]) | {words[i] for i in range(1, len(words)) if words[i - 1] in _COMMAND_INTRODUCERS}
