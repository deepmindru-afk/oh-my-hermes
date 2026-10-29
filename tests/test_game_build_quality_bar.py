"""Build-time looking and the playable check, the game lane of `frontend` and `visual-qa`.

A scene or game built in one unseen batch ships its defects together, and a
game that boots to a still frame can be dead on input. These tests pin the
`frontend` rule to look at each visual change while building, the
`screenshot-loop.md` section that states it, and the `visual-qa` rule that a
game passes only when it boots, answers input, keeps running and logs no
console errors.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from omh.skill_pack import builtin_skill_templates
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_portable import PORTABLE_REFERENCE_PATHS
from omh.skills.catalog_types import omh_skill_display_name
from omh.skills.packaging import builtin_skill_reference_templates

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP = ("frontend", "references/screenshot-loop.md")
PLAYABLE = ("visual-qa", "references/playable-check.md")


def _unwrapped(content: str) -> str:
    return " ".join(content.split())


def _reference(key: tuple[str, str]) -> str:
    for template in builtin_skill_reference_templates():
        if (template.skill_name, template.relative_path) == key:
            return template.content
    raise AssertionError(f"{key} is not packaged")


def _definition(name: str):
    for definition in builtin_definitions():
        if definition.name == name:
            return definition
    raise AssertionError(f"missing skill {name}")


class ObserveEachChangeTests(unittest.TestCase):
    def test_frontend_quality_bar_names_the_section(self) -> None:
        lines = [
            line for line in _definition("frontend").quality_bar
            if "look at each change that affects what renders" in line
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn("references/screenshot-loop.md", lines[0])
        self.assertIn("During implementation", lines[0])
        self.assertIn("a change not looked at is not done", lines[0])

    def test_the_section_comes_before_the_loop(self) -> None:
        content = _reference(LOOP)
        self.assertIn("## During implementation", content)
        self.assertLess(content.index("## During implementation"), content.index("## The loop"))
        section = _unwrapped(content.split("## During implementation")[1].split("## The loop")[0])
        self.assertIn("default camera and each declared preset", section)
        self.assertIn("A batch of unseen edits compounds", section)
        self.assertIn("does not replace the loop", section)
        self.assertIn("1440/768/375", section)


class PlayableCheckTests(unittest.TestCase):
    def test_both_visual_qa_bodies_carry_the_line(self) -> None:
        definition = _definition("visual-qa")
        bodies = [definition.quality_bar]
        for override in (definition.portable_overrides or {}).values():
            if isinstance(override, (list, tuple)):
                bodies.append(override)
        carriers = [
            body for body in bodies
            if any("references/playable-check.md" in line for line in body)
        ]
        self.assertGreaterEqual(len(carriers), 2, "Hermes and portable bodies both need the line")
        for template in builtin_skill_templates():
            if template.name == "visual-qa":
                self.assertIn("references/playable-check.md", _unwrapped(template.content))

    def test_the_reference_is_packaged_generated_and_portable(self) -> None:
        content = _reference(PLAYABLE)
        self.assertIn(f"{PLAYABLE[0]}/{PLAYABLE[1]}", PORTABLE_REFERENCE_PATHS)
        path = REPO_ROOT / "skills" / omh_skill_display_name(PLAYABLE[0]) / PLAYABLE[1]
        self.assertTrue(path.exists(), path)
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_the_four_checks_and_the_verdict_rules(self) -> None:
        content = _unwrapped(_reference(PLAYABLE))
        for token in ("**Boots.**", "**Answers input.**", "**Keeps running.**", "**Clean console.**"):
            self.assertIn(token, content)
        self.assertIn("not blank and not a single flat colour", content)
        self.assertIn("Background-only animation", content)
        self.assertIn("zero errors across boot and the input", content)
        self.assertIn("is BLOCK, not PASS", content)
        self.assertIn("never passes a check", content)
        self.assertIn("environment: no_webgl", content)
        self.assertIn("not judged as a blank frame", content)
        self.assertIn("| check | capture path | verdict |", content)


if __name__ == "__main__":
    unittest.main()
