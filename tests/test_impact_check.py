"""Tests for the opt-in cross-file impact check and the `lsp_diagnostics` tool."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from omh.plugin_bundle.omh import impact_check, lsp_bridge, runtime_paths  # noqa: E402
from omh.plugin_bundle.omh.hooks import verify_hooks  # noqa: E402
from omh.plugin_bundle.omh.impact_check import (  # noqa: E402
    MAX_NOTE_LINES,
    baseline_before_edit,
    edit_targets,
    enabled,
    find_importers,
    format_note,
    module_names,
    round_end_note,
)
from omh.plugin_bundle.omh.lsp_bridge import BridgeError  # noqa: E402
from omh.plugin_bundle.omh.tools import lsp_nav_tool  # noqa: E402
from omh.plugin_bundle.omh.tools.lsp_nav_tool import OMH_LSP_DIAGNOSTICS_SCHEMA, lsp_diagnostics_handler  # noqa: E402


def _diag(line, message, code="reportCallIssue", severity=1):
    return {"range": {"start": {"line": line, "character": 4}, "end": {"line": line, "character": 9}},
            "message": message, "code": code, "severity": severity}


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class EditTargetTests(unittest.TestCase):
    def test_replace_write_and_v4a(self):
        self.assertEqual(edit_targets("patch", {"path": "src/a.py", "old_string": "x"}), ["src/a.py"])
        self.assertEqual(edit_targets("write_file", {"path": "b.py", "content": ""}), ["b.py"])
        body = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Update File: notes.md\n*** End Patch\n"
        self.assertEqual(edit_targets("patch", {"mode": "patch", "patch": body}), ["src/a.py"])

    def test_other_tools_and_files_ignored(self):
        self.assertEqual(edit_targets("read_file", {"path": "a.py"}), [])
        self.assertEqual(edit_targets("patch", {"path": "README.md"}), [])
        self.assertEqual(edit_targets("patch", "not a dict"), [])


class ImporterScanTests(_Root):
    def setUp(self):
        super().setUp()
        self.target = self.write("src/shop/pricing.py", "def compute_fee(a: int, r: int) -> int:\n    return a * r\n")
        self.write("src/shop/__init__.py", "from .pricing import compute_fee as fee\n")
        self.direct = self.write("src/shop/billing.py", "from shop.pricing import compute_fee\n")
        self.alias = self.write("src/shop/invoices.py", "from shop.pricing import compute_fee as f\n")
        self.module = self.write("src/shop/report.py", "from shop import pricing\n")
        self.relative = self.write("src/shop/rel.py", "from .pricing import compute_fee\n")
        self.dot = self.write("src/shop/dot.py", "from . import pricing\n")
        self.plain = self.write("src/tool.py", "import shop.pricing\n")
        self.reexport = self.write("src/cli.py", "from shop import fee\n")
        self.write("src/other.py", "from shop import billing\nimport json\n")
        self.write("src/broken.py", "from shop.pricing import (\n")
        self.write(".venv/lib/pricing_user.py", "from shop.pricing import compute_fee\n")

    def test_module_names(self):
        self.assertEqual(module_names(self.target, self.root), {"src.shop.pricing", "shop.pricing", "pricing"})
        self.assertEqual(module_names(os.path.join(self.root, "src/shop/__init__.py"), self.root), {"src.shop", "shop"})

    def test_finds_every_import_form_and_the_reexport(self):
        found = set(find_importers(self.root, self.target))
        package = os.path.join(self.root, "src/shop/__init__.py")
        self.assertEqual(found, {package, self.direct, self.alias, self.module, self.relative, self.dot, self.plain,
                                 self.reexport})

    def test_cap(self):
        self.assertEqual(len(find_importers(self.root, self.target, cap=3)), 3)


class EnabledTests(unittest.TestCase):
    def setUp(self):
        old = os.environ.pop("OMH_IMPACT_CHECK", None)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, "OMH_IMPACT_CHECK", old)
        self.addCleanup(os.environ.pop, "OMH_IMPACT_CHECK", None)
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(runtime_paths, "default_hermes_home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_by_default_env_and_setting(self):
        self.assertFalse(enabled())
        os.environ["OMH_IMPACT_CHECK"] = "1"
        self.assertTrue(enabled())
        os.environ["OMH_IMPACT_CHECK"] = "0"
        self.assertFalse(enabled())
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    omh:\n      settings:\n        impact_check: true\n", encoding="utf-8")
        self.assertTrue(enabled())
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    omh:\n      settings:\n        impact_check: off\n", encoding="utf-8")
        self.assertFalse(enabled())


class _FakeDiagnostics:
    """Stands in for lsp_bridge.diagnostics: answers from a per-phase table, records each call."""

    def __init__(self):
        self.answers, self.calls = {}, []

    def __call__(self, paths, sync=(), timeout=0):
        self.calls.append((list(paths), list(sync), timeout))
        return {path: self.answers.get(path, []) for path in paths}


class RoundTripTests(_Root):
    def setUp(self):
        super().setUp()
        impact_check._sessions.clear()
        self.addCleanup(impact_check._sessions.clear)
        self.target = self.write("src/shop/pricing.py", "def compute_fee(a: int, r: int) -> int:\n    return a * r\n")
        self.billing = self.write("src/shop/billing.py", "from shop.pricing import compute_fee\n\nX: int = 'legacy'\n")
        self.report = self.write("src/shop/report.py", "from shop import pricing\n")
        self.fake = _FakeDiagnostics()
        for target, name, value in (
            (impact_check, "enabled", lambda: True),
            (impact_check, "_workspace_root", lambda task_id: self.root),
            (impact_check, "_lsp_tools_enabled", lambda: True),
            (lsp_bridge, "in_git_worktree", lambda path: True),
            (lsp_bridge, "diagnostics", self.fake),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def edit(self, path="src/shop/pricing.py", turn="t1", session="s1", tool="patch"):
        baseline_before_edit({"tool_name": tool, "session_id": session, "turn_id": turn, "task_id": "x"},
                             {"path": path, "old_string": "a", "new_string": "b"})

    def test_new_error_is_named_and_preexisting_is_not(self):
        legacy = _diag(2, 'Type "str" is not assignable to declared type "int"', code="reportAssignmentType")
        self.fake.answers = {self.billing: [legacy]}
        self.edit()
        self.assertEqual(self.fake.calls[0][1], [self.target])
        self.assertEqual(sorted(self.fake.calls[0][0]), sorted([self.billing, self.report]))
        # After the edit the legacy error moved a line and a new one appeared.
        moved = dict(legacy, range={"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 1}})
        self.fake.answers = {self.billing: [moved, _diag(3, 'Argument missing for parameter "minimum"\n  more')]}
        note = round_end_note("s1", [self.target])
        self.assertEqual(note.splitlines()[1], 'src/shop/billing.py:4: Argument missing for parameter "minimum"')
        self.assertNotIn("legacy", note)
        self.assertIn("`lsp_diagnostics`", note)
        self.assertLessEqual(len(note.splitlines()), MAX_NOTE_LINES)
        self.assertEqual(round_end_note("s1", [self.target]), "")  # consumed

    def test_silent_when_nothing_new_or_no_verdict(self):
        self.edit()
        self.assertEqual(round_end_note("s1", [self.target]), "")
        self.edit(turn="t2")
        self.fake.answers = {self.billing: None}
        self.assertEqual(round_end_note("s1", [self.target]), "")

    def test_warnings_are_not_errors(self):
        self.edit()
        self.fake.answers = {self.report: [_diag(0, "unused import", code="reportUnusedImport", severity=2)]}
        self.assertEqual(round_end_note("s1", [self.target]), "")

    def test_one_baseline_per_file_per_turn_and_new_turn_resets(self):
        self.edit()
        self.edit()
        self.assertEqual(len(self.fake.calls), 1)
        self.edit(turn="t2")
        self.assertEqual(len(self.fake.calls), 2)

    def test_skips_non_python_unknown_tools_and_disabled(self):
        self.edit(path="notes.md")
        self.edit(tool="terminal")
        with mock.patch.object(impact_check, "enabled", lambda: False):
            self.edit()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(round_end_note("s1", [self.target]), "")

    def test_bridge_failure_is_silent(self):
        def boom(*args, **kwargs):
            raise BridgeError("timeout", "slow")

        with mock.patch.object(lsp_bridge, "diagnostics", boom):
            self.edit()
        self.assertEqual(round_end_note("s1", [self.target]), "")
        self.edit(turn="t2")
        with mock.patch.object(lsp_bridge, "diagnostics", boom):
            self.assertEqual(round_end_note("s1", [self.target]), "")

    def test_pre_verify_continues_with_the_note_even_outside_coding(self):
        self.edit()
        self.fake.answers = {self.report: [_diag(4, 'Cannot access attribute "get" for class "Quote"')]}
        with mock.patch.object(verify_hooks, "observe_plugin_hook_call", lambda *a, **k: None):
            out = verify_hooks.pre_verify(session_id="s1", coding=False, attempt=0, changed_paths=[self.target])
            self.assertEqual(out["action"], "continue")
            self.assertIn("src/shop/report.py:5:", out["message"])
            self.edit(turn="t2")
            self.assertIsNone(verify_hooks.pre_verify(session_id="s1", coding=False, attempt=1, changed_paths=[self.target]))


class FormatTests(unittest.TestCase):
    def test_overflow_keeps_the_cap(self):
        rows = [(f"src/m{index:02d}.py", index + 1, "boom") for index in range(40)]
        note = format_note(rows, recheck_tool=False)
        lines = note.splitlines()
        self.assertEqual(len(lines), MAX_NOTE_LINES)
        self.assertEqual(lines[-2], "... and 28 more")
        self.assertIn("re-run the checks", lines[-1])
        self.assertEqual(format_note([], recheck_tool=True), "")


class DiagnosticsToolTests(_Root):
    def setUp(self):
        super().setUp()
        self.a = self.write("src/a.py", "x: int = 'a'\n")
        self.b = self.write("src/b.py", "y = 1\n")
        self.write("src/notes.txt", "")
        self.write(".venv/c.py", "")
        patcher = mock.patch.object(lsp_nav_tool, "_workspace_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("available", lambda: (object(), None)), ("in_git_worktree", lambda path: True)):
            patcher = mock.patch.object(lsp_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, answer, **args):
        with mock.patch.object(lsp_bridge, "diagnostics", answer):
            return json.loads(lsp_diagnostics_handler(args, task_id="t"))

    def test_glob_counts_and_rows(self):
        seen = {}

        def answer(paths, sync=(), timeout=0):
            seen["paths"] = paths
            return {self.a: [_diag(0, "bad\nmore", code="reportAssignmentType"), _diag(0, "w", severity=2)], self.b: []}

        out = self.call(answer, path="src/*.py")
        self.assertEqual(seen["paths"], [self.a, self.b])
        self.assertEqual((out["ok"], out["errors"], out["warnings"]), (True, 1, 1))
        self.assertEqual(out["files"], [{"path": "src/a.py", "status": "checked", "error": 1, "warning": 1},
                                        {"path": "src/b.py", "status": "checked", "error": 0, "warning": 0}])
        self.assertEqual(out["diagnostics"][0], {"path": "src/a.py", "line": 1, "severity": "error", "message": "bad",
                                                 "code": "reportAssignmentType"})

    def test_no_verdict_and_unsupported_are_not_clean(self):
        out = self.call(lambda paths, sync=(), timeout=0: {self.a: None}, path="src/**/*.py")
        self.assertEqual([row["status"] for row in out["files"]], ["no_verdict", "unsupported"])
        self.assertEqual(out["errors"], 0)

    def test_errors(self):
        self.assertEqual(self.call(lambda *a, **k: {}, path="missing.py")["error"], "not_found")
        self.assertEqual(self.call(lambda *a, **k: {}, path="nothing/*.py")["error"], "not_found")
        self.assertEqual(self.call(lambda *a, **k: {}, path="../x.py")["error"], "scope_mismatch")
        self.assertEqual(self.call(lambda *a, **k: {}, path="")["error"], "invalid_argument")

        def slow(*args, **kwargs):
            raise BridgeError("timeout", "slow")

        self.assertEqual(self.call(slow, path="src/a.py")["error"], "timeout")

    def test_schema(self):
        self.assertEqual(OMH_LSP_DIAGNOSTICS_SCHEMA["parameters"]["required"], ["path"])


class _Client:
    def __init__(self, fresh):
        self.fresh, self.opened = fresh, []

    async def open_file(self, path, language_id="plaintext", text=None):
        self.opened.append(path)
        return len(self.opened)

    async def wait_for_diagnostics(self, path, version, timeout=None):
        return self.fresh.get(path, False)

    def diagnostics_for(self, path, fresh_only=False):
        return [_diag(0, "e")] if self.fresh.get(path) else []


class _Loop:
    def run(self, coro, timeout=None):
        try:
            coro.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("fake loop expects a coroutine that never suspends")


class _Service:
    def __init__(self, client, handled=lambda path: path.endswith(".py")):
        self.client, self._loop, self.handled = client, _Loop(), handled

    async def _get_or_spawn(self, path):
        return self.client

    def enabled_for(self, path):
        return self.handled(path)


class BridgeDiagnosticsTests(unittest.TestCase):
    def test_sync_first_then_targets_and_no_verdict_is_none(self):
        client = _Client({"/w/a.py": True})
        with mock.patch.object(lsp_bridge, "available", lambda: (_Service(client), None)), \
                mock.patch.object(lsp_bridge, "_language_id", lambda svc, path: "python"), \
                mock.patch.object(os.path, "isfile", lambda path: True):
            out = lsp_bridge.diagnostics(["/w/a.py", "/w/b.py", "/w/c.md"], sync=["/w/m.py"], timeout=1)
        self.assertEqual(client.opened, ["/w/m.py", "/w/a.py", "/w/b.py"])
        self.assertEqual(out, {"/w/a.py": [_diag(0, "e")], "/w/b.py": None})

    def test_unavailable(self):
        with mock.patch.object(lsp_bridge, "available", lambda: (None, "lsp_unavailable: off")):
            with self.assertRaises(BridgeError) as caught:
                lsp_bridge.diagnostics(["/w/a.py"])
        self.assertEqual(caught.exception.code, "lsp_unavailable")


if __name__ == "__main__":
    unittest.main()
