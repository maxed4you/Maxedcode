"""Fixes found by comparing with DeepSeek Harness (2026-11-02): files, output, retries, instructions."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from maxedcode import instruction_state, llmretry, memory, skills
from maxedcode.tools import fs, mcp, runners, undo, web


class FileFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-dsh-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        snap = patch.object(undo, "snap")
        snap.start()
        self.addCleanup(snap.stop)
        fs.clear_read_cache()

    def test_write_file_keeps_lf_of_an_existing_file(self):
        p = self.root / "run.sh"
        p.write_bytes(b"echo 1\necho 2\n")
        fs.tool_read(str(p))
        fs.tool_write(str(p), "echo 1\necho 3\n")
        self.assertEqual(p.read_bytes(), b"echo 1\necho 3\n")

    def test_write_file_crlf_content_is_not_doubled(self):
        p = self.root / "new.txt"
        fs.tool_write(str(p), "a\r\nb\r\n")
        self.assertEqual(p.read_bytes(), b"a\r\nb\r\n")

    def test_new_file_gets_platform_line_endings_as_before(self):
        p = self.root / "new2.txt"
        fs.tool_write(str(p), "a\nb\n")
        self.assertEqual(p.read_bytes(), ("a" + os.linesep + "b" + os.linesep).encode())

    def test_one_stray_crlf_does_not_turn_an_lf_file_into_crlf(self):
        p = self.root / "mixed.txt"
        p.write_bytes(b"one\ntwo\nthree\r\nfour\n")
        fs.tool_edit(str(p), "two", "TWO")
        self.assertEqual(p.read_bytes().count(b"\r\n"), 0)
        self.assertIn(b"TWO", p.read_bytes())

    def test_cp1251_file_is_read_and_edited_in_its_encoding(self):
        with patch.object(fs, "ansi_codepage", return_value="cp1251"):
            p = self.root / "old.bat"
            p.write_bytes("echo Привет\r\n".encode("cp1251"))
            shown = fs.tool_read(str(p))
            self.assertIn("Привет", shown)
            self.assertIn("cp1251", shown)
            out = fs.tool_edit(str(p), "Привет", "Пока")
            self.assertTrue(out.startswith("OK"), out)
            self.assertEqual(p.read_bytes(), "echo Пока\r\n".encode("cp1251"))
            refused = fs.tool_edit(str(p), "Пока", "Пока 🙂")
            self.assertIn("cannot be stored in cp1251", refused)
            self.assertEqual(p.read_bytes(), "echo Пока\r\n".encode("cp1251"))

    def test_a_very_long_line_is_cut_in_read_file(self):
        p = self.root / "min.js"
        p.write_text("x" * 10_000 + "\nshort\n", encoding="utf-8")
        out = fs.tool_read(str(p))
        self.assertIn("line cut: 10,000 characters", out)
        self.assertIn("2: short", out)
        self.assertLess(len(out), 3000)

    def test_grep_that_stops_on_the_file_limit_keeps_its_matches(self):
        for i in range(6):
            (self.root / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
        with patch.object(fs, "GREP_FILES_MAX", 3), patch.object(fs.shutil, "which", return_value=None):
            out = fs.tool_grep("needle", str(self.root), output_mode="files_with_matches")
        self.assertIn("f0.txt", out)
        self.assertIn("search stopped after 3 files", out)

    def test_apply_diff_tolerates_indentation_differences(self):
        # The whole hunk is indented 2 spaces less than the file (a small model often loses one level).
        p = self.root / "m.py"
        p.write_text("class A:\n    def f(self):\n        a = 1\n        return a\n", encoding="utf-8")
        diff = "@@ -2,3 +2,3 @@\n   def f(self):\n-      a = 1\n+      a = 2\n       return a\n"
        out = fs.tool_apply_diff(str(p), diff)
        self.assertTrue(out.startswith("OK"), out)
        self.assertIn("indentation adjusted", out)
        self.assertEqual(p.read_text(encoding="utf-8"), "class A:\n    def f(self):\n        a = 2\n        return a\n")


class OutputTests(unittest.TestCase):
    def test_cut_command_output_is_really_saved(self):
        with tempfile.TemporaryDirectory() as d, patch.object(runners, "tool_output_dir", return_value=Path(d)):
            text = "start\n" + "middle line\n" * 2000 + "Traceback: the end\n"
            out = runners._head_tail(text, 4000)
            self.assertLessEqual(len(out), 4000)
            saved = next(Path(d).glob("tool-output-*.txt"))
            self.assertIn(str(saved), out)
            self.assertEqual(saved.read_text(encoding="utf-8").replace("\r\n", "\n"), text)

    def test_pre_blocks_keep_their_indentation(self):
        page = "<p>Example</p><pre><span>def</span> f(x):\n    if x:\n        return 1</pre><p>after</p>"
        text = web._text(page)
        self.assertIn("def f(x):\n    if x:\n        return 1", text)
        self.assertIn("after", text)

    def test_binary_content_types_are_not_decoded_as_text(self):
        self.assertEqual(web._not_text({"Content-Type": "application/pdf"}), "application/pdf")
        self.assertEqual(web._not_text({"Content-Type": "text/html; charset=utf-8"}), "")
        self.assertEqual(web._not_text({"Content-Type": "application/json"}), "")

    def test_mcp_image_is_not_dumped_as_base64(self):
        item = SimpleNamespace(type="image", data="A" * 4000, mimeType="image/png")
        self.assertNotIn("AAAA", mcp._content_text(item))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_server_failure_is_retried_with_the_same_call(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise llmretry.StreamCut("cut")
            return "ok"
        seen = []
        with patch.object(llmretry, "DELAYS", (0,)):
            result = await llmretry.call(fn, lambda e, n, w: seen.append(n))
        self.assertEqual(result, "ok")
        self.assertEqual(seen, [1, 2])

    async def test_other_errors_are_not_retried(self):
        calls = []

        async def fn():
            calls.append(1)
            raise ValueError("refused")
        with self.assertRaises(ValueError):
            await llmretry.call(fn)
        self.assertEqual(len(calls), 1)

    async def test_retries_end(self):
        async def fn():
            raise llmretry.StreamCut("down")
        with patch.object(llmretry, "DELAYS", (0,)), patch.object(llmretry, "RETRIES", 2):
            with self.assertRaises(llmretry.StreamCut):
                await llmretry.call(fn)

    def test_missing_finish_reason_is_a_cut_only_after_the_server_has_sent_one(self):
        with patch.object(llmretry, "FINISH_SEEN", [False]):
            llmretry.stream_ended(None)          # a server that never sends it is not punished
            llmretry.stream_ended("stop")
            with self.assertRaises(llmretry.StreamCut):
                llmretry.stream_ended(None)

    def test_sdk_does_not_retry_on_its_own(self):
        self.assertEqual(llmretry.client_options()["max_retries"], 0)


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-mem-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_compaction_forgets_folded_instructions(self):
        st = instruction_state.InstructionState()
        st.add("doc", "a/AGENTS.md", 5)
        st.add("rule", "r.md", 40)
        st.compacted(30, 3)
        self.assertEqual(st.seen("doc"), set())
        self.assertEqual(st.entries, [("rule", "r.md", 13)])

    def test_both_agents_and_claude_files_are_loaded_import_only_is_skipped(self):
        (self.root / "AGENTS.md").write_text("agents rules", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
        self.assertEqual([f.name for f in memory.instruction_files(self.root)], ["AGENTS.md", "CLAUDE.md"])
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        self.assertEqual([f.name for f in memory.instruction_files(self.root)], ["AGENTS.md"])

    def test_memory_has_one_budget_and_says_what_was_cut(self):
        (self.root / "CLAUDE.md").write_text("x" * 5000, encoding="utf-8")
        with patch.object(memory, "MEMORY_CHARS", 1000), patch.object(memory, "MEM_DIR", self.root / "mem"), \
                patch.object(memory, "HOME", self.root / "home"):
            text = memory.text(self.root)
        self.assertIn("memory budget 1000 characters", text)
        self.assertIn("applies to work under", text)
        self.assertLess(len(text), 2500)

    def test_project_root_is_found_from_a_subfolder(self):
        (self.root / ".git").mkdir()
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(memory.project_root(sub), self.root)

    def test_changed_instruction_file_is_sent_again(self):
        f = self.root / "AGENTS.md"
        f.write_text("new rule", encoding="utf-8")
        self.assertTrue(memory.is_instruction(f))
        self.assertIn("use this content instead", memory.updated_block(f))
        self.assertIn("new rule", memory.updated_block(f))
        self.assertFalse(memory.is_instruction(self.root / "main.py"))

    def test_skill_catalog_says_to_load_before_acting_and_cuts_long_descriptions(self):
        fake = [{"name": "big", "description": "d" * 900}]
        with patch.object(skills, "skills", return_value=fake):
            text = skills.listing()
        self.assertIn("before taking task actions", text)
        self.assertLess(len(text), 1200)


if __name__ == "__main__":
    unittest.main()
