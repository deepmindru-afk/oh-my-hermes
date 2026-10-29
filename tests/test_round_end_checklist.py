"""The round-end checklist `pre_verify` adds after a turn that edited code.

When a turn's edits include a source file, the first stop of that turn
(`attempt == 0`) continues once with a short checklist: consistency (every
place that must change together did), scope (no edit nobody asked for) and
verification (a check that fails without the change, with its command). It is
one-shot: the host moves `attempt` past 0 as soon as any `pre_verify` message
continues the turn, so a later stop in the same turn never repeats it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo

# `agent.max_verify_nudges` default; the host enforces it.
MAX_VERIFY_NUDGES = 3
SESSION = "checklist-session"


class _Homes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True)
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def _fire(self, **overrides):
        payload = {
            "session_id": SESSION,
            "coding": True,
            "attempt": 0,
            "changed_paths": ["src/benchapp/pricing.py"],
            "omh_home": str(self.home),
            "hermes_home": str(self.hermes),
        }
        payload.update(overrides)
        return verify_hooks.pre_verify(**payload)


class ChecklistFiresOnCodeEditsTest(_Homes):
    def test_a_code_edit_continues_the_turn_with_the_checklist(self):
        self.assertEqual(self._fire(), {"action": "continue", "message": verify_hooks.ROUND_END_CHECKLIST})

    def test_the_checklist_names_consistency_scope_and_verification(self):
        text = verify_hooks.ROUND_END_CHECKLIST
        for item in ("- Consistency:", "- Scope:", "- Verification:"):
            self.assertIn(item, text)
        self.assertIn("not verification evidence", text)

    def test_any_code_file_among_the_edits_is_enough(self):
        for paths in (["README.md", "src/app.py"], ["web/main.TS"], ["cmd/server.go"], ["scripts/run.sh"]):
            with self.subTest(paths=paths):
                result = self._fire(changed_paths=paths)
                self.assertIsNotNone(result)
                self.assertIn(verify_hooks.ROUND_END_CHECKLIST, result["message"])

    def test_edits_without_code_get_no_checklist(self):
        for paths in (["docs/guide.md"], [".omh-benchmark-answer.json"], ["config/settings.yaml"], ["notes.txt", "data.csv"]):
            with self.subTest(paths=paths):
                self.assertIsNone(self._fire(changed_paths=paths))

    def test_outside_a_coding_posture_or_on_a_malformed_attempt_it_is_silent(self):
        self.assertIsNone(self._fire(coding=False))
        self.assertIsNone(self._fire(attempt=-1))
        self.assertIsNone(self._fire(attempt="0"))


class ChecklistIsOncePerTurnTest(_Homes):
    def test_a_later_attempt_in_the_same_turn_does_not_repeat_it(self):
        for attempt in range(1, MAX_VERIFY_NUDGES):
            with self.subTest(attempt=attempt):
                self.assertIsNone(self._fire(attempt=attempt))

    def test_a_host_loop_sees_the_checklist_exactly_once_per_turn(self):
        # The host's gate: call while `attempt < max_verify_nudges`, and move
        # `attempt` on only when a message continued the turn.
        issued = []
        attempt = 0
        for _stop in range(MAX_VERIFY_NUDGES + 2):
            if attempt >= MAX_VERIFY_NUDGES:
                break
            result = self._fire(attempt=attempt)
            if result is None:
                break
            issued.append(result["message"])
            attempt += 1
        self.assertEqual(issued, [verify_hooks.ROUND_END_CHECKLIST])

    def test_a_new_turn_gets_it_again(self):
        self.assertIsNotNone(self._fire(attempt=0))
        self.assertIsNone(self._fire(attempt=1))
        self.assertIsNotNone(self._fire(attempt=0))


class ChecklistSitsBetweenTheOtherObligationsTest(_Homes):
    def test_a_rendered_surface_edit_carries_the_surface_gate_then_the_checklist(self):
        message = self._fire(changed_paths=["src/app.tsx"])["message"]
        surface, checklist = message.split("\n", 1)
        self.assertIn("rendered surface", surface)
        self.assertEqual(checklist, verify_hooks.ROUND_END_CHECKLIST)

    def test_an_open_plan_follows_the_checklist(self):
        record = build_todo_record(
            "plan",
            [{"text": "fix", "state": "done"}, {"text": "report", "state": "pending"}],
            source="test",
            session_ref=SESSION,
        )
        _ = write_todo(self.home, record)
        message = self._fire()["message"]
        self.assertTrue(message.startswith(verify_hooks.ROUND_END_CHECKLIST + "\n"))
        self.assertIn("[OMH plan todo]", message)


if __name__ == "__main__":
    unittest.main()
