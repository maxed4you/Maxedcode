"""Fixes found by comparing with Cline (2026-11-02): resumed prefix, cut replies, patch, compaction, MCP names."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode import compact, config, memory, tool_validation
from maxedcode.tools import fs, mcp, undo

ENV_OLD = "<env>\nOS: Windows\nToday: 2026-11-01, Sunday\n</env>"
ENV_NEW = "<env>\nOS: Windows\nToday: 2026-11-02, Monday\n</env>"
MEM = "# Memory\nnote\n\n## Memory (project: C:\\p\\CLAUDE.md)\nold rule"


def history(chars):
    return [{"role": "user", "content": "x" * chars}, {"role": "assistant", "content": "done"}]


class ResumePrefixTests(unittest.TestCase):
    def test_new_day_keeps_the_saved_prefix_and_sends_only_the_date(self):
        old = [{"role": "system", "content": MEM}, {"role": "user", "content": ENV_OLD}]
        new = [{"role": "system", "content": MEM}, {"role": "user", "content": ENV_NEW}]
        prefix, note = memory.resume_prefix(old, new, history(5000))
        self.assertIs(prefix, old)
        self.assertIn("Today: 2026-11-02", note)
        self.assertNotIn("OS: Windows", note)

    def test_changed_memory_sends_only_the_changed_section(self):
        old = [{"role": "system", "content": MEM}, {"role": "user", "content": ENV_OLD}]
        new = [{"role": "system", "content": MEM.replace("old rule", "new rule")}, {"role": "user", "content": ENV_OLD}]
        prefix, note = memory.resume_prefix(old, new, history(5000))
        self.assertIs(prefix, old)
        self.assertIn("new rule", note)
        self.assertNotIn("note\n", note)

    def test_short_history_is_rebuilt_instead(self):
        old = [{"role": "system", "content": MEM}, {"role": "user", "content": ENV_OLD}]
        new = [{"role": "system", "content": MEM.replace("old rule", "new rule " * 50)}, {"role": "user", "content": ENV_NEW}]
        prefix, note = memory.resume_prefix(old, new, history(10))
        self.assertIs(prefix, new)
        self.assertEqual(note, "")

    def test_split_saved_handles_headless_and_tui_files(self):
        headless = [{"role": "system", "content": "SYSTEM"}, {"role": "system", "content": MEM},
                    {"role": "user", "content": ENV_OLD}, {"role": "user", "content": "task"}]
        prefix, hist = memory.split_saved(headless)
        self.assertEqual([m["content"] for m in prefix], [MEM, ENV_OLD])
        self.assertEqual(hist, [{"role": "user", "content": "task"}])
        prefix, hist = memory.split_saved([{"role": "user", "content": "task"}])
        self.assertEqual(prefix, [])
        self.assertEqual(len(hist), 1)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-cline-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        snap = patch.object(undo, "snap")
        snap.start()
        self.addCleanup(snap.stop)
        fs.clear_read_cache()

    def test_patch_reports_every_failed_block_and_changes_nothing(self):
        p = self.root / "a.txt"
        p.write_text("one\ntwo\nthree\n", encoding="utf-8")
        out = fs.tool_patch(str(p), [{"old_text": "one", "new_text": "1"}, {"old_text": "zwei", "new_text": "2"},
                                     {"old_text": "drei", "new_text": "3"}])
        self.assertIn("2 of 3 block(s) did not match", out)
        self.assertIn("block 2", out)
        self.assertIn("block 3", out)
        self.assertEqual(p.read_text(encoding="utf-8"), "one\ntwo\nthree\n")

    def test_failed_edit_says_when_the_file_changed_after_the_read(self):
        p = self.root / "b.txt"
        p.write_text("alpha\n", encoding="utf-8")
        fs.tool_read(str(p))
        time.sleep(0.02)
        p.write_text("beta gamma\n", encoding="utf-8")
        os.utime(p, None)
        out = fs.tool_edit(str(p), "alpha", "omega")
        self.assertIn("changed on disk after you last read it", out)

    def test_broken_json_is_reported_after_the_write(self):
        p = self.root / "c.json"
        out = fs.tool_write(str(p), '{"a": 1,}')
        self.assertIn("JSON validation FAILED", out)
        out = fs.tool_write(str(self.root / "d.json"), '{\n  // comment\n  "a": 1\n}')
        self.assertNotIn("JSON validation", out)


class CutAndCompactTests(unittest.TestCase):
    def test_cut_write_hint_gets_firmer(self):
        tool_validation.CUT_COUNT.clear()
        raw = '{"path": "C:/x/big.py", "content": "def f(): pass'
        first = tool_validation.cut_off_error("write_file", raw)
        second = tool_validation.cut_off_error("write_file", raw)
        third = tool_validation.cut_off_error("write_file", raw)
        self.assertIn("Send the call again", first)
        self.assertIn("2nd cut call for C:/x/big.py", second)
        self.assertIn("do NOT send it whole again", third)

    def test_length_continue_quotes_the_end(self):
        text = config.length_continue("A long report that ends here")
        self.assertIn("ends here", text)
        self.assertIn("Continue it exactly where it stopped", text)

    def test_files_touched_comes_from_the_calls(self):
        def call(name, path):
            return {"function": {"name": name, "arguments": json.dumps({"path": path})}}
        msgs = [{"role": "assistant", "content": "", "tool_calls": [call("read_file", "C:/p/a.py")]},
                {"role": "assistant", "content": "", "tool_calls": [call("edit_file", "C:/p/a.py"),
                                                                    call("read_file", "C:/p/b.py")]}]
        files = compact.files_touched(msgs)
        lines = files.splitlines()
        self.assertTrue(any(x.startswith("changed: ") and x.endswith("a.py") for x in lines), files)
        self.assertTrue(any(x.startswith("read: ") and x.endswith("b.py") for x in lines), files)
        self.assertEqual(files.count("a.py"), 1)
        body = compact.anchor_body("# Objective\nx", msgs)
        self.assertIn("<files_touched>", body)
        again = compact.files_touched([{"role": "user", "content": f'<compact id="s">\n{body}\n</compact>'}])
        self.assertIn("changed:", again)

    def test_basic_summary_needs_no_model(self):
        msgs = [{"role": "user", "content": "fix the bug"},
                {"role": "assistant", "content": "Found it in parse()", "tool_calls": [
                    {"function": {"name": "powershell", "arguments": json.dumps({"command": "pytest -q"})}}]}]
        text = compact.basic_summary(msgs)
        self.assertTrue(compact.filled(text))
        self.assertIn("pytest -q", text)
        self.assertIn("Found it in parse()", text)


class FileWatchUndoTests(unittest.TestCase):
    def test_a_command_change_keeps_the_exact_old_bytes_for_undo(self):
        from maxedcode.filewatch import FileWatch
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "conf.ini"
            p.write_bytes(b"a=1\r\nb=2\r\n")
            fw = FileWatch(texts=True)
            snap = fw.before(d)
            time.sleep(0.02)
            p.write_bytes(b"a=1\r\nb=3\r\n")
            (Path(d) / "new.txt").write_text("x", encoding="utf-8")
            changes = fw.diff(d, [], snap)
            kinds = {Path(path).name: kind for kind, path, _o, _n in changes}
            self.assertEqual(kinds, {"conf.ini": "changed", "new.txt": "new"})
            self.assertEqual(fw.old_raw[str(p)], b"a=1\r\nb=2\r\n")
            old_text = next(o for k, path, o, _n in changes if k == "changed")
            self.assertEqual(old_text, "a=1\nb=2\n")


class McpNameTests(unittest.TestCase):
    def test_names_are_valid_and_long_ones_stay_unique(self):
        self.assertEqual(mcp.tool_name("my.server", "do it"), "mcp__my_server__do_it")
        a = mcp.tool_name("server", "x" * 80 + "a")
        b = mcp.tool_name("server", "x" * 80 + "b")
        self.assertLessEqual(len(a), 64)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
