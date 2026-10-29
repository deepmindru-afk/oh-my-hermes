"""A Kimi main session gets its own execution block; children and other families do not."""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import register  # noqa: E402
from omh.plugin_bundle.omh.main_calibration import MAIN_SESSION_EXECUTION_CALIBRATIONS  # noqa: E402
from test_delegation_protocol_section import _SectionCtx  # noqa: E402

SECTION_ID = "omh.main-calibration"
KIMI_MODELS = ("moonshotai/kimi-k3.1-pro", "kimi-k3", "moonshotai/kimi-k3-ultrafast")


class MainSessionCalibrationTests(unittest.TestCase):
    def sections(self):
        ctx = _SectionCtx()
        register(ctx)
        return ctx.sections

    def section(self):
        found = [s for s in self.sections() if s["id"] == SECTION_ID]
        self.assertEqual(len(found), 1, "main calibration must register exactly once")
        return found[0]

    def test_registered_once_after_memory_and_kimi_main_renders_block_once(self):
        section = self.section()
        self.assertEqual(section["position"], "after_memory")
        expected = MAIN_SESSION_EXECUTION_CALIBRATIONS["kimi"]
        for model in KIMI_MODELS:
            with self.subTest(model=model):
                text = section["content"]({"platform": "discord", "model": model})
                self.assertEqual(text, expected)
                self.assertEqual(text.count("Kimi execution calibration:"), 1)
                self.assertLessEqual(len(text), section["max_chars"])

    def test_kimi_child_gets_nothing_here_and_keeps_its_own_child_block(self):
        sections = {s["id"]: s for s in self.sections()}
        info = {"platform": "subagent", "model": "moonshotai/kimi-k3.1-pro"}
        self.assertEqual(sections[SECTION_ID]["content"](info), "")
        child = sections["omh.child-calibration"]["content"](info)
        self.assertTrue(child)
        self.assertNotEqual(child, MAIN_SESSION_EXECUTION_CALIBRATIONS["kimi"])

    def test_other_families_render_nothing(self):
        callback = self.section()["content"]
        for model in ("anthropic/claude-opus-5-5", "openai/gpt-6.1-sol",
                      "deepseek/deepseek-v4.1-flash", "z-ai/glm-5", "qwen/qwen3.6-coder-plus", ""):
            with self.subTest(model=model):
                self.assertEqual(callback({"platform": "cli", "model": model}), "")

    def test_malformed_session_info_renders_nothing(self):
        callback = self.section()["content"]
        for info in (None, [], "discord", {"platform": "discord"}, {"platform": "discord", "model": None}):
            with self.subTest(info=info):
                self.assertEqual(callback(info), "")

    def test_block_text_avoids_authority_markers_and_stays_short(self):
        self.assertEqual(set(MAIN_SESSION_EXECUTION_CALIBRATIONS), {"kimi"})
        for text in MAIN_SESSION_EXECUTION_CALIBRATIONS.values():
            self.assertTrue(text.startswith("Kimi execution calibration:"))
            self.assertLessEqual(len(text), 600)
            for word in ("MUST", "CRITICAL", "SYSTEM", "DIRECTIVE", "["):
                self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
