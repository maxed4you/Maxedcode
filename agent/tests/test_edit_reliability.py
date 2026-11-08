import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode.tools import fs, undo


class EditReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-edit-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "sample.py"
        self.path.write_text("def f():\n    return 1\n", encoding="utf-8")

    def test_invalid_python_edit_is_rejected_without_disk_write_or_checkpoint(self):
        with patch.object(undo, "snap") as snap:
            result = fs.tool_edit(str(self.path), "return 1", "return (")
        self.assertIn("dry-run Python validation failed", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "def f():\n    return 1\n")
        snap.assert_not_called()

    def test_duplicate_exact_text_reports_line_locations(self):
        self.path.write_text("x = 1\nx = 1\n", encoding="utf-8")
        result = fs.tool_edit(str(self.path), "x = 1", "x = 2")
        self.assertIn("found 2 times", result)
        self.assertIn("L1", result)
        self.assertIn("L2", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_fuzzy_hint_is_fast_on_repetitive_file(self):
        # The full quadratic scan took ~80 s here and froze the UI while a failed patch "ran".
        lines = [f"echo   %YL%{i % 7})%C0% IQ2_XS   2-bit, best balance: prompts read faster {i}" for i in range(300)]
        search = "\n".join(lines[50:70]).replace("bit", "bіt")
        started = time.monotonic()
        match = fs.find_similar_lines(search, "\n".join(lines))
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(match["line"], 51)

    def test_fuzzy_match_is_a_hint_never_an_automatic_edit(self):
        original = "def f():\n    return 1\n"
        result = fs.tool_edit(str(self.path), "def f():\n    return 11", "def f():\n    return 2")
        self.assertIn("did you mean", result.lower())
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_whitespace_only_difference_is_applied_once(self):
        with patch.object(undo, "snap"):
            result = fs.tool_edit(str(self.path), "def f():\n  return 1", "def f():\n    return 2")
        self.assertIn("OK, edited", result)
        self.assertIn("indentation adjusted", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "def f():\n    return 2\n")

    def test_indent_shift_is_carried_into_new_text(self):
        self.path.write_text("class A:\n    def f(self):\n        return 1\n", encoding="utf-8")
        with patch.object(undo, "snap"):
            result = fs.tool_patch(str(self.path), [{"old_text": "def f(self):\n    return 1",
                                                     "new_text": "def f(self):\n    x = 2\n    return x"}])
        self.assertIn("OK, applied", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "class A:\n    def f(self):\n        x = 2\n        return x\n")

    def test_trailing_spaces_and_smart_quotes_match(self):
        self.path.write_text("msg = 'hi'   \nok = 1\n", encoding="utf-8")
        with patch.object(undo, "snap"):
            result = fs.tool_edit(str(self.path), "msg = \u2018hi\u2019\nok = 1", "msg = 'bye'\nok = 1")
        self.assertIn("OK, edited", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "msg = 'bye'\nok = 1\n")

    def test_grep_searches_a_named_big_file_and_reports_skipped_ones(self):
        log = self.root / "big.log"
        log.write_text("ok 200\n" * 10 + "bad 503\n" * 3, encoding="utf-8")
        with patch.object(fs, "GREP_WALK_MAX", 10), patch.object(fs.shutil, "which", return_value=None):
            self.assertIn("big.log: 3", fs.tool_grep("503", str(log), output_mode="count"))
            walked = fs.tool_grep("503", str(self.root), output_mode="count")
        self.assertIn("skipped", walked)
        self.assertIn("big.log", walked)

    def test_missing_path_uses_the_only_same_name_file(self):
        root = self.path.parent
        (root / "config").mkdir()
        target = root / "config" / "dev.json"
        target.write_text('{"port": 8080}\n', encoding="utf-8")
        with contextlib.chdir(root), patch.object(undo, "snap"):
            result = fs.tool_edit(str(root / "dev.json"), '"port": 8080', '"port": 9000')
        self.assertIn("does not exist; used the only file", result)
        self.assertIn("OK, edited", result)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"port": 9000}\n')

    def test_missing_path_with_two_candidates_is_not_guessed(self):
        root = self.path.parent
        for d in ("a", "b"):
            (root / d).mkdir()
            (root / d / "dev.json").write_text("{}\n", encoding="utf-8")
        with contextlib.chdir(root):
            self.assertIsNone(fs.resolve_missing(str(root / "dev.json")))
            self.assertIsNone(fs.resolve_missing(str(root / "nope_123.txt")))
            self.assertIn("Error", fs.tool_read(str(root / "dev.json")))

    def test_line_number_copied_into_new_text_is_removed(self):
        self.path.write_text("a = 1\nb = 2\n", encoding="utf-8")
        with patch.object(undo, "snap"):
            result = fs.tool_patch(str(self.path), [{"old_text": "b = 2", "new_text": "2: b = 2\nc = 3"}])
        self.assertIn("OK, applied", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "a = 1\nb = 2\nc = 3\n")

    def test_real_numbered_content_is_kept(self):
        path = self.path.with_suffix(".txt")
        path.write_text("steps:\n1: start\n", encoding="utf-8")
        with patch.object(undo, "snap"):
            fs.tool_edit(str(path), "1: start", "1: start\n2: start")
        self.assertEqual(path.read_text(encoding="utf-8"), "steps:\n1: start\n2: start\n")

    def test_edit_that_already_landed_is_reported_not_retried(self):
        self.path.write_text("def f():\n    return 2\n", encoding="utf-8")
        old, new = "def f():\n    return 1", "def f():\n    return 2"
        edited = fs.tool_edit(str(self.path), old, new)
        patched = fs.tool_patch(str(self.path), [{"old_text": old, "new_text": new}])
        other = fs.tool_edit(str(self.path), "    return 1", "    return 2  # fixed")
        for result in (edited, patched, other):
            self.assertIn("found 0 times", result)
        self.assertIn("already in the file (L1)", edited)
        self.assertIn("already in the file (L1)", patched)
        self.assertNotIn("already in the file", other)                 # new_text is not there: a normal miss
        self.assertEqual(self.path.read_text(encoding="utf-8"), "def f():\n    return 2\n")

    def test_ambiguous_tolerant_match_is_refused(self):
        self.path.write_text("if a:\n    x = 1\nif b:\n  x = 1\n", encoding="utf-8")
        result = fs.tool_edit(str(self.path), "   x = 1   ", "x = 2")
        self.assertIn("found 0 times", result)

    def test_exact_edit_preserves_crlf(self):
        self.path.write_bytes(b"x = 1\r\ny = 2\r\n")
        with patch.object(undo, "snap"):
            result = fs.tool_edit(str(self.path), "x = 1\ny = 2", "x = 3\ny = 4")
        self.assertIn("OK, edited", result)
        self.assertEqual(self.path.read_bytes(), b"x = 3\r\ny = 4\r\n")

    def test_dry_run_rejects_conflict_markers(self):
        out, err = fs.apply_edits_dry_run("x = 1\n", [{"old_text": "x = 1", "new_text": "<<<<<<< ours"}], "sample.py")
        self.assertIsNone(out)
        self.assertIn("conflict markers", err)

    def test_noop_edits_never_report_ok(self):
        original = "def f():\n    return 1\n"
        with patch.object(undo, "snap") as snap:
            results = [
                fs.tool_edit(str(self.path), "return 1", "return 1"),
                fs.tool_patch(str(self.path), [{"old_text": "return 1", "new_text": "return 1"}]),
                fs.tool_apply_diff(str(self.path), "@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 1\n"),
            ]
        for result in results:
            self.assertTrue(result.startswith("Error: nothing changed"), result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        snap.assert_not_called()

    def test_write_with_same_content_says_nothing_changed(self):
        with patch.object(undo, "snap") as snap:
            result = fs.tool_write(str(self.path), "def f():\n    return 1\n")
        self.assertIn("nothing changed on disk", result)
        snap.assert_not_called()

    def test_disk_text_reads_current_file_and_missing_as_empty(self):
        self.path.write_bytes(b"a\r\nb\r\n")
        self.assertEqual(fs.disk_text(str(self.path)), "a\nb\n")
        self.assertEqual(fs.disk_text(str(self.root / "missing.py")), "")
        self.assertIsNone(fs.disk_text(str(self.root)))


class ClaudeCodeStyleGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-guard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.py"
        self.path.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
        fs.READ_SEEN.clear()
        self.addCleanup(fs.READ_SEEN.clear)
        patcher = patch.object(undo, "snap")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_line_number_prefixes_copied_from_read_file_are_stripped(self):
        result = fs.tool_edit(str(self.path), "1: def f():\n2:     return 1", "1: def f():\n2:     return 10")
        self.assertTrue(result.startswith("OK"), result)
        self.assertIn("line-number prefixes", result)
        self.assertIn("    return 10\n", self.path.read_text(encoding="utf-8"))
        result = fs.tool_patch(str(self.path), [{"old_text": "6:     return 2", "new_text": "6:     return 20"}])
        self.assertTrue(result.startswith("OK"), result)
        self.assertIn("    return 20\n", self.path.read_text(encoding="utf-8"))

    def test_real_code_that_looks_like_a_prefix_is_not_rewritten(self):
        self.assertIsNone(fs.strip_line_numbers("1: x", "1: y", "a\nb\n"))
        self.assertIsNone(fs.strip_line_numbers("1: def f():\n    return 1", "", "def f():\n    return 1\n"))

    def test_write_file_over_an_unread_file_is_refused(self):
        self.path.write_text("def f():\n    return 1\n" * 200, encoding="utf-8")    # larger than a stub
        original = self.path.read_text(encoding="utf-8")
        result = fs.tool_write(str(self.path), "print('new')\n")
        self.assertIn("was not read in this session", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        fs.tool_read(str(self.path))
        self.assertTrue(fs.tool_write(str(self.path), "print('new')\n").startswith("OK"))
        # its own write keeps the file "known": a second rewrite needs no new read
        self.assertTrue(fs.tool_write(str(self.path), "print('newer')\n").startswith("OK"))

    def test_write_file_after_an_outside_change_is_refused(self):
        fs.tool_read(str(self.path))
        self.path.write_text("changed by the user\n", encoding="utf-8")
        import os
        os.utime(self.path, ns=(1, 1))
        result = fs.tool_write(str(self.path), "print('new')\n")
        self.assertIn("changed on disk after you last read it", result)

    def test_edit_keeps_a_read_file_known_for_a_later_rewrite(self):
        fs.tool_read(str(self.path))
        self.assertTrue(fs.tool_edit(str(self.path), "return 1", "return 3").startswith("OK"))
        self.assertTrue(fs.tool_write(str(self.path), "print('new')\n").startswith("OK"))

    def test_lazy_placeholder_rewrite_is_refused(self):
        fs.tool_read(str(self.path))
        original = self.path.read_text(encoding="utf-8")
        result = fs.tool_write(str(self.path), "def f():\n    return 5\n\n# ... rest of the code unchanged\n")
        self.assertIn("placeholder", result)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_new_files_are_written_freely(self):
        new = Path(self.tmp.name) / "new.py"
        self.assertTrue(fs.tool_write(str(new), "x = 1\n").startswith("OK"))


class CompactAndPromptHintTests(unittest.TestCase):
    def test_summary_text_reads_only_compact_anchors(self):
        from maxedcode import compact
        anchor = {"role": "user", "content": '<compact id="s1">\n# Objective\nfix it\n(3 messages compressed)</compact>'}
        self.assertEqual(compact.summary_text(anchor), "# Objective\nfix it\n(3 messages compressed)")
        self.assertEqual(compact.summary_text({"role": "user", "content": "hello"}), "")
        self.assertEqual(compact.summary_text({"role": "assistant", "content": '<compact id="x">a</compact>'}), "")

    def test_deep_think_keywords(self):
        from maxedcode import config
        for text in ("ultrathink: fix the parser", "please think hard", "подумай хорошо и почини", "Глубоко подумай"):
            self.assertTrue(config.DEEP_THINK_RE.search(text), text)
        for text in ("fix the parser", "подумай, что ещё добавить", "I think hardware is fine"):
            self.assertFalse(config.DEEP_THINK_RE.search(text), text)


class EditCardTests(unittest.TestCase):
    def test_card_shows_real_disk_diff_with_file_line_numbers(self):
        from maxedcode.ui.widgets import Card
        card = Card("edit_file", {"path": "C:\\x\\a.py", "old_text": "return 1", "new_text": "return 2"})
        self.assertTrue(card.disk_change("a\nreturn 1\nz\n", "a\nreturn 2\nz\n"))
        self.assertIn(" 2 -return 1", str(card.diff))
        self.assertFalse(card.disk_change("same\n", "same\n"))
        self.assertIn("no changes on disk", str(card.diff))


    def test_patch_card_is_a_diff_before_and_after_the_edit(self):
        # /history and subagent tabs rebuild the card after the patch is on disk: it used to fall
        # back to raw JSON with escaped newlines.
        from maxedcode.ui.widgets import Card
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "chain.ts"
            f.write_text("a\nb\nexport const x = 1;\nz\n", encoding="utf-8")
            args = {"path": str(f), "blocks": [{"old_text": "export const x = 1;\n",
                                                "new_text": "export const x = 2;\n// two\n"}]}
            before = str(Card("patch", args).diff)
            self.assertIn(" 3 -export const x = 1;", before)
            self.assertIn(" 4 +// two", before)
            f.write_text("a\nb\nexport const x = 2;\n// two\nz\n", encoding="utf-8")
            self.assertEqual(str(Card("patch", args).diff), before)   # placed by undoing the blocks
            f.write_text("rewritten since\n", encoding="utf-8")
            loose = str(Card("patch", args).diff)                      # cannot be placed: the blocks alone
            self.assertIn("-export const x = 1;", loose)
            self.assertIn("+// two", loose)
            self.assertNotIn("\\n", loose)
            bad = Card("patch", {"path": str(f), "blocks": [{"old_text": 1}]})   # malformed: JSON, no crash
            self.assertIsNone(bad.diff)

    def test_write_file_card_is_clipped(self):
        from maxedcode.ui.widgets import Card
        with tempfile.TemporaryDirectory() as d:
            body = "".join(f"x{i}\n" for i in range(500))
            text = str(Card("write_file", {"path": str(Path(d) / "big.py"), "content": body}).diff)
            self.assertIn("+500", text)
            self.assertIn("more lines", text)
            self.assertLess(text.count("\n"), 140)


class EditProgressTests(unittest.TestCase):
    def test_streaming_tool_call_status_counts_lines_incrementally(self):
        from maxedcode.util import tool_call_status, track_tool_call
        c = {"id": "", "name": "write_file", "args": ""}
        for piece in ['{"path": "C:\\\\proj\\\\app', '.py", "content": "a\\nb', '\\nc\\n"}']:
            c["args"] += piece
            track_tool_call(c, piece)
        status = tool_call_status(c, 2, {"write_file": "Writing file"})
        self.assertEqual(status, "Composing: Writing file → app.py · 3 lines (call 2)…")

    def test_step_after_successful_tools_gets_short_think_budget(self):
        from maxedcode import config
        ok = [{"role": "user", "content": "x"}, {"role": "assistant", "content": ""},
              {"role": "tool", "tool_call_id": "1", "content": "OK, edited a.py"}]
        failed = ok[:-1] + [{"role": "tool", "tool_call_id": "1", "content": "Error: old_text found 0 times"}]
        self.assertTrue(config.quick_step(ok))
        self.assertFalse(config.quick_step(failed))
        self.assertFalse(config.quick_step(ok[:1]))
        # The step right after the turn's first reads plans the solution: full cap, not the step cap.
        read = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
        wrote = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "2", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]}
        result = {"role": "tool", "tool_call_id": "1", "content": "# Task\n..."}
        self.assertFalse(config.quick_step([ok[0], read, result]))
        self.assertTrue(config.quick_step([ok[0], read, result, wrote, {**result, "tool_call_id": "2"}]))
        with patch.object(config, "THINK_BUDGET", 0), patch.object(config, "THINK_CAP", 4096), \
             patch.object(config, "THINK_CAP_STEP", 1024), patch.object(config, "model_profile", lambda *a: {}):
            self.assertEqual(config.think_budget(0, "medium", step=True), 1024)
            self.assertEqual(config.think_budget(0, "medium", step=False), 4096)
            self.assertEqual(config.think_budget(0, "high", step=True), config.THINK_CAP_HIGH)
            self.assertTrue(config.planning_step([ok[0], read, result]))
            # Live R4: the task, then the tests in a second read step; the solution step still plans.
            two_reads = [ok[0], read, result, read, result]
            self.assertTrue(config.planning_step(two_reads))
            self.assertFalse(config.quick_step(two_reads))
            self.assertFalse(config.planning_step([ok[0], read, result, wrote, {**result, "tool_call_id": "2"}]))
            self.assertEqual(config.think_budget(0, "medium", plan=True), config.THINK_CAP_HIGH)
            self.assertEqual(config.think_budget(0, "low", plan=True), config.THINK_CAP_LOW)


class SystemCodeNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-syscode-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fs._SYSTEM_NOTED.clear()

    def test_registry_write_names_existing_tests_once(self):
        src = self.root / "setup_tool.py"
        src.write_text("import winreg\n\ndef main():\n    winreg.SetValueEx(k, 'Path', 0, 2, 'x')\n", encoding="utf-8")
        (self.root / "test_setup_tool.py").write_text("import setup_tool\n", encoding="utf-8")
        note = fs._system_code_note(src)
        self.assertIn("[system code]", note)
        self.assertIn("registry", note)
        self.assertIn("test_setup_tool.py", note)
        self.assertEqual(fs._system_code_note(src), "")   # said once per file

    def test_ordinary_code_gets_no_note(self):
        src = self.root / "calc.py"
        src.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        self.assertEqual(fs._system_code_note(src), "")


if __name__ == "__main__":
    unittest.main()
