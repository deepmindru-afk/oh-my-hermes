"""A model capture points back at the person's own words in Hermes' session store.

`omh_memory(action="capture")` records which Hermes session it was made in
(`source_ref = "hermes-session:<id>"`); `omh_memory(action="recall")` resolves
that, read-only, to the newest user message of that session at or before the
capture and hands back `origin = {"session_id", "around_message_id"}` for
Hermes' `session_search`. These pin:

- the stored ref is the exact string and the capture still auto-approves;
- demote and correct keep the `source_ref` they already carried;
- the origin names the user message just before the capture, never a later
  one, another role's, or another session's;
- a missing, session-less, corrupt or locked store gives no origin and recall
  still succeeds;
- a shared or background lens gets no origin;
- recall leaves the store's bytes and mtime as they were.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import memory_origin
from omh.plugin_bundle.omh.hermes_memory import HERMES_MEMORY_DELIMITER
from omh.plugin_bundle.omh.iso_datetime import strict_fromisoformat
from omh.plugin_bundle.omh.memory_origin import origin_source_ref, resolve_origin
from omh.plugin_bundle.omh.memory_session_lens import SURFACE_BACKGROUND, SURFACE_SHARED
from omh.plugin_bundle.omh.tools import memory_tool
from omh.plugin_bundle.omh.tools.memory_tool import RECALL_ORIGIN_NEXT_ACTION, omh_memory_handler
from project_identity_fixture import seed_project_identity
from test_memory_prefetch_canonical import SESSION, provider

FACT = "The operator ships releases only from the release branch."
QUERY = "which branch do releases ship from"


def make_state_db(path: Path) -> None:
    """The minimal Hermes `sessions`/`messages` shape `session_search` reads."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL NOT NULL);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),"
            " role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);"
        )
        connection.commit()
    finally:
        connection.close()


def add_messages(path: Path, session_id: str, rows: list[tuple[str, float]]) -> list[int]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("INSERT OR IGNORE INTO sessions (id, source, started_at) VALUES (?, 'cli', 0)", (session_id,))
        ids = [
            int(connection.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, 'x', ?)", (session_id, role, stamp)).lastrowid)
            for role, stamp in rows
        ]
        connection.commit()
        return ids
    finally:
        connection.close()


def epoch(stamp: str) -> float:
    return strict_fromisoformat(stamp).timestamp()


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        seed_project_identity(self.root)
        self.hermes = self.root / ".hermes"
        self.hermes.mkdir()
        self.store = self.root / ".omh"
        self.db = self.hermes / "state.db"
        self.enterContext(patch.dict(os.environ, {"OMH_HOME": str(self.store), "HERMES_HOME": str(self.hermes)}))
        provider(self.root)

    def call(self, action: str, **args) -> dict:
        return json.loads(omh_memory_handler({"action": action, **args}, cwd=str(self.root), session_id=SESSION))

    def read_record(self, record_id: str) -> dict:
        return json.loads((self.store / "memory" / "records" / f"{record_id}.json").read_text(encoding="utf-8"))

    def capture(self, summary: str = FACT) -> dict:
        result = self.call("capture", summary=summary)
        self.assertEqual(result["status"], "remembered", result)
        return self.read_record(str(result["record_id"]))

    def seed_turn(self, record: dict) -> int:
        """Messages around the capture; returns the id the origin must name."""
        make_state_db(self.db)
        at = epoch(record["created_at"])
        _, before, _, _ = add_messages(
            self.db, SESSION, [("user", at - 60), ("user", at - 0.5), ("assistant", at + 0.5), ("user", at + 600)]
        )
        # Another conversation's message, newer and closer, must not be chosen.
        add_messages(self.db, "session-b", [("user", at - 0.1)])
        return before

    def recall(self) -> dict:
        result = self.call("recall", query=QUERY)
        self.assertEqual(result["status"], "found", result)
        return result


class CaptureOriginTests(_Home):
    def test_capture_stores_the_session_ref_and_still_auto_approves(self) -> None:
        record = self.capture()
        self.assertEqual(record["source_ref"], f"hermes-session:{SESSION}")
        self.assertEqual(record["admission"]["state"], "approved_auto_safe")
        self.assertNotIn("source_evidence", record)
        self.assertEqual([row["record_id"] for row in self.recall()["records"]], [record["record_id"]])

    def test_an_unusable_session_id_records_no_ref(self) -> None:
        for session_id in ("", "a b", "x" * 121, "id/../etc", "é"):
            with self.subTest(session_id=session_id):
                self.assertEqual(origin_source_ref(session_id), "")
        self.assertEqual(origin_source_ref("20261030_004630_c791b1e2"), "hermes-session:20261030_004630_c791b1e2")

    def test_correct_carries_the_old_records_ref(self) -> None:
        old = self.capture("The deploy window is Friday afternoon.")
        old["source_ref"] = "docs/deploy.md"
        (self.store / "memory" / "records" / f"{old['record_id']}.json").write_text(json.dumps(old), encoding="utf-8")
        result = self.call("correct", record_id=old["record_id"], summary="The deploy window is Thursday afternoon.")
        self.assertEqual(result["status"], "corrected", result)
        self.assertEqual(self.read_record(result["record_id"])["source_ref"], "docs/deploy.md")

    def test_demote_keeps_its_demotion_ref(self) -> None:
        entry = "The operator deploys from the release branch every Friday afternoon, never from main."
        memories = self.hermes / "memories"
        memories.mkdir()
        (memories / "MEMORY.md").write_text(HERMES_MEMORY_DELIMITER.join([entry]), encoding="utf-8")
        digest = hashlib.sha256(entry.encode("utf-8")).hexdigest()
        result = self.call("demote", entry_sha256=digest[:12])
        self.assertEqual(result["status"], "demoted", result)
        self.assertEqual(self.read_record(result["record_id"])["source_ref"], f"hermes:MEMORY.md#{digest[:16]}")


class RecallOriginTests(_Home):
    def test_recall_names_the_user_message_just_before_the_capture(self) -> None:
        record = self.capture()
        expected = self.seed_turn(record)
        result = self.recall()
        [row] = result["records"]
        self.assertEqual(row["origin"], {"session_id": SESSION, "around_message_id": expected})
        self.assertTrue(result["next_action"].endswith(RECALL_ORIGIN_NEXT_ACTION), result["next_action"])
        self.assertEqual(result["schema_version"], "omh_memory_recall/v1")

    def test_recall_reads_and_never_writes_the_store(self) -> None:
        self.seed_turn(self.capture())
        before = (self.db.read_bytes(), self.db.stat().st_mtime_ns, sorted(path.name for path in self.hermes.iterdir()))
        self.assertIn("origin", self.recall()["records"][0])
        after = (self.db.read_bytes(), self.db.stat().st_mtime_ns, sorted(path.name for path in self.hermes.iterdir()))
        self.assertEqual(after, before)

    def test_a_record_without_a_session_ref_gets_no_origin(self) -> None:
        record = self.capture()
        self.seed_turn(record)
        record["source_ref"] = ""
        (self.store / "memory" / "records" / f"{record['record_id']}.json").write_text(json.dumps(record), encoding="utf-8")
        result = self.recall()
        self.assertNotIn("origin", result["records"][0])
        self.assertNotIn("session_search", result["next_action"])

    def assert_no_origin(self) -> None:
        result = self.recall()
        self.assertEqual(len(result["records"]), 1)
        self.assertNotIn("origin", result["records"][0])
        self.assertNotIn("session_search", result["next_action"])

    def test_a_missing_store_gives_no_origin(self) -> None:
        self.capture()
        self.assert_no_origin()
        self.assertFalse(self.db.exists(), "a read-only open never creates the store")

    def test_a_missing_session_gives_no_origin(self) -> None:
        record = self.capture()
        make_state_db(self.db)
        add_messages(self.db, "session-b", [("user", epoch(record["created_at"]))])
        self.assert_no_origin()

    def test_only_later_or_non_user_messages_give_no_origin(self) -> None:
        record = self.capture()
        make_state_db(self.db)
        at = epoch(record["created_at"])
        add_messages(self.db, SESSION, [("assistant", at - 1), ("tool", at - 1), ("user", at + 600)])
        self.assert_no_origin()

    def test_a_corrupt_store_gives_no_origin(self) -> None:
        self.capture()
        self.db.write_bytes(b"not a sqlite database at all" * 64)
        self.assert_no_origin()

    def test_a_store_without_the_messages_table_gives_no_origin(self) -> None:
        self.capture()
        sqlite3.connect(self.db).close()
        self.assert_no_origin()

    def test_a_locked_store_gives_no_origin(self) -> None:
        self.seed_turn(self.capture())
        writer = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        self.addCleanup(writer.execute, "ROLLBACK")
        self.assert_no_origin()

    def test_a_shared_or_background_lens_gets_no_origin(self) -> None:
        self.seed_turn(self.capture())
        lens = memory_tool._session_lens({"session_id": SESSION})
        for surface_class in (SURFACE_SHARED, SURFACE_BACKGROUND):
            with self.subTest(surface_class=surface_class):
                result = memory_tool._recall({"query": QUERY}, dataclasses.replace(lens, surface_class=surface_class))
                self.assertEqual(result["status"], "found", result)
                self.assertNotIn("origin", result["records"][0])
                self.assertNotIn("session_search", result["next_action"])


class ResolveOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.db = self.home / "state.db"
        make_state_db(self.db)
        self.stamp = "2026-10-30T01:00:00Z"
        self.ids = add_messages(self.db, "s1", [("user", epoch(self.stamp) + 4), ("user", epoch(self.stamp) + 6)])

    def test_the_slack_covers_a_second_truncated_stamp(self) -> None:
        self.assertEqual(resolve_origin(self.home, "s1", self.stamp), {"session_id": "s1", "around_message_id": self.ids[0]})

    def test_a_naive_stamp_is_utc(self) -> None:
        self.assertEqual(resolve_origin(self.home, "s1", "2026-10-30T01:00:00")["around_message_id"], self.ids[0])

    def test_bad_inputs_give_none(self) -> None:
        for session_id, stamp in (("s1", ""), ("s1", "not-a-time"), ("s1", "2026-10-29T24:00:00Z"), ("", self.stamp), ("a b", self.stamp)):
            with self.subTest(session_id=session_id, stamp=stamp):
                self.assertIsNone(resolve_origin(self.home, session_id, stamp))

    def test_an_open_error_gives_none(self) -> None:
        with patch.object(memory_origin, "connect_readonly", side_effect=sqlite3.OperationalError("unable to open")):
            self.assertIsNone(resolve_origin(self.home, "s1", self.stamp))


if __name__ == "__main__":
    unittest.main()
