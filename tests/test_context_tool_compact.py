"""The `omh_context` tool returns a compact view of the context brief.

Its result rides every later model call of the session. On the 2026-10-30
bench GPT-6 Luna (`tool_search` off) called it first in 20-33 of 33 coding
sessions and the full brief was about 39,700 characters. These tests pin what
the compact view drops, where each dropped piece is still served, and a size
ceiling so the result cannot grow back silently.
"""

from __future__ import annotations

import json
import unittest

from omh.plugin_bundle.omh.context_brief import (
    COMPACT_CHECKPOINT_ROUTE_FIELDS,
    CONTEXT_BRIEF_CATALOG_SECTIONS,
    build_context_brief,
    compact_context_brief,
)
from omh.plugin_bundle.omh.tools.capability_tool import omh_capabilities_handler
from omh.plugin_bundle.omh.tools.context_tool import OMH_CONTEXT_SCHEMA, omh_context_handler

BENCH_MESSAGE = (
    "Change build_quote in src/benchapp/quotes.py to return a frozen dataclass Quote and update its callers. "
    "Run tests with python -m pytest -q. Write the final machine answer as JSON to .omh-benchmark-answer.json."
)
# Measured 2026-10-31 on the 209 recorded `omh_context` calls of the OPT-C runs:
# 12,201-21,867 characters (mean 15,956; the full brief was 36,374-44,819). The
# largest carries two route hints and the prompt context; the ceiling keeps
# about 10% over it. A larger `limit` adds a route hint per step.
COMPACT_RESULT_CHAR_CEILING = 24_000


class ContextToolCompactTests(unittest.TestCase):
    def test_tool_result_drops_the_catalog_sections_capabilities_serves(self) -> None:
        result = json.loads(omh_context_handler({"message": BENCH_MESSAGE, "source": "hermes"}))
        summary = json.loads(omh_capabilities_handler({"action": "summary"}))
        full = build_context_brief(BENCH_MESSAGE, source="hermes")
        self.assertEqual(result["view"], "compact")
        for section in CONTEXT_BRIEF_CATALOG_SECTIONS:
            self.assertNotIn(section, result)
            self.assertIn(section, result["expand"])
            # The pointer is only true while omh_capabilities returns the same section.
            self.assertEqual(summary[section], full[section], section)

    def test_checkpoint_routes_keep_workflow_and_action_for_every_family(self) -> None:
        result = json.loads(omh_context_handler({"message": BENCH_MESSAGE}))
        full = build_context_brief(BENCH_MESSAGE)["generic_tool_checkpoint"]
        compact = result["generic_tool_checkpoint"]
        self.assertEqual(
            compact["routes"],
            [{field: route[field] for field in COMPACT_CHECKPOINT_ROUTE_FIELDS} for route in full["routes"]],
        )
        self.assertEqual({key: value for key, value in compact.items() if key != "routes"},
                         {key: value for key, value in full.items() if key != "routes"})

    def test_matched_route_hint_still_carries_fallback_and_not_evidence(self) -> None:
        result = json.loads(omh_context_handler({"message": "make an image card for this PR", "limit": 2}))
        hint = result["route_hint"]["hints"][0]
        self.assertEqual(result["route_hint"]["primary_workflow"], "img-summary")
        self.assertTrue(hint["fallback_action"])
        self.assertIn("image generation", hint["not_evidence_yet"])
        self.assertIn("first_response_shape", hint["workflow_context_card"])

    def test_every_other_field_is_the_full_brief(self) -> None:
        args = {"message": BENCH_MESSAGE, "limit": 2, "include_prompt_context": True}
        result = json.loads(omh_context_handler(args))
        full = build_context_brief(BENCH_MESSAGE, source="hermes-agent", max_hints=2, include_prompt_context=True)
        for key, value in full.items():
            if key in CONTEXT_BRIEF_CATALOG_SECTIONS or key == "generic_tool_checkpoint":
                continue
            self.assertEqual(result[key], value, key)

    def test_compact_result_stays_under_its_ceiling(self) -> None:
        # Two hints (the default limit) and the prompt context: the recorded maximum's shape.
        args = {"message": BENCH_MESSAGE, "limit": 2, "include_prompt_context": True}
        self.assertLess(len(omh_context_handler(args)), COMPACT_RESULT_CHAR_CEILING)

    def test_brief_without_the_full_sections_passes_unchanged(self) -> None:
        error = {"schema_version": "omh_context_brief_error/v1", "status": "error"}
        self.assertIs(compact_context_brief(error), error)

    def test_schema_description_is_unchanged(self) -> None:
        # Description wording moved GPT-6 Luna's call rate in OPT-C (21 -> 33 of 33);
        # the compact view must not ride a description change.
        self.assertEqual(
            OMH_CONTEXT_SCHEMA["description"],
            "Return compact OMH operating context for Hermes before generic chat, image, file, search, or coding "
            "tools. The payload is metadata-only and does not store or echo the raw user request.",
        )
        self.assertNotIn("view", OMH_CONTEXT_SCHEMA["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
