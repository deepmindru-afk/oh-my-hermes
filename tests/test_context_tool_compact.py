"""The `omh_context` tool returns a compact view of the context brief.

Its result rides every later model call of the session. On the 2026-10-30
bench GPT-6 Luna (`tool_search` off) called it first in 20-33 of 33 coding
sessions and the full brief was about 39,700 characters. These tests pin what
the compact view drops, where each dropped piece is still served, and a size
ceiling so the result cannot grow back silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest

from omh.plugin_bundle.omh.context_brief import (
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
MULTI_CUE_MESSAGE = "make an image card for this PR and search current news and run pytest on the PR"
# Re-derived 2026-10-31 on the 209 recorded `omh_context` calls of the OPT-C runs,
# after the compact routes kept their fallback and not-evidence lists: 15,154-24,820
# characters (mean 18,909; the full brief was 36,374-44,819). The largest carries
# two route hints and the prompt context; the ceiling keeps about 10% over it.
COMPACT_RESULT_CHAR_CEILING = 27_500
# No recorded call passed a `limit` above 2, but the schema allows 10, and each
# extra matched hint adds about 3,500 characters (its workflow_context_card rides
# with it). MULTI_CUE_MESSAGE matches 4 hints at limit 10: 30,281 characters with
# the prompt context on 2026-10-31; this ceiling keeps about 10% over it.
COMPACT_RESULT_LIMIT_10_CHAR_CEILING = 33_500
# The route fields the compact view keeps, as literals so a change to the
# producer's tuple has to change this test too.
KEPT_ROUTE_FIELDS = ("tool_family", "primary_workflow", "primary_next_action", "fallback_action", "not_evidence_yet")
STANDALONE_PROBE = textwrap.dedent(
    """
    import importlib.util
    import json
    from pathlib import Path
    import sys

    plugin_dir = Path(sys.argv[1])
    assert importlib.util.find_spec("omh") is None
    spec = importlib.util.spec_from_file_location(
        "_compact_plugin", plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


    class Context:
        def __init__(self):
            self.tools = {}

        def register_tool(self, name, *args, **kwargs):
            self.tools[name] = args[2]

        def register_hook(self, name, handler):
            pass


    context = Context()
    module.register(context)
    from _compact_plugin.context_brief import build_context_brief

    message = sys.argv[2]
    print(json.dumps({
        "context": json.loads(context.tools["omh_context"]({"message": message})),
        "summary": json.loads(context.tools["omh_capabilities"]({"action": "summary"})),
        "full": build_context_brief(message),
    }))
    """
)


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

    def test_standalone_bundle_drops_what_its_own_capabilities_tool_serves(self) -> None:
        # Hermes loads the bundle with its own interpreter, where `omh` is absent and both
        # tools take their standalone fallbacks; the pointer has to hold there too.
        bundle = Path(__file__).resolve().parents[1] / "src" / "plugin_bundle" / "omh"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "omh"
            shutil.copytree(bundle, plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
            for name in ("home", "hermes", "omh-home"):
                (root / name).mkdir()
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", STANDALONE_PROBE, str(plugin_dir), BENCH_MESSAGE],
                cwd=root,
                env={**os.environ, "HOME": str(root / "home"), "HERMES_HOME": str(root / "hermes"),
                     "OMH_HOME": str(root / "omh-home")},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        context = payload["context"]
        self.assertEqual(context["source_backend"], "standalone_plugin_bundle_fallback")
        self.assertEqual(context["view"], "compact")
        for section in CONTEXT_BRIEF_CATALOG_SECTIONS:
            self.assertNotIn(section, context)
            self.assertEqual(payload["summary"][section], payload["full"][section], section)

    def test_checkpoint_routes_keep_workflow_action_fallback_and_not_evidence(self) -> None:
        result = json.loads(omh_context_handler({"message": BENCH_MESSAGE}))
        full = build_context_brief(BENCH_MESSAGE)["generic_tool_checkpoint"]
        compact = result["generic_tool_checkpoint"]
        self.assertEqual(
            compact["routes"],
            [{field: route[field] for field in KEPT_ROUTE_FIELDS} for route in full["routes"]],
        )
        for route in compact["routes"]:
            self.assertTrue(route["fallback_action"], route["tool_family"])
            self.assertTrue(route["not_evidence_yet"], route["tool_family"])
        self.assertEqual({key: value for key, value in compact.items() if key != "routes"},
                         {key: value for key, value in full.items() if key != "routes"})

    def test_every_matched_hint_carries_its_lane_card_as_expand_says(self) -> None:
        result = json.loads(omh_context_handler({"message": MULTI_CUE_MESSAGE, "limit": 10}))
        # The second clause is the claim this test checks; "workflow_context_card" alone also
        # matches the first clause's "workflow_context_cards".
        self.assertIn("carries the workflow_context_card of its matched workflow's lane", result["expand"])
        self.assertNotIn("family's", result["expand"])
        hints = result["route_hint"]["hints"]
        self.assertGreater(len(hints), 1)
        for hint in hints:
            self.assertEqual(hint["workflow_context_card"]["id"], hint["lane"], hint["workflow"])
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

    def test_compact_result_at_the_largest_limit_stays_under_its_ceiling(self) -> None:
        args = {"message": MULTI_CUE_MESSAGE, "limit": 10, "include_prompt_context": True}
        self.assertLess(len(omh_context_handler(args)), COMPACT_RESULT_LIMIT_10_CHAR_CEILING)

    def test_view_marks_the_compact_shape_under_the_same_schema_version(self) -> None:
        result = json.loads(omh_context_handler({"message": BENCH_MESSAGE}))
        self.assertEqual(result["schema_version"], "omh_context_brief/v1")
        self.assertEqual(result["view"], "compact")
        self.assertNotIn("view", build_context_brief(BENCH_MESSAGE))

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
