"""Every change on disk is shown, and a turn's time survives /history.

Edit tools draw their own diff. Files that powershell, py_run, a subagent or an MCP tool create,
change or delete get the same diff card from a before/after snapshot (maxedcode.filewatch). The
"⏱ total …" line of a turn is saved on its last answer and must never reach the server.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

_TEST_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-filewatch-tests-")
os.environ["AGENT_HOME"] = _TEST_HOME.name

from maxedcode import filewatch
from maxedcode.filewatch import FileWatch, named_files, watched_files
from maxedcode.util import wire


def touch(path, text):
    """Write and push the mtime forward: two writes inside one clock tick must still count as a change."""
    old = os.stat(path).st_mtime if os.path.exists(path) else 0
    Path(path).write_text(text, encoding="utf-8")
    # strictly after the previous mtime: setUp and the test may run inside one clock tick (seen on CI)
    t = max(time.time() + 2, old + 1)
    os.utime(path, (t, t))


class FileWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        touch(os.path.join(self.root, "a.py"), "x = 1\ny = 2\n")
        touch(os.path.join(self.root, "gone.txt"), "old\n")
        os.makedirs(os.path.join(self.root, "node_modules"))
        touch(os.path.join(self.root, "node_modules", "dep.js"), "1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_changed_and_deleted_files_are_reported_with_their_text(self):
        w = FileWatch()
        snap = w.before(self.root)
        touch(os.path.join(self.root, "a.py"), "x = 1\ny = 3\n")
        touch(os.path.join(self.root, "sub_new.md"), "hello\n")
        os.remove(os.path.join(self.root, "gone.txt"))
        touch(os.path.join(self.root, "node_modules", "dep.js"), "2")   # skipped folder
        got = {(k, os.path.basename(p)): (old, new) for k, p, old, new in w.diff(self.root, [], snap)}
        self.assertEqual(got[("changed", "a.py")], ("x = 1\ny = 2\n", "x = 1\ny = 3\n"))
        self.assertEqual(got[("new", "sub_new.md")], ("", "hello\n"))
        self.assertEqual(got[("deleted", "gone.txt")], ("old\n", ""))
        self.assertEqual(len(got), 3)

    def test_nothing_changed_reports_nothing(self):
        w = FileWatch()
        snap = w.before(self.root)
        self.assertEqual(w.diff(self.root, [], snap), [])

    def test_a_named_file_outside_the_folder_is_watched(self):
        with tempfile.TemporaryDirectory() as other:
            out = os.path.join(other, "report.txt")
            cmd = f'Set-Content -Path "{out}" -Value hi'
            files = watched_files("powershell", {"command": cmd})
            self.assertEqual(files, [out])
            w = FileWatch()
            snap = w.before(self.root, files)
            touch(out, "hi\n")
            self.assertEqual([(k, p) for k, p, _o, _n in w.diff(self.root, files, snap)], [("new", out)])

    def test_binary_file_has_no_text(self):
        w = FileWatch()
        snap = w.before(self.root)
        Path(self.root, "img.bin").write_bytes(b"\x89PNG\0\0data")
        (kind, _path, _old, new), = w.diff(self.root, [], snap)
        self.assertEqual((kind, new), ("new", None))

    def test_a_huge_folder_is_not_walked_again(self):
        w = FileWatch()
        old = filewatch.MAX_FILES
        filewatch.MAX_FILES = 1
        try:
            self.assertEqual(w.before(self.root), {})
            self.assertIn(os.path.abspath(self.root), w.too_big)
        finally:
            filewatch.MAX_FILES = old

    def test_which_tools_are_watched(self):
        self.assertIsNone(watched_files("read_file", {"path": "x"}))
        self.assertIsNone(watched_files("edit_file", {"path": "x"}))       # draws its own diff
        self.assertIsNone(watched_files("powershell", {"command": "Get-ChildItem"}))
        self.assertEqual(watched_files("py_run", {"code": "print(1)"}), [])
        self.assertEqual(watched_files("subagent", {"prompt": "x"}), [])
        self.assertEqual(watched_files("mcp__fs__write", {}), [])

    def test_named_files_parses_quoted_and_bare_paths(self):
        self.assertEqual(named_files(r"python C:\w\run.py > 'D:\out dir\log.txt'"),
                         [r"C:\w\run.py", r"D:\out dir\log.txt"])


class ScratchNoteTests(unittest.TestCase):
    def test_new_project_files_get_one_cleanup_hint(self):
        root = r"C:\w\proj"
        changes = [("new", r"C:\w\proj\_t1.bat", "", None), ("new", r"C:\w\proj\.coverage", "", None),
                   ("new", r"C:\other\out.txt", "", None), ("changed", r"C:\w\proj\start.bat", "a", "b")]
        note = filewatch.scratch_note(changes, root)
        self.assertIn("_t1.bat", note)
        self.assertNotIn(".coverage", note)
        self.assertNotIn("out.txt", note)
        self.assertNotIn("start.bat", note)
        self.assertIn("%TEMP%", note)
        self.assertEqual(filewatch.scratch_note([("changed", r"C:\w\proj\a.py", "a", "b")], root), "")
        # Live UI run: the user asked for log.txt; it is the deliverable, not a probe.
        self.assertEqual(filewatch.scratch_note([("new", r"C:\w\proj\log.txt", "", "done")], root,
                                                "создай рядом log.txt со строкой done"), "")

    def test_headless_tells_the_model_about_files_its_script_left(self):
        import asyncio
        from maxedcode import headless
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as work:
            os.chdir(work)
            try:
                code = "open('_probe.py', 'w').write('print(1)')"
                out = asyncio.run(headless.guarded_exec("py_run", {"code": code}))
                self.assertIn("New file(s) in the project from this call: _probe.py", out)
                out = asyncio.run(headless.guarded_exec("py_run", {"code": "print(2)"}))
                self.assertNotIn("New file(s)", out)
            finally:
                os.chdir(cwd)


class ChangeCardTests(unittest.TestCase):
    def test_card_shows_the_same_diff_as_an_edit(self):
        from maxedcode.ui.widgets import ChangeCard
        card = ChangeCard("powershell", "changed", r"C:\w\a.py", "x = 1\n", "x = 2\n")
        self.assertIn("File changed", card._sel_text)
        self.assertIn("+1 −1", card._sel_text)
        self.assertIn("x = 2", card._sel_text)
        gone = ChangeCard("py_run", "deleted", r"C:\w\b.py", "a\nb\n", "")
        self.assertIn("deleted", gone._sel_text)
        binary = ChangeCard("py_run", "new", r"C:\w\c.bin", "", None)
        self.assertIn("binary or large file", binary._sel_text)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_file_written_by_py_run_gets_a_diff_card_and_turn_time_shows_on_reopen(self):
        import json
        from unittest.mock import AsyncMock, patch
        from rich.text import Text
        from maxedcode.state import S
        from maxedcode.ui import app as app_module
        from maxedcode.ui.widgets import ChangeCard

        hook = {"event": "", "decision": None, "input": None, "context": "", "message": "", "stop": False}
        agent = app_module.AgentApp()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as work, \
             patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=hook)), \
             patch.dict(S, {"auto": True, "mode": "auto"}):
            os.chdir(work)
            try:
                async with agent.run_test(size=(120, 40)):
                    code = "open('made.txt', 'w').write('one\\ntwo\\n')"
                    call = {"id": "c1", "type": "function", "function": {"name": "py_run", "arguments": json.dumps({"code": code})}}
                    answer = {"role": "assistant", "content": "", "tool_calls": [call]}
                    agent.msgs.append(answer)
                    await agent.run_call({"id": "c1", "name": "py_run", "args": json.dumps({"code": code})})
                    cards = list(agent.query(ChangeCard))
                    self.assertEqual(len(cards), 1)
                    self.assertIn("New file", cards[0]._sel_text)
                    self.assertIn("made.txt", cards[0]._sel_text)
                    self.assertIn("+2 −0", cards[0]._sel_text)

                    # Saved on the calling answer: a session reopened from /history shows the same card.
                    self.assertEqual(answer["file_changes"]["c1"][0][:2], ["new", os.path.join(work, "made.txt")])
                    self.assertNotIn("file_changes", wire([answer])[0])
                    reopened = agent._transcript([answer, {"role": "tool", "tool_call_id": "c1", "content": "ok"}])
                    self.assertTrue(any(isinstance(w, ChangeCard) and "made.txt" in w._sel_text for w in reopened))

                    shown = agent._transcript([{"role": "user", "content": "go"},
                                               {"role": "assistant", "content": "done", "turn_time": "⏱ total 42s"}])
                    self.assertTrue(any("⏱ total 42s" in getattr(w, "_sel_text", "") for w in shown))
            finally:
                os.chdir(cwd)


class TurnTimeTests(unittest.TestCase):
    def test_saved_turn_time_is_never_sent_to_the_server(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "done", "turn_time": "⏱ total 12s"}]
        sent = wire(msgs)
        self.assertNotIn("turn_time", sent[1])
        self.assertEqual(sent[1]["content"], "done")
        self.assertIn("turn_time", msgs[1])    # the saved session keeps it


if __name__ == "__main__":
    unittest.main()
