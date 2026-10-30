"""Contracts for the catch ledger the OMH TUI prints its `✦ OMH` rows from.

Four hooks catch a model mistake and tell the MODEL; `catch_log` records the
moment so the PERSON can see it too. These tests pin the three things that
make that safe to add beside hooks that already ship:

* the record is metadata only, bounded, ordered by one file-wide `seq`, and a
  failure to write it never raises;
* each hook records once per event on its own latch -- the first refusal of a
  repeat streak, the first note on a truncated window, the first attempt of a
  turn, the turn's one unarmed-wait directive -- and never on a path where it
  caught nothing;
* each hook returns exactly what it returned before the record existed.

Every test passes an explicit temporary home, or relies on `_local_package`
redirecting `OMH_HOME`/`HERMES_HOME` to a temporary root for the process, and
one test checks that the redirected root is where a home-less call wrote.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import catch_log  # noqa: E402
from omh.plugin_bundle.omh.catch_log import (  # noqa: E402
    CATCH_LOG_SCHEMA_VERSION,
    MAX_CATCH_SESSIONS,
    MAX_CATCHES_PER_SESSION,
    PLAN_CONTINUED,
    REPEAT_LOOP,
    TRUNCATED_READ,
    UNARMED_WAIT,
    catch_log_path,
    read_catches,
    record_catch,
)
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks  # noqa: E402
from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result  # noqa: E402
from omh.plugin_bundle.omh.hooks.tool_hooks import pre_tool_call  # noqa: E402
from omh.plugin_bundle.omh.remote_wait_nudge import reset_remote_wait_state  # noqa: E402
from omh.plugin_bundle.omh.runtime_reader import HUD_CATCH_LIMIT, read_omh_hud  # noqa: E402
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo  # noqa: E402
from omh.plugin_bundle.omh.tool_bursts import REPEAT_CALL_BLOCK_THRESHOLD  # noqa: E402
from omh.plugin_bundle.omh.truncated_read_recovery import reset_truncated_read_records  # noqa: E402

SESSION = "20260919_140745_db409e"
PATH = "/repo/src/skills/render.py"
TRUNCATED = json.dumps(
    {"content": "1|x\n", "total_lines": 9172, "truncated": True, "next_offset": 1667}
)
REFUSAL = json.dumps({"error": "BLOCKED: already read", "path": PATH, "already_read": 3})
PUSHED = json.dumps({"output": "pushed", "exit_code": 0, "error": None})


class HomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        self.hermes = Path(self._tmp.name) / "hermes"
        self.hermes.mkdir(parents=True)
        catch_log.record_failures.clear()
        self.addCleanup(catch_log.record_failures.clear)

    def catches(self, session: str = SESSION, limit: int = 100) -> list[dict]:
        return read_catches(self.home, session, limit=limit)

    def kinds(self, session: str = SESSION) -> list[str]:
        return [str(entry["kind"]) for entry in self.catches(session)]


class WriterTests(HomeTestCase):
    def test_a_catch_is_recorded_with_its_kind_numbers_and_a_seq(self) -> None:
        record_catch(str(self.home), SESSION, TRUNCATED_READ, offset=1667)
        record_catch(str(self.home), SESSION, UNARMED_WAIT)

        self.assertEqual(
            self.catches(),
            [{"seq": 1, "kind": TRUNCATED_READ, "offset": 1667}, {"seq": 2, "kind": UNARMED_WAIT}],
        )
        stored = json.loads(catch_log_path(str(self.home)).read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], CATCH_LOG_SCHEMA_VERSION)
        self.assertEqual(stored["privacy"], "metadata_only")

    def test_seq_is_one_counter_across_sessions_so_an_evicted_session_never_restarts_it(self) -> None:
        record_catch(str(self.home), "a", UNARMED_WAIT)
        record_catch(str(self.home), "b", UNARMED_WAIT)
        record_catch(str(self.home), "a", UNARMED_WAIT)

        self.assertEqual([entry["seq"] for entry in self.catches("a")], [1, 3])
        self.assertEqual([entry["seq"] for entry in self.catches("b")], [2])

    def test_entries_per_session_and_sessions_per_file_are_capped(self) -> None:
        for _ in range(MAX_CATCHES_PER_SESSION + 3):
            record_catch(str(self.home), SESSION, UNARMED_WAIT)
        seqs = [entry["seq"] for entry in self.catches()]
        self.assertEqual(len(seqs), MAX_CATCHES_PER_SESSION)
        self.assertEqual(seqs[-1], MAX_CATCHES_PER_SESSION + 3)  # the newest are kept

        for index in range(MAX_CATCH_SESSIONS + 2):
            record_catch(str(self.home), f"s{index}", UNARMED_WAIT)
        stored = json.loads(catch_log_path(str(self.home)).read_text(encoding="utf-8"))
        self.assertEqual(len(stored["sessions"]), MAX_CATCH_SESSIONS)
        self.assertIn(f"s{MAX_CATCH_SESSIONS + 1}", stored["sessions"])
        self.assertNotIn(SESSION, stored["sessions"])  # the oldest session went first

    def test_only_the_closed_shape_is_recorded(self) -> None:
        refused = [
            ("", UNARMED_WAIT, {}),  # no session
            (SESSION, "model_text", {}),  # a kind outside the set
            (SESSION, TRUNCATED_READ, {}),  # a missing field
            (SESSION, TRUNCATED_READ, {"offset": 1, "path": 2}),  # an extra field
            (SESSION, TRUNCATED_READ, {"offset": "/etc/passwd"}),  # text where a number goes
            (SESSION, TRUNCATED_READ, {"offset": True}),
            (SESSION, TRUNCATED_READ, {"offset": -1}),
            (SESSION, UNARMED_WAIT, {"command": "git push"}),
        ]
        for session, kind, numbers in refused:
            with self.subTest(kind=kind, numbers=numbers):
                record_catch(str(self.home), session, kind, **numbers)
        self.assertFalse(catch_log_path(str(self.home)).exists())

    def test_a_failed_write_is_counted_and_never_raises(self) -> None:
        with mock.patch.object(catch_log, "_write_delivery_record", side_effect=OSError("disk full")):
            self.assertIsNone(record_catch(str(self.home), SESSION, UNARMED_WAIT))
        self.assertEqual(catch_log.record_failures["OSError"], 1)
        self.assertEqual(self.catches(), [])

    def test_an_unparseable_file_is_replaced_by_the_next_write(self) -> None:
        path = catch_log_path(str(self.home))
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.catches(), [])
        record_catch(str(self.home), SESSION, UNARMED_WAIT)
        self.assertEqual(self.catches(), [{"seq": 1, "kind": UNARMED_WAIT}])

    def test_a_hand_edited_file_hands_the_reader_only_well_formed_rows(self) -> None:
        path = catch_log_path(str(self.home))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"next_seq": 9, "sessions": {SESSION: {"ts": 1, "catches": [
            {"seq": 4, "ts": 1, "kind": UNARMED_WAIT},
            {"seq": 5, "ts": 1, "kind": UNARMED_WAIT, "text": "model said this"},
            {"seq": 6, "ts": 1, "kind": ["list"]},
            {"seq": "7", "ts": 1, "kind": UNARMED_WAIT},
        ]}}}), encoding="utf-8")

        self.assertEqual(self.catches(), [{"seq": 4, "kind": UNARMED_WAIT}])
        record_catch(str(self.home), SESSION, UNARMED_WAIT)
        self.assertEqual([entry["seq"] for entry in self.catches()], [4, 9])

    def test_a_huge_number_or_deep_nesting_in_the_file_never_raises(self) -> None:
        path = catch_log_path(str(self.home))
        path.parent.mkdir(parents=True)
        huge = "1" + "0" * 400  # parses as an int that no float can hold
        path.write_text(
            '{"next_seq": 3, "sessions": {"%s": {"ts": %s, "catches": [{"seq": 2, "ts": %s, "kind": "%s"}]}}}'
            % (SESSION, huge, huge, UNARMED_WAIT),
            encoding="utf-8",
        )
        self.assertEqual(self.catches(), [{"seq": 2, "kind": UNARMED_WAIT}])
        record_catch(str(self.home), SESSION, UNARMED_WAIT)
        self.assertEqual([entry["seq"] for entry in self.catches()], [2, 3])

        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        self.assertEqual(self.catches(), [])
        record_catch(str(self.home), SESSION, UNARMED_WAIT)  # counted as a failure or a fresh file, never raised
        self.assertIn(self.catches(), ([], [{"seq": 1, "kind": UNARMED_WAIT}]))

    def test_a_home_less_call_writes_into_the_redirected_test_home(self) -> None:
        # The hooks at the tool-result seam get no home from the host, so the
        # writer resolves it. Under the suite that must be `_local_package`'s
        # temporary root, never the developer's `~/.omh`.
        redirected = Path(os.environ["OMH_HOME"]).resolve()
        self.assertNotEqual(redirected, (Path.home() / ".omh").resolve())
        self.assertEqual(catch_log_path("").resolve(), redirected / "runtime" / "catches.json")


class RepeatLoopHookTests(HomeTestCase):
    def call(self, session: str = SESSION, pattern: str = "def render") -> dict | None:
        return pre_tool_call(
            tool_name="search_files",
            tool_input={"pattern": pattern, "path": "src"},
            session_id=session,
            omh_home=str(self.home),
        )

    def test_the_first_refusal_of_a_streak_records_once_and_later_refusals_do_not(self) -> None:
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(self.call())
        self.assertEqual(self.catches(), [])  # watching is not a catch

        first = self.call()
        second = self.call()

        self.assertEqual(first["action"], "block")
        self.assertEqual(second["action"], "block")
        self.assertEqual(
            self.catches(),
            [{"seq": 1, "kind": REPEAT_LOOP, "period": 1, "laps": REPEAT_CALL_BLOCK_THRESHOLD}],
        )

    def test_a_new_streak_after_a_different_call_records_again(self) -> None:
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD + 1):
            self.call()
        self.assertIsNone(self.call(pattern="something else"))  # breaks the cycle and clears the count
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.assertIsNone(self.call())
        self.assertEqual(self.call()["action"], "block")
        self.assertEqual(self.kinds(), [REPEAT_LOOP, REPEAT_LOOP])

    def test_the_directive_is_the_same_with_or_without_the_record(self) -> None:
        for _ in range(REPEAT_CALL_BLOCK_THRESHOLD):
            self.call(session="with")
            self.call(session="without")
        recorded = self.call(session="with")
        with mock.patch("omh.plugin_bundle.omh.hooks.tool_hooks.record_catch") as silenced:
            unrecorded = self.call(session="without")
        silenced.assert_called_once()
        self.assertEqual(recorded, unrecorded)
        with mock.patch.object(catch_log, "_write_delivery_record", side_effect=OSError("disk full")):
            self.assertEqual(self.call(session="with"), recorded)


class TruncatedReadHookTests(HomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        reset_truncated_read_records()
        self.addCleanup(reset_truncated_read_records)

    def read(self, result: str, session: str = SESSION, **args: object) -> str | None:
        return transform_tool_result(
            tool_name="read_file",
            args={"path": PATH, **args},
            result=result,
            session_id=session,
            omh_home=str(self.home),
        )

    def test_the_first_note_on_a_truncated_window_records_its_offset_once(self) -> None:
        self.assertIsNone(self.read(TRUNCATED))
        self.assertEqual(self.catches(), [])  # recording the window is not a catch

        first = self.read(REFUSAL)
        second = self.read(REFUSAL)

        self.assertIn("omh_truncated_read", json.loads(str(first)))
        self.assertEqual(first, second)  # the model gets the note every time
        self.assertEqual(self.catches(), [{"seq": 1, "kind": TRUNCATED_READ, "offset": 1667}])

    def test_a_new_truncated_read_of_the_window_is_its_own_catch(self) -> None:
        self.read(TRUNCATED)
        self.read(REFUSAL)
        self.read(TRUNCATED)
        self.read(REFUSAL)
        self.assertEqual(self.kinds(), [TRUNCATED_READ, TRUNCATED_READ])

    def test_a_refusal_of_a_window_never_recorded_truncated_records_nothing(self) -> None:
        self.read(TRUNCATED)
        self.assertIsNone(self.read(REFUSAL, offset=5000))
        self.assertIsNone(self.read(REFUSAL, session="another"))
        self.assertEqual(self.catches(), [])
        self.assertEqual(self.catches("another"), [])

    def test_the_note_is_the_same_when_the_record_fails(self) -> None:
        self.read(TRUNCATED, session="with")
        self.read(TRUNCATED, session="failing")
        recorded = self.read(REFUSAL, session="with")
        with mock.patch.object(catch_log, "_write_delivery_record", side_effect=OSError("disk full")):
            failing = self.read(REFUSAL, session="failing")
        self.assertEqual(json.loads(str(recorded)), json.loads(str(failing)))
        self.assertEqual(catch_log.record_failures["OSError"], 1)


class UnarmedWaitHookTests(HomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        nudge_budget.reset_nudge_budget()
        reset_remote_wait_state()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        self.addCleanup(reset_remote_wait_state)

    def run_command(self, command: str, *, turn: str = "t1", result: str = PUSHED) -> str | None:
        return transform_tool_result(
            tool_name="terminal",
            args={"command": command},
            result=result,
            session_id=SESSION,
            turn_id=turn,
            hermes_home=str(self.hermes),
            omh_home=str(self.home),
        )

    def test_the_directive_records_once_per_turn(self) -> None:
        self.assertIsNotNone(self.run_command("git push origin HEAD"))
        self.assertIsNone(self.run_command("git push origin HEAD"))  # latched for the turn
        self.assertEqual(self.kinds(), [UNARMED_WAIT])

        self.assertIsNotNone(self.run_command("git push origin HEAD", turn="t2"))
        self.assertEqual(self.kinds(), [UNARMED_WAIT, UNARMED_WAIT])

    def test_no_remote_work_and_the_approval_gate_record_nothing(self) -> None:
        self.assertIsNone(self.run_command("git status"))
        held = json.dumps({"output": "", "exit_code": -1, "status": "approval_required", "approval_pending": True})
        carried = self.run_command("git push", result=held)
        self.assertIn("[OMH approval gate]", str(carried))
        self.assertEqual(self.catches(), [])

    def test_the_directive_is_the_same_when_the_record_fails(self) -> None:
        with mock.patch.object(catch_log, "_write_delivery_record", side_effect=OSError("disk full")):
            failing = self.run_command("git push origin HEAD")
        reset_remote_wait_state()
        nudge_budget.reset_nudge_budget()
        self.assertEqual(self.run_command("git push origin HEAD"), failing)


class PlanContinuedHookTests(HomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def write_plan(self, *states: str) -> None:
        items = [{"text": f"item {index}", "state": state} for index, state in enumerate(states)]
        write_todo(self.home, build_todo_record("plan", items, source="test", session_ref=SESSION))

    def fire(self, attempt: int = 0, paths: list[str] | None = None) -> dict | None:
        return verify_hooks.pre_verify(
            session_id=SESSION,
            coding=True,
            attempt=attempt,
            changed_paths=["docs/example.md"] if paths is None else paths,
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
        )

    def test_a_turn_continued_over_an_open_plan_records_its_open_items_once(self) -> None:
        self.write_plan("done", "active", "pending")

        first = self.fire(attempt=0)
        self.fire(attempt=1)

        self.assertEqual(first["action"], "continue")
        self.assertEqual(self.catches(), [{"seq": 1, "kind": PLAN_CONTINUED, "open_items": 2}])

    def test_a_finished_plan_or_a_surface_only_continue_records_nothing(self) -> None:
        self.write_plan("done", "done")
        self.assertIsNone(self.fire())
        surface_only = self.fire(paths=["src/app.tsx"])
        self.assertEqual(surface_only["action"], "continue")
        self.assertEqual(self.catches(), [])

    def test_the_directive_is_the_same_with_or_without_the_record(self) -> None:
        self.write_plan("done", "active")
        recorded = self.fire()
        nudge_budget.reset_nudge_budget()
        with mock.patch.object(catch_log, "_write_delivery_record", side_effect=OSError("disk full")):
            failing = self.fire()
        self.assertEqual(recorded, failing)


class HudCatchesTests(HomeTestCase):
    def hud(self, session: str) -> dict:
        return read_omh_hud(
            self.home, self.hermes, status={"runs": []}, session_ref=session, delegation_graph=False
        )

    def test_the_reading_session_s_newest_catches_ride_the_payload_capped(self) -> None:
        for _ in range(HUD_CATCH_LIMIT + 2):
            record_catch(str(self.home), SESSION, UNARMED_WAIT)
        record_catch(str(self.home), "another", TRUNCATED_READ, offset=9)

        catches = self.hud(SESSION)["catches"]

        self.assertEqual(len(catches), HUD_CATCH_LIMIT)
        self.assertEqual(catches[-1], {"seq": HUD_CATCH_LIMIT + 2, "kind": UNARMED_WAIT})
        self.assertEqual(self.hud("another")["catches"], [{"seq": HUD_CATCH_LIMIT + 3, "kind": TRUNCATED_READ, "offset": 9}])

    def test_no_reading_session_or_no_file_is_an_empty_list(self) -> None:
        self.assertEqual(self.hud(SESSION)["catches"], [])
        record_catch(str(self.home), SESSION, UNARMED_WAIT)
        self.assertEqual(self.hud("someone-else")["catches"], [])


if __name__ == "__main__":
    unittest.main()
