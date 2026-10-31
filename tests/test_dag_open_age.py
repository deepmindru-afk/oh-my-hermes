"""How long each open dag-ulw node has been open, the stuck warning, and the profile marks that move both.

A node in ``handed_out`` / ``running`` / ``returned`` used to read the same
after two minutes and after eight hours. Each test drives the tool the way
Hermes calls it (`test_dag_tool.call`) and reads what the parent reads: the
rows, ``say``, ``stuck`` and the status line.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from _delegation_state import PARENT, homes, stop  # noqa: E402
from omh.plugin_bundle.omh import dag_run  # noqa: E402
from omh.plugin_bundle.omh.tools import dag_tool  # noqa: E402
from test_dag_tool import call, child_started, node, slots_table  # noqa: E402

HOUR = 3600.0


def rows(result: dict) -> dict[str, dict]:
    return {row["id"]: row for row in result["nodes"]}


def settings(home, **values: object) -> None:
    lines = "".join(f"        {name}: {value}\n" for name, value in values.items())
    (home.hermes / "config.yaml").write_text(
        f"plugins:\n  entries:\n    omh:\n      settings:\n{lines}", encoding="utf-8")


def at(moment: float, **arguments: object) -> dict:
    with patch.object(dag_tool.time, "time", return_value=moment):
        return call(**arguments)


class OpenForTests(unittest.TestCase):
    """Mutation: drop `open_for_s` from `node_rows`, or count it from the last re-offer instead of the first."""

    def test_an_open_node_says_how_long_it_has_been_open_and_others_do_not(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api"), node("docs", depends_on=("api",))])
            child_started(home, "feat", "api", now=moment + 5)
            status = at(moment + 125, action="status", run="feat")
        self.assertEqual(rows(status)["api"]["state"], "running")
        self.assertEqual(rows(status)["api"]["open_for_s"], 125)
        self.assertNotIn("open_for_s", rows(status)["docs"])
        # Below the mark nothing else changes: no warning, no stuck list, no clause on the status line.
        self.assertNotIn("stuck", status)
        self.assertNotIn("say", status)
        self.assertEqual(status["status_line"], "DAG feat · 0/2 done · 1 running")

    def test_a_returned_node_counts_until_it_is_settled(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api")])
            child_started(home, "feat", "api", now=moment + 5)
            stop(home, "child-api", now=moment + 60)
            status = at(moment + 600, action="status", run="feat")
        self.assertEqual((rows(status)["api"]["state"], rows(status)["api"]["open_for_s"]), ("returned", 600))

    def test_offering_a_node_again_does_not_reset_its_age(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api")])
            again = moment + dag_run.HANDOUT_STALE_SECONDS + 1
            reoffered = at(again, action="ready", run="feat")
            status = at(again + 60, action="status", run="feat")
            attempt = dag_run.read_run(dag_run.record_path(home.omh, PARENT, "feat"))["nodes"][0]["attempts"][-1]
        self.assertEqual([c["nodes"] for c in reoffered["calls"]], [["api"]])
        self.assertEqual((attempt["opened_at"], attempt["handed_out_at"]), (moment, again))
        self.assertEqual(rows(status)["api"]["open_for_s"], int(again + 60 - moment))


class StuckWarningTests(unittest.TestCase):
    """Mutation: drop the stuck sentence from `_result`, let it replace another say, or warn on a lost node."""

    def test_a_running_node_past_the_mark_is_named_with_its_age_and_what_to_check(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat",
               nodes=[node("api", label="Build the API"), node("docs", depends_on=("api",))])
            child_started(home, "feat", "api", now=moment + 5)
            below = at(moment + HOUR - 1, action="status", run="feat")
            past = at(moment + 9 * HOUR + 12 * 60, action="status", run="feat")
            ready = at(moment + 9 * HOUR + 12 * 60, action="ready", run="feat")
        self.assertNotIn("stuck", below)
        self.assertEqual(past["stuck"], [{"id": "api", "open_for_s": int(9 * HOUR + 12 * 60)}])
        self.assertEqual(past["say"], (
            "Node api (9h 12m) has run past the 1h stuck mark: run delegate_task action=list, and settle it failed "
            "and repair when no child of it is live there."))
        self.assertEqual(past["status_line"], "DAG feat · 0/2 done · 1 running · 1 open past 1h (Build the API 9h 12m)")
        # `ready` says it too, and still hands nothing out: the child is bound, so the node is never offered twice.
        self.assertEqual((ready["say"], ready["calls"]), (past["say"], []))

    def test_each_open_state_gets_the_sentence_for_what_to_check(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("a"), node("b"), node("c")])
            child_started(home, "feat", "b", now=moment + 5)
            child_started(home, "feat", "c", now=moment + 5)
            stop(home, "child-c", now=moment + 60)
            # Re-offered on every ready since, and no child ever took it: its age still runs from the first offer.
            at(moment + 2 * HOUR, action="ready", run="feat")
            past = at(moment + 3 * HOUR, action="status", run="feat")
        self.assertEqual({row["id"]: row["state"] for row in past["nodes"]},
                         {"a": "handed_out", "b": "running", "c": "returned"})
        self.assertEqual([item["id"] for item in past["stuck"]], ["a", "b", "c"])
        self.assertEqual(past["say"], (
            "Node a (3h) has been out past the 1h stuck mark with no child taking it: the delegate_task call did not "
            "start one (a refused route does that). Dispatch the call ready returns for it, or settle it failed if "
            "the route cannot run. "
            "Node b (3h) has run past the 1h stuck mark: run delegate_task action=list, and settle it failed and "
            "repair when no child of it is live there. "
            "Node c (3h) came back but is still not settled past the 1h stuck mark: read its handoff and settle it."))
        self.assertIn("3 open past 1h (Node a 3h, +2)", past["status_line"])

    def test_the_warning_is_added_to_what_else_the_reply_says(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api"), node("ui")])
            child_started(home, "feat", "api", now=moment + 5)
            child_started(home, "feat", "ui", now=moment + 5)
            stop(home, "child-ui", "failed", now=moment + 60)
            settled = at(moment + 2 * HOUR, action="settle", run="feat", node="ui", outcome="failed",
                         provider_error=True)
        self.assertTrue(settled["say"].startswith("The child hit a provider error"), settled["say"])
        self.assertIn("Node api (2h) has run past the 1h stuck mark", settled["say"])

    def test_a_node_lost_to_a_restart_or_given_to_the_parent_is_not_called_stuck(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api"), node("ui")])
            record_path = dag_run.record_path(home.omh, PARENT, "feat")
            record = dag_run.read_run(record_path)
            record["nodes"][1]["attempts"][-1]["lane"] = "fanout"
            dag_run.write_run(record_path, record)
            record = dag_run.read_run(record_path)
            derived = dag_run.derive(record, [], moment + 5 * HOUR)
            derived["api"]["lost"] = True
            found = dag_run.stuck(record, derived, moment + 5 * HOUR)
            cancelled = at(moment + 5 * HOUR, action="cancel", run="feat")
        self.assertEqual(found, [])
        self.assertNotIn("stuck", cancelled)
        self.assertNotIn("stuck mark", cancelled["say"])


class LimitSettingTests(unittest.TestCase):
    """The profile's marks, with today's constants as the default.

    Mutation: ignore a setting, let a bad value through, or let `0` mean anything but "no warning".
    """

    def test_the_defaults_are_todays_constants(self) -> None:
        with homes() as home:
            self.assertEqual(dag_tool.dag_limits(home.hermes), (dag_run.DEFAULT_LIMITS, ()))
            self.assertEqual(dag_run.DEFAULT_LIMITS, dag_run.Limits(600.0, 3600.0, 3))

    def test_the_stuck_mark_moves_and_zero_turns_the_warning_off(self) -> None:
        moment = time.time()
        for mark, warned in ((600, True), (0, False), (7200, False)):
            with self.subTest(mark=mark), homes() as home:
                slots_table(home)
                settings(home, dag_stuck_after_s=mark)
                at(moment, action="define", run="feat", nodes=[node("api")])
                child_started(home, "feat", "api", now=moment + 5)
                status = at(moment + 1800, action="status", run="feat")
                self.assertEqual("stuck" in status, warned, status)
                self.assertEqual(rows(status)["api"]["open_for_s"], 1800)
                self.assertEqual(status["limits"]["stuck_after_s"], float(mark))
                if warned:
                    self.assertIn("past the 10m stuck mark", status["say"])

    def test_the_reoffer_wait_and_the_restart_bound_move(self) -> None:
        moment = time.time()
        with homes() as home:
            slots_table(home)
            settings(home, dag_stale_after_s=120, dag_max_lost_reoffers=1)
            at(moment, action="define", run="feat", nodes=[node("api")])
            early = at(moment + 121, action="ready", run="feat")
            self.assertEqual(dag_tool.dag_limits(home.hermes)[0].max_lost_reoffers, 1)
        self.assertEqual([c["nodes"] for c in early["calls"]], [["api"]])
        with homes() as home:
            slots_table(home)
            at(moment, action="define", run="feat", nodes=[node("api")])
            default = at(moment + 121, action="ready", run="feat")
        self.assertEqual(default["calls"], [])

    def test_a_bad_or_out_of_range_value_is_bounded_or_named_and_never_breaks_the_run(self) -> None:
        with homes() as home:
            settings(home, dag_stale_after_s=1, dag_stuck_after_s='"soon"', dag_max_lost_reoffers=500)
            limits, ignored = dag_tool.dag_limits(home.hermes)
            self.assertEqual(limits, dag_run.Limits(dag_run.MIN_MARK_SECONDS, 3600.0, dag_run.MAX_LOST_REOFFERS_LIMIT))
            self.assertEqual(ignored, ("dag_stuck_after_s",))
            settings(home, dag_stuck_after_s=-5)
            self.assertEqual(dag_tool.dag_limits(home.hermes), (dag_run.DEFAULT_LIMITS, ("dag_stuck_after_s",)))
            slots_table(home)
            status = call(action="define", run="feat", nodes=[node("api")])
            self.assertEqual(status["status"], "ok", status)
            self.assertEqual(status["limits"]["ignored"], ["dag_stuck_after_s"])
            (home.hermes / "config.yaml").write_text("plugins:\n\tentries: {}\n", encoding="utf-8")
            self.assertEqual(dag_tool.dag_limits(home.hermes),
                             (dag_run.DEFAULT_LIMITS, dag_tool.DAG_LIMIT_SETTINGS))


if __name__ == "__main__":
    unittest.main()
