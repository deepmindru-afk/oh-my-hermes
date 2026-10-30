"""The round-end consistency check `pre_verify` adds after a turn that edited code.

When a turn's edits include a source file, the first stop of that turn
(`attempt == 0`) continues once with a consistency check: other copies or call
sites of the changed logic that had to change with it. It is one-shot: the
host moves `attempt` past 0 as soon as any `pre_verify` message continues the
turn, so a later stop in the same turn never repeats it.

Whatever the model says after the check becomes the turn's final response, so
without an open plan the message ends with one closing sentence: reply with a
fixed sentinel line if nothing more changed. `pre_verify` stashes the answer
the turn stopped with, and `transform_llm_output` swaps the sentinel back to
it. Delegated children never get the check; that only matters under
`agent.coding_context: on`, the one posture in which a `subagent` session
arrives with `coding=True`, which is the state these tests send.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.hooks import nudge_budget, result_transforms, verify_hooks
from omh.plugin_bundle.omh.impact_check import format_note
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo
from omh.tui.gateway import Event
from omh.tui.transcript import AssistantBlock, Transcript

# `agent.max_verify_nudges` default; the host enforces it.
MAX_VERIFY_NUDGES = 3
SESSION = "checklist-session"
ANSWER = '{"changed_paths": ["src/benchapp/pricing.py", "tests/test_pricing.py"]}'
CHECK = verify_hooks.ROUND_END_CHECKLIST
CLOSING = verify_hooks.ROUND_END_CLOSING
SENTINEL = verify_hooks.ROUND_END_SENTINEL


class _Homes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True)
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        verify_hooks.reset_stashed_answers()
        self.addCleanup(verify_hooks.reset_stashed_answers)

    def _fire(self, **overrides):
        payload = {
            "session_id": SESSION,
            "coding": True,
            "attempt": 0,
            "final_response": ANSWER,
            "changed_paths": ["src/benchapp/pricing.py"],
            "omh_home": str(self.home),
            "hermes_home": str(self.hermes),
        }
        payload.update(overrides)
        return verify_hooks.pre_verify(**payload)

    def _open_plan(self):
        record = build_todo_record(
            "plan",
            [{"text": "fix", "state": "done"}, {"text": "report", "state": "pending"}],
            source="test",
            session_ref=SESSION,
        )
        _ = write_todo(self.home, record)


class ChecklistFiresOnCodeEditsTest(_Homes):
    def test_a_code_edit_continues_the_turn_with_the_check_then_the_closing(self):
        self.assertEqual(self._fire(), {"action": "continue", "message": f"{CHECK}\n{CLOSING}"})

    def test_the_check_is_consistency_only_and_scoped(self):
        self.assertIn("other copies or call sites of the logic", CHECK)
        self.assertIn("Do not re-run tests for this check.", CHECK)
        self.assertIn("not verification evidence", CHECK)
        for dropped in ("Scope:", "Verification:", "re-check anything else", "restate"):
            self.assertNotIn(dropped, CHECK)
        self.assertIn(f"\n{SENTINEL}\n", CLOSING)

    def test_without_an_answer_to_stash_the_closing_asks_for_the_answer_itself(self):
        for missing in ({"final_response": ""}, {"final_response": None}, {"session_id": ""}):
            with self.subTest(missing=missing):
                message = self._fire(**missing)["message"]
                self.assertEqual(message, f"{CHECK}\n{verify_hooks.ROUND_END_CLOSING_UNSTASHED}")
                self.assertNotIn(SENTINEL, message)

    def test_a_delegated_child_gets_no_checklist(self):
        # coding=True with platform "subagent" is the `agent.coding_context: on` case.
        self.assertIsNone(self._fire(platform="subagent"))
        for platform in ("cli", "tui", "", None, ["cli"]):
            with self.subTest(platform=platform):
                self.assertIsNotNone(self._fire(platform=platform))

    def test_any_code_file_among_the_edits_is_enough(self):
        for paths in (["README.md", "src/app.py"], ["web/main.TS"], ["cmd/server.go"], ["scripts/run.sh"], ["infra/main.tf"], ["lib/app.ex"]):
            with self.subTest(paths=paths):
                result = self._fire(changed_paths=paths)
                self.assertIsNotNone(result)
                self.assertIn(CHECK, result["message"])

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
        self.assertEqual(issued, [f"{CHECK}\n{CLOSING}"])

    def test_a_new_turn_gets_it_again(self):
        self.assertIsNotNone(self._fire(attempt=0))
        self.assertIsNone(self._fire(attempt=1))
        self.assertIsNotNone(self._fire(attempt=0))


class MessageOrderTest(_Homes):
    def test_surface_gate_then_check_then_one_closing_and_the_gate_request_stands(self):
        for paths in (["src/app.tsx"], ["src/plugin_bundle/omh/tool.py"], ["src/app.py", "pyproject.toml"],
                      ["src/app.py", ".github/workflows/ci.yml"]):
            with self.subTest(paths=paths):
                message = self._fire(changed_paths=paths)["message"]
                surface, rest = message.split("\n", 1)
                self.assertIn("OMH served-surface verification gate", surface)
                self.assertEqual(rest, f"{verify_hooks.ROUND_END_CHECKLIST_BESIDE_A_RECHECK}\n{CLOSING}")
                self.assertEqual(message.count("When you are done"), 1)
                # Beside the gate's request to run something, the check says nothing about not re-running.
                self.assertNotIn("Do not re-run", message)

    def test_an_open_plan_follows_the_check_with_no_closing_and_no_stash(self):
        self._open_plan()
        message = self._fire()["message"]
        self.assertTrue(message.startswith(CHECK + "\n"))
        self.assertIn("[OMH plan todo]", message)
        self.assertNotIn("When you are done", message)
        self.assertNotIn(SENTINEL, message)
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL))


class WithTheImpactNoteTest(_Homes):
    """#2410's impact note joins the same message: surface gate, impact note, check, plan, one closing."""

    NOTE = format_note([("src/shop/report.py", 5, 'Cannot access attribute "get"')], recheck_tool=False)
    PATHS = ["src/app.tsx", "src/shop/quote.py"]

    def _fire_with_note(self, **overrides):
        with patch.object(verify_hooks, "round_end_note", lambda session_id, changed_paths: self.NOTE):
            return self._fire(changed_paths=self.PATHS, **overrides)

    def _parts(self, message):
        surface = message.index("OMH served-surface verification gate")
        impact = message.index("OMH impact check")
        check = message.index("OMH round-end consistency check")
        return surface, impact, check

    def test_all_four_come_in_the_defined_order_and_the_plan_leaves_no_closing(self):
        self._open_plan()
        message = self._fire_with_note()["message"]
        surface, impact, check = self._parts(message)
        plan = message.index("[OMH plan todo]")
        self.assertLess(surface, impact)
        self.assertLess(impact, check)
        self.assertLess(check, plan)
        self.assertNotIn("final answer", message.replace(self.NOTE, ""))
        self.assertNotIn("final answer", self.NOTE)
        self.assertNotIn("Do not re-run", message)

    def test_without_a_plan_one_closing_sentence_ends_the_message(self):
        message = self._fire_with_note()["message"]
        surface, impact, check = self._parts(message)
        self.assertLess(surface, impact)
        self.assertLess(impact, check)
        self.assertTrue(message.endswith(CLOSING))
        self.assertEqual(message.count("When you are done"), 1)
        self.assertEqual(message.count("final answer"), 1)
        # The note asks to fix callers and re-check; the check beside it must not say "do not re-run".
        self.assertIn(verify_hooks.ROUND_END_CHECKLIST_BESIDE_A_RECHECK, message)
        self.assertNotIn("Do not re-run", message)

    def test_the_note_alone_outside_coding_still_gets_the_one_closing(self):
        with patch.object(verify_hooks, "round_end_note", lambda session_id, changed_paths: self.NOTE):
            message = self._fire(coding=False, changed_paths=self.PATHS)["message"]
        self.assertEqual(message, f"{self.NOTE}\n{CLOSING}")

    def test_the_swap_fires_only_on_the_exact_sentinel_after_an_impact_fix(self):
        self._fire_with_note()
        updated = '{"changed_paths": ["src/shop/quote.py", "src/shop/report.py"]}'
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=updated))
        self._fire_with_note()
        self.assertEqual(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL), ANSWER)


class SentinelSwapTest(_Homes):
    def test_the_sentinel_comes_back_as_the_stashed_answer_byte_for_byte_once(self):
        self._fire()
        self.assertEqual(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL), ANSWER)
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL))

    def test_light_wrapping_of_the_sentinel_still_matches(self):
        for reply in (f"  {SENTINEL}\n", f"`{SENTINEL}`", f'"{SENTINEL}"'):
            with self.subTest(reply=reply):
                self._fire()
                self.assertEqual(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=reply), ANSWER)

    def test_any_other_reply_is_kept_and_clears_the_stash(self):
        self._fire()
        updated = '{"changed_paths": ["src/benchapp/pricing.py", "src/benchapp/refunds.py"]}'
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=updated))
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL))
        self._fire()
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=f"{SENTINEL} Also X."))

    def test_never_swaps_without_a_stash_or_for_another_session(self):
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL))
        self._fire()
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id="other", response_text=SENTINEL))
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id="", response_text=SENTINEL))

    def test_a_stale_stash_is_never_swapped(self):
        with patch.object(verify_hooks.time, "monotonic", return_value=1000.0):
            self._fire()
        later = 1000.0 + verify_hooks.STASHED_ANSWER_SECONDS + 1
        with patch.object(verify_hooks.time, "monotonic", return_value=later):
            self.assertIsNone(verify_hooks.restore_round_end_answer(session_id=SESSION, response_text=SENTINEL))

    def test_the_stash_is_bounded(self):
        for index in range(verify_hooks.MAX_STASHED_ANSWERS + 5):
            self._fire(session_id=f"s{index}")
        self.assertEqual(len(verify_hooks._STASHED_ANSWERS), verify_hooks.MAX_STASHED_ANSWERS)
        self.assertIsNone(verify_hooks.restore_round_end_answer(session_id="s0", response_text=SENTINEL))

    def test_the_composed_output_hook_returns_the_original_answer(self):
        self._fire()
        restored = result_transforms.transform_llm_output(
            response_text=SENTINEL, session_id=SESSION, model="m", platform="cli", turn_id="t1",
        )
        self.assertEqual(restored, ANSWER)
        self.assertIsNone(result_transforms.transform_llm_output(
            response_text=SENTINEL, session_id=SESSION, model="m", platform="cli", turn_id="t2",
        ))


class TuiKeepsTheSentinelLineTest(unittest.TestCase):
    def _turn(self, final_text: str, streamed: str = SENTINEL) -> list[str]:
        transcript = Transcript()
        sid = "tui-session"
        transcript.apply(Event("message.start", sid, {}))
        transcript.apply(Event("message.delta", sid, {"text": ANSWER}))
        transcript.apply(Event("message.interim", sid, {"text": ANSWER, "already_streamed": True}))
        transcript.apply(Event("message.delta", sid, {"text": streamed}))
        transcript.apply(Event("message.complete", sid, {"text": final_text, "status": "complete"}))
        return [block.text for block in transcript.blocks if isinstance(block, AssistantBlock)]

    def test_the_swapped_answer_is_not_printed_a_second_time(self):
        self.assertEqual(self._turn(ANSWER), [ANSWER, SENTINEL])

    def test_an_updated_answer_still_settles_the_live_block(self):
        updated = '{"changed_paths": ["a.py"]}'
        self.assertEqual(self._turn(updated, streamed=updated), [ANSWER, updated])


if __name__ == "__main__":
    unittest.main()
