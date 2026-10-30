"""The trigger that decides whether the round-end consistency check is worth its extra round.

`pre_tool_call` keeps the pre-edit text of each Python file a turn edits; at the turn's first stop
`pre_verify` asks `round_end_trigger` whether a copy or a reference of the edit exists outside the
edited files. No signal skips the check; anything undecidable fires it, as it fired before.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import consistency_trigger as ct  # noqa: E402
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks  # noqa: E402

SESSION = "trigger-session"
INVOICE = (
    '"""Invoice totals."""\nfrom decimal import Decimal, ROUND_HALF_EVEN\n\nCENT = Decimal("0.01")\n\n\n'
    "def _to_cents(amount: str) -> Decimal:\n    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_EVEN)\n\n\n"
    "def invoice_total(amounts: list[str]) -> Decimal:\n"
    '    return sum((_to_cents(amount) for amount in amounts), Decimal("0.00"))\n'
)
RECEIPT = (
    '"""Receipt totals."""\nimport decimal\n\n\n'
    "def _to_cents(amount: str) -> decimal.Decimal:\n"
    '    return decimal.Decimal(amount).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_EVEN)\n'
)
UNITS = (
    '"""Units."""\nimport re\n\n_DURATION_SECONDS = {"s": 60, "m": 60, "h": 3600}\n\n\n'
    "def parse_duration(text: str) -> int:\n    return int(text[:-1]) * _DURATION_SECONDS[text[-1]]\n"
)
TO_CENTS = '"""Money units."""\n\n\ndef to_cents(amount: float) -> int:\n    return int(round(amount * 100))\n'
CART = "from benchapp.units import to_cents\n\n\ndef cart_total(prices: list[float]) -> int:\n    return sum(map(to_cents, prices))\n"


class _Workspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        patcher = mock.patch.object(ct, "_workspace_root", lambda task_id: str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        ct.reset()
        self.addCleanup(ct.reset)
        self.turn = "t1"

    def write(self, relative: str, text: str) -> str:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return relative

    def edit(self, relative: str, old: str, new: str, session: str = SESSION) -> str:
        """Snapshot through the `pre_tool_call` entry point, then land a replace-mode `patch`."""
        args = {"path": relative, "old_string": old, "new_string": new}
        ct.snapshot_before_edit({"tool_name": "patch", "session_id": session, "turn_id": self.turn}, args)
        path = self.root / relative
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return str(path)

    def create(self, relative: str, text: str) -> str:
        ct.snapshot_before_edit({"tool_name": "write_file", "session_id": SESSION, "turn_id": self.turn},
                                {"path": relative, "content": text})
        return str(self.root / self.write(relative, text))

    def verdict(self, *paths: str) -> ct.Verdict:
        return ct.round_end_trigger(SESSION, tuple(paths))


class CopySignalTest(_Workspace):
    def test_a_second_copy_of_the_edited_helper_fires(self):
        self.write("src/app/invoice.py", INVOICE)
        self.write("src/app/receipt.py", RECEIPT)
        changed = self.edit("src/app/invoice.py", "ROUND_HALF_EVEN)\n\n\n", "ROUND_HALF_UP)\n\n\n")
        verdict = self.verdict(changed)
        self.assertEqual((verdict.fire, verdict.reason), (True, "copy"))
        self.assertIn("_to_cents in src/app/receipt.py", verdict.detail)

    def test_both_copies_already_edited_skips(self):
        self.write("src/app/invoice.py", INVOICE)
        self.write("src/app/receipt.py", RECEIPT)
        first = self.edit("src/app/invoice.py", "ROUND_HALF_EVEN)\n\n\n", "ROUND_HALF_UP)\n\n\n")
        second = self.edit("src/app/receipt.py", "rounding=decimal.ROUND_HALF_EVEN", "rounding=decimal.ROUND_HALF_UP")
        self.assertEqual(self.verdict(first, second), ct.Verdict(False, "no_copy_or_reference", "0"))

    def test_an_edit_with_no_copy_or_reference_elsewhere_skips(self):
        self.write("src/app/units.py", UNITS)
        self.write("src/app/other.py", "def unrelated() -> int:\n    return 1\n")
        changed = self.edit("src/app/units.py", '{"s": 60,', '{"s": 1,')
        test = self.create("tests/test_units.py", "from app.units import parse_duration\n\n\n"
                           "def test_seconds():\n    assert parse_duration('90s') == 90\n")
        verdict = self.verdict(changed, test)
        self.assertFalse(verdict.fire)
        self.assertEqual(verdict.reason, "no_copy_or_reference")

    def test_a_common_or_dunder_name_defined_elsewhere_is_not_a_copy(self):
        self.write("src/app/a.py", '__all__ = ["main"]\n\n\ndef main() -> int:\n    return 1\n')
        self.write("src/app/b.py", '__all__ = ["main"]\n\n\ndef main() -> int:\n    return 2\n')
        changed = self.edit("src/app/a.py", "return 1", "return 3")
        self.assertFalse(self.verdict(changed).fire)

    def test_a_replaced_line_that_also_appears_elsewhere_fires(self):
        line = "    total = sum(item.price * item.quantity for item in basket)\n"
        self.write("src/app/a.py", f"def cost(basket):\n{line}    return total\n")
        self.write("src/app/b.py", f"def cost_with_tax(basket, rate):\n{line}    return total * rate\n")
        changed = self.edit("src/app/a.py", "item.price * item.quantity", "item.unit_price * item.quantity")
        verdict = self.verdict(changed)
        self.assertEqual((verdict.fire, verdict.reason, verdict.detail), (True, "line_copy", "src/app/b.py"))


class ReferenceSignalTest(_Workspace):
    def test_a_renamed_definition_still_named_in_an_unedited_file_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        changed = self.edit("src/app/units.py", "def to_cents(", "def amount_to_cents(")
        verdict = self.verdict(changed)
        self.assertEqual((verdict.fire, verdict.reason), (True, "renamed"))
        self.assertIn("to_cents in src/app/cart.py", verdict.detail)

    def test_a_rename_carried_into_every_reference_skips(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        first = self.edit("src/app/units.py", "def to_cents(", "def amount_to_cents(")
        second = self.edit("src/app/cart.py", "import to_cents", "import amount_to_cents")
        second = self.edit("src/app/cart.py", "map(to_cents,", "map(amount_to_cents,")
        self.assertFalse(self.verdict(first, second).fire)

    def test_a_changed_signature_with_a_caller_elsewhere_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        changed = self.edit("src/app/units.py", "(amount: float)", "(amount: float, *, places: int)")
        self.assertEqual(self.verdict(changed)[:2], (True, "signature"))

    def test_a_body_only_change_with_callers_elsewhere_skips(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        changed = self.edit("src/app/units.py", "    return int(", "    if amount < 0:\n        raise ValueError('negative amount')\n    return int(")
        self.assertEqual(self.verdict(changed)[:2], (False, "no_copy_or_reference"))


class FailOpenTest(_Workspace):
    def test_no_snapshot_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        self.assertEqual(self.verdict(str(self.root / "src/app/units.py"))[:2], (True, "no_snapshot"))

    def test_a_changed_file_without_its_own_snapshot_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        changed = self.edit("src/app/units.py", "return int(", "return int(")
        self.assertEqual(self.verdict(changed, str(self.root / "src/app/cart.py"))[:2], (True, "no_snapshot"))

    def test_a_non_python_source_file_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        changed = self.edit("src/app/units.py", "100", "1000")
        self.assertEqual(self.verdict(changed, "web/app.ts")[:2], (True, "not_python"))

    def test_a_file_that_no_longer_parses_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        changed = self.edit("src/app/units.py", "return int(", "return int((")
        self.assertEqual(self.verdict(changed)[:2], (True, "parse_error"))

    def test_the_scan_budget_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        self.write("src/app/cart.py", CART)
        changed = self.edit("src/app/units.py", "100", "1000")
        with mock.patch.object(ct, "MAX_SCAN_FILES", 0):
            self.assertEqual(self.verdict(changed)[:2], (True, "scan_budget"))
        self.edit("src/app/units.py", "1000", "100")
        with mock.patch.object(ct, "SCAN_SECONDS", -1.0):
            self.assertEqual(self.verdict(changed)[:2], (True, "scan_budget"))

    def test_an_error_inside_the_decision_fires(self):
        self.write("src/app/units.py", TO_CENTS)
        changed = self.edit("src/app/units.py", "100", "1000")
        with mock.patch.object(ct, "_collect", side_effect=RuntimeError("boom")):
            self.assertEqual(self.verdict(changed), ct.Verdict(True, "error", "RuntimeError"))


class SnapshotStateTest(_Workspace):
    def test_the_first_pre_edit_text_of_the_turn_is_kept(self):
        self.write("src/app/units.py", TO_CENTS)
        self.edit("src/app/units.py", "100", "1000")
        self.edit("src/app/units.py", "1000", "10")
        path = os.path.realpath(self.root / "src/app/units.py")
        self.assertEqual(ct._sessions[SESSION]["before"][path], TO_CENTS)

    def test_a_new_turn_starts_over_and_a_verdict_consumes_the_turn(self):
        self.write("src/app/units.py", TO_CENTS)
        self.edit("src/app/units.py", "100", "1000")
        self.turn = "t2"
        changed = self.edit("src/app/units.py", "1000", "10")
        path = os.path.realpath(self.root / "src/app/units.py")
        self.assertIn("1000", ct._sessions[SESSION]["before"][path])
        self.verdict(changed)
        self.assertNotIn(SESSION, ct._sessions)

    def test_a_new_file_snapshots_as_empty_and_non_python_or_other_tools_are_ignored(self):
        self.create("src/app/new.py", "def fresh() -> int:\n    return 1\n")
        ct.snapshot_before_edit({"tool_name": "write_file", "session_id": SESSION, "turn_id": "t1"},
                                {"path": "README.md", "content": "x"})
        ct.snapshot_before_edit({"tool_name": "read_file", "session_id": SESSION, "turn_id": "t1"},
                                {"path": "src/app/other.py"})
        self.assertEqual(ct._sessions[SESSION]["before"], {os.path.realpath(self.root / "src/app/new.py"): ""})

    def test_v4a_patch_headers_are_snapshotted(self):
        self.write("src/app/units.py", TO_CENTS)
        body = "*** Begin Patch\n*** Update File: src/app/units.py\n@@\n-x\n+y\n*** Add File: src/app/added.py\n+z\n*** End Patch\n"
        ct.snapshot_before_edit({"tool_name": "patch", "session_id": SESSION, "turn_id": "t1"}, {"mode": "patch", "patch": body})
        self.assertEqual(sorted(ct._sessions[SESSION]["before"].values()), ["", TO_CENTS])

    def test_snapshots_are_bounded_per_session_and_across_sessions(self):
        for index in range(ct.MAX_SNAPSHOT_FILES + 3):
            self.create(f"src/app/m{index}.py", "")
        self.assertEqual(len(ct._sessions[SESSION]["before"]), ct.MAX_SNAPSHOT_FILES)
        for index in range(ct._MAX_SESSIONS + 2):
            ct.snapshot_before_edit({"tool_name": "write_file", "session_id": f"s{index}", "turn_id": "t1"},
                                    {"path": "src/app/m0.py"})
        self.assertEqual(len(ct._sessions), ct._MAX_SESSIONS)

    def test_a_malformed_call_never_raises(self):
        ct.snapshot_before_edit({"tool_name": "patch", "session_id": SESSION}, None)
        ct.snapshot_before_edit({"tool_name": "patch", "session_id": SESSION}, {"path": 7})
        with mock.patch.object(ct, "_workspace_root", side_effect=RuntimeError("no root")):
            ct.snapshot_before_edit({"tool_name": "patch", "session_id": SESSION}, {"path": "a.py"})
        self.assertNotIn(SESSION, ct._sessions)


class PreVerifyGateTest(_Workspace):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "omh"
        self.hermes = Path(tmp.name) / "hermes"
        self.hermes.mkdir(parents=True)
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        verify_hooks.reset_stashed_answers()
        self.addCleanup(verify_hooks.reset_stashed_answers)

    def fire(self, *paths: str):
        return verify_hooks.pre_verify(session_id=SESSION, coding=True, attempt=0, final_response='{"changed_paths": []}',
                                       changed_paths=list(paths), omh_home=str(self.home), hermes_home=str(self.hermes))

    def test_alone_and_with_nothing_to_find_the_turn_ends(self):
        self.write("src/app/units.py", UNITS)
        self.assertIsNone(self.fire(self.edit("src/app/units.py", '{"s": 60,', '{"s": 1,')))
        self.assertNotIn(SESSION, ct._sessions)

    def test_alone_with_a_copy_the_check_fires_unchanged(self):
        self.write("src/app/invoice.py", INVOICE)
        self.write("src/app/receipt.py", RECEIPT)
        result = self.fire(self.edit("src/app/invoice.py", "ROUND_HALF_EVEN)\n\n\n", "ROUND_HALF_UP)\n\n\n"))
        self.assertEqual(result["message"], f"{verify_hooks.ROUND_END_CHECKLIST}\n{verify_hooks.ROUND_END_CLOSING}")

    def test_beside_another_nudge_the_check_rides_along_without_a_verdict(self):
        self.write("src/app/units.py", UNITS)
        changed = self.edit("src/app/units.py", '{"s": 60,', '{"s": 1,')
        with mock.patch.object(verify_hooks, "round_end_trigger") as trigger:
            result = self.fire(changed, "pyproject.toml")
        trigger.assert_not_called()
        self.assertIn(verify_hooks.ROUND_END_CHECKLIST_BESIDE_A_RECHECK, result["message"])
        self.assertNotIn(SESSION, ct._sessions)

    def test_a_later_attempt_asks_nothing(self):
        self.write("src/app/units.py", UNITS)
        changed = self.edit("src/app/units.py", '{"s": 60,', '{"s": 1,')
        with mock.patch.object(verify_hooks, "round_end_trigger") as trigger:
            verify_hooks.pre_verify(session_id=SESSION, coding=True, attempt=1, changed_paths=[changed],
                                    omh_home=str(self.home), hermes_home=str(self.hermes))
        trigger.assert_not_called()

    def test_pre_tool_call_snapshots_before_any_gate(self):
        from omh.plugin_bundle.omh.hooks import tool_hooks

        self.write("src/app/units.py", UNITS)
        with mock.patch.object(tool_hooks.runtime_paths, "plugin_home", side_effect=RuntimeError("no home")):
            tool_hooks.pre_tool_call(tool_name="patch", args={"path": "src/app/units.py", "old_string": "a", "new_string": "b"},
                                     session_id=SESSION, turn_id="t1")
        self.assertEqual(list(ct._sessions[SESSION]["before"].values()), [UNITS])


if __name__ == "__main__":
    unittest.main()
