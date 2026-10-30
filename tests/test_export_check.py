"""The round-end export check: names an edited module exports or imports that no longer exist.

The measured miss (OPT-A live run, 2026-10-30): after renaming `compute_quota`, GPT models
updated `from .quota import compute_quota` in `api.py` and left `__all__ = ["compute_quota"]`.
No test that imports the new name fails and pyright reports nothing, so the stale entry stayed.
These tests pin what the check says, when it stays silent, and where its note sits in the one
`pre_verify` message.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import export_check
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks
from omh.plugin_bundle.omh.impact_check import format_note as impact_format_note

QUOTA = "def allocate_quota(units: int) -> int:\n    return units * 3\n\n\nclass LegacyPlanner:\n    def compute_quota(self) -> int:\n        return 1\n"
STALE_API = 'from .quota import allocate_quota\n\n__all__ = ["compute_quota"]\n'
FIXED_API = 'from .quota import allocate_quota\n\n__all__ = ["allocate_quota"]\n'


class _Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "ws"
        (self.root / ".git").mkdir(parents=True)

    def write(self, relative: str, text: str) -> str:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def note(self, *relatives: str) -> str:
        return export_check.round_end_note([str(self.root / relative) for relative in relatives])


class StaleAllTest(_Workspace):
    def test_the_measured_miss_is_named_by_file_and_line(self):
        self.write("src/benchapp/__init__.py", "")
        self.write("src/benchapp/quota.py", QUOTA)
        self.write("src/benchapp/api.py", STALE_API)
        note = self.note("src/benchapp/api.py", "src/benchapp/quota.py")
        self.assertEqual(note.splitlines(), [
            "OMH export check: a file you edited names something that no longer exists:",
            "src/benchapp/api.py:3: `__all__` lists `compute_quota`, which this module no longer defines or imports",
            "Update or remove each stale name.",
        ])

    def test_a_correct_rename_is_silent(self):
        self.write("src/benchapp/__init__.py", "")
        self.write("src/benchapp/quota.py", QUOTA)
        self.write("src/benchapp/api.py", FIXED_API)
        self.assertEqual(self.note("src/benchapp/api.py", "src/benchapp/quota.py"), "")

    def test_every_valid_way_to_bind_an_exported_name_is_silent(self):
        # The control template's package: a submodule exported without an import, a name bound in
        # both arms of a try, `+=`, `append`, `extend`, a name defined after `__all__`, and more.
        self.write("src/store/rows.py", "ROW_LIMIT = 5\n\n\ndef load_rows(count):\n    return list(range(count))\n")
        self.write("src/store/formats.py", "")
        self.write("src/store/codecs.py", "")
        self.write("src/store/drivers/__init__.py", "")
        self.write("src/store/__init__.py", (
            "import os.path\nfrom . import formats\nfrom .rows import load_rows, ROW_LIMIT as LIMIT\n"
            "try:\n    from ._native import fast_rows\nexcept ImportError as missing:\n    fast_rows = None\n"
            "if os.path.sep:\n    class Reader:\n        pass\nfor index, (left, *rest) in enumerate([(1, 2)]):\n    pass\n"
            "with open(__file__) as handle:\n    pass\n"
            "def configure():\n    global CONFIGURED\n    CONFIGURED = True\n"
            "TOTAL: int = 0\nCOUNT = 0\nCOUNT += 1\n"
            '__all__ = ["load_rows", "LIMIT", "formats", "codecs", "drivers", "fast_rows", "missing", "Reader"]\n'
            '__all__ += ["STORE_VERSION", "os", "index", "left", "rest", "handle", "configure", "CONFIGURED"]\n'
            '__all__.append("TOTAL")\n__all__.extend(("COUNT",))\nSTORE_VERSION = "1"\n'
        ))
        self.assertEqual(self.note("src/store/__init__.py"), "")

    def test_a_module_whose_names_cannot_be_known_is_skipped(self):
        # Each is how a real package binds names without a statement: found in a sweep over CPython's
        # standard library, a 4,576-file site-packages, Hermes and this repo (re, attrs, numpy, the shim).
        for body in (
            "from .other import *\n",
            "def __getattr__(name):\n    return name\n",
            "__getattr__ = make_getattr(__name__)\n",
            "globals()['gone'] = 1\n",
            "def add(module):\n    setattr(module, 'gone', 1)\n",
            "import enum\n@enum.global_enum\nclass Flag(enum.IntFlag):\n    gone = 1\n",
            "__path__.append('elsewhere')\n",
            "__all__ = ['gone'] + []\n",
            "__all__.extend(other.__all__)\n",
        ):
            with self.subTest(body=body):
                self.write("pkg/mod.py", f"__all__ = ['gone']\n{body}")
                self.assertEqual(self.note("pkg/mod.py"), "")

    def test_implicit_module_names_and_compiled_submodules_are_bound(self):
        self.write("pkg/_speedups.cpython-311-darwin.so", "")
        self.write("pkg/_typed.pyi", "")
        self.write("pkg/__init__.py", "__all__ = ['__doc__', '__file__', '_speedups', '_typed']\n")
        self.assertEqual(self.note("pkg/__init__.py"), "")

    def test_parse_errors_unknown_paths_and_other_files_are_silent(self):
        self.write("pkg/broken.py", "__all__ = ['gone'\n")
        self.write("pkg/notes.txt", "__all__ = ['gone']\n")
        self.assertEqual(self.note("pkg/broken.py", "pkg/notes.txt", "pkg/missing.py"), "")
        # Hermes reports landed paths as absolute; a relative one has no root to resolve against.
        self.write("pkg/mod.py", "__all__ = ['gone']\n")
        self.assertEqual(export_check.round_end_note(["pkg/mod.py"]), "")
        for value in (None, "pkg/mod.py", 7, [None, 3]):
            self.assertEqual(export_check.round_end_note(value), "")


class StaleImportTest(_Workspace):
    def test_a_relative_and_an_absolute_import_of_a_renamed_name_are_named(self):
        self.write("src/benchapp/__init__.py", "")
        self.write("src/benchapp/quota.py", QUOTA)
        self.write("src/benchapp/api.py", 'from .quota import compute_quota\n\n__all__ = ["compute_quota"]\n')
        self.write("src/benchapp/service.py", "from benchapp.quota import compute_quota as cq\n")
        note = self.note("src/benchapp/api.py", "src/benchapp/service.py")
        self.assertIn("src/benchapp/api.py:1: `from .quota import compute_quota`: src/benchapp/quota.py no longer defines "
                      "`compute_quota`", note)
        self.assertIn("src/benchapp/service.py:1: `from benchapp.quota import compute_quota`", note)
        # The `__all__` entry is bound by the stale import, so it is that import that is named.
        self.assertNotIn("`__all__` lists", note)

    def test_imports_that_resolve_or_leave_the_workspace_are_silent(self):
        self.write("src/benchapp/__init__.py", "from . import formats\n")
        self.write("src/benchapp/formats.py", "")
        self.write("src/benchapp/quota.py", QUOTA)
        # Inside a package an absolute import starts above it, and a standard-library name is the
        # standard library's: neither of these workspace files is what those imports load.
        self.write("src/benchapp/typing.py", "")
        self.write("src/benchapp/helpers.py", "")
        self.write("src/benchapp/api.py", (
            "import os\nfrom os import path\nfrom . import formats\nfrom .quota import allocate_quota, LegacyPlanner\n"
            "from benchapp import quota\nfrom requests import get\nfrom .absent import thing\n"
            "from typing import Any\nfrom helpers import tool\n"
            "try:\n    from .quota import optional_name\nexcept ImportError:\n    optional_name = None\n"
        ))
        self.assertEqual(self.note("src/benchapp/api.py"), "")


class PreVerifyCompositionTest(_Workspace):
    def setUp(self):
        super().setUp()
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        verify_hooks.reset_stashed_answers()
        self.addCleanup(verify_hooks.reset_stashed_answers)
        self.write("src/benchapp/__init__.py", "")
        self.write("src/benchapp/quota.py", QUOTA)
        self.api = self.write("src/benchapp/api.py", STALE_API)
        self.home = Path(self._tmp.name) / "omh"

    def fire(self, **overrides):
        payload = {"session_id": "export-session", "coding": True, "attempt": 0, "final_response": "{}",
                   "changed_paths": [self.api], "omh_home": str(self.home)}
        payload.update(overrides)
        return verify_hooks.pre_verify(**payload)

    def test_the_note_sits_between_the_impact_note_and_the_check_under_one_closing(self):
        impact = impact_format_note([("src/benchapp/report.py", 5, "error")], recheck_tool=False)
        with patch.object(verify_hooks, "round_end_note", lambda session_id, changed_paths: impact):
            message = self.fire()["message"]
        self.assertLess(message.index("OMH impact check"), message.index("OMH export check"))
        self.assertLess(message.index("OMH export check"), message.index("OMH round-end consistency check"))
        self.assertTrue(message.endswith(verify_hooks.ROUND_END_CLOSING))
        self.assertEqual(message.count("When you are done"), 1)

    def test_the_note_alone_outside_coding_gets_the_one_closing(self):
        message = self.fire(coding=False)["message"]
        self.assertEqual(message, f"{self.note('src/benchapp/api.py')}\n{verify_hooks.ROUND_END_CLOSING}")

    def test_only_the_first_stop_runs_it_and_a_clean_tree_adds_nothing(self):
        self.assertIsNone(self.fire(coding=False, attempt=1))
        Path(self.api).write_text(FIXED_API, encoding="utf-8")
        self.assertIsNone(self.fire(coding=False))
        self.assertEqual(self.fire()["message"],
                         f"{verify_hooks.ROUND_END_CHECKLIST}\n{verify_hooks.ROUND_END_CLOSING}")


if __name__ == "__main__":
    unittest.main()
