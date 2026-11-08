import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode.tools import undo


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-checkpoints-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ckpt = self.root / "checkpoints"
        self.p1 = self.project / "a.txt"
        self.p2 = self.project / "b.txt"
        self.p1.write_text("a0", encoding="utf-8")
        self.p2.write_text("b0", encoding="utf-8")
        patcher = patch.object(undo, "CKPT", self.ckpt)
        patcher.start()
        self.addCleanup(patcher.stop)
        undo.set_sid("test-session")
        undo.set_message_id(None)

    def test_rewind_by_message_id_keeps_all_records_in_that_tool_call(self):
        undo.set_message_id("call-1")
        undo.snap(str(self.p1), "a0")
        self.p1.write_text("a1", encoding="utf-8")
        undo.snap(str(self.p2), "b0")
        self.p2.write_text("b1", encoding="utf-8")
        self.assertEqual(undo.checkpoint_latest_changes_count(), 2)
        undo.set_message_id("call-2")
        undo.snap(str(self.p1), "a1")
        self.p1.write_text("a2", encoding="utf-8")

        self.assertEqual(undo.rewind("call-1"), 1)
        self.assertEqual(self.p1.read_text(encoding="utf-8"), "a1")
        self.assertEqual(self.p2.read_text(encoding="utf-8"), "b1")
        rows = [json.loads(line) for line in (self.ckpt / "test-session" / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["message_id"] for r in rows], ["call-1", "call-1"])

    def test_undo_last_restores_old_contents(self):
        undo.set_message_id("call-a")
        undo.snap(str(self.p1), "a0")
        self.p1.write_text("changed", encoding="utf-8")
        self.assertEqual(undo.undo_last(), str(self.p1))
        self.assertEqual(self.p1.read_text(encoding="utf-8"), "a0")

    def test_undo_step_reverts_every_change_of_the_last_turn(self):
        undo.set_turn("turn-1")
        undo.set_message_id("call-1")
        undo.snap(str(self.p1), "a0")
        self.p1.write_text("a1", encoding="utf-8")
        undo.set_turn("turn-2")
        undo.set_message_id("call-2")
        undo.snap(str(self.p1), "a1")
        self.p1.write_text("a2", encoding="utf-8")
        undo.set_message_id("call-3")
        undo.snap(str(self.p2), "b0")
        self.p2.write_text("b1", encoding="utf-8")
        created = self.project / "new.txt"
        undo.snap(str(created), "", new=True)
        created.write_text("created", encoding="utf-8")
        undo.set_turn(None)

        self.assertEqual(len(undo.last_group()), 3)
        self.assertEqual(sorted(undo.undo_step()), sorted([str(self.p1), str(self.p2), str(created)]))
        self.assertEqual(self.p1.read_text(encoding="utf-8"), "a1")
        self.assertEqual(self.p2.read_text(encoding="utf-8"), "b0")
        self.assertFalse(created.exists())
        self.assertEqual(undo.undo_step(), [str(self.p1)])
        self.assertEqual(self.p1.read_text(encoding="utf-8"), "a0")
        self.assertEqual(undo.undo_step(), [])

    def test_bytes_snapshot_keeps_crlf(self):
        self.p1.write_bytes(b"x\r\ny\r\n")
        undo.snap(str(self.p1), self.p1.read_bytes())
        self.p1.write_bytes(b"changed\n")
        undo.undo_step()
        self.assertEqual(self.p1.read_bytes(), b"x\r\ny\r\n")

    def test_prune_removes_stray_files_and_stale_sessions(self):
        undo.snap(str(self.p1), "a0")
        stale = self.ckpt / "old-session"
        stale.mkdir()
        (stale / "index.jsonl").write_text("", encoding="utf-8")
        old = 1_000_000_000
        os.utime(stale / "index.jsonl", (old, old))
        os.utime(stale, (old, old))
        (self.ckpt / "stray.bak").write_text("x", encoding="utf-8")
        undo.prune(keep={"test-session"})
        self.assertFalse(stale.exists())
        self.assertFalse((self.ckpt / "stray.bak").exists())
        self.assertTrue((self.ckpt / "test-session" / "index.jsonl").exists())

    def test_rewind_removes_files_created_after_boundary(self):
        undo.set_message_id("before")
        undo.snap(str(self.p1), "a0")
        self.p1.write_text("a1", encoding="utf-8")
        created = self.project / "new.txt"
        undo.set_message_id("later")
        undo.snap(str(created), "", new=True)
        created.write_text("created", encoding="utf-8")
        self.assertEqual(undo.rewind("before"), 1)
        self.assertFalse(created.exists())

    def test_rewind_after_kept_calls_undoes_later_turns_of_that_session(self):
        # the window's "fork from here" with files: the kept history holds call-1, later turns wrote the rest
        undo.set_message_id("call-1")
        undo.snap(str(self.p1), "a0")
        self.p1.write_text("a1", encoding="utf-8")
        undo.set_message_id("call-2")
        undo.snap(str(self.p1), "a1")
        self.p1.write_text("a2", encoding="utf-8")
        created = self.project / "new.txt"
        undo.snap(str(created), "", new=True)
        created.write_text("created", encoding="utf-8")
        undo.set_sid("other-chat")
        self.assertEqual(undo.rewind_after(["call-1"], "test-session"), [str(self.p1), str(created)])
        self.assertEqual(self.p1.read_text(encoding="utf-8"), "a1")
        self.assertFalse(created.exists())
        self.assertEqual(undo.rewind_after(["call-1"], "test-session"), [])     # consumed: nothing replays

    def test_rewind_after_without_kept_calls_undoes_everything(self):
        undo.set_message_id("call-9")
        undo.snap(str(self.p2), "b0")
        self.p2.write_text("b1", encoding="utf-8")
        self.assertEqual(undo.rewind_after([], "test-session"), [str(self.p2)])
        self.assertEqual(self.p2.read_text(encoding="utf-8"), "b0")


if __name__ == "__main__":
    unittest.main()
