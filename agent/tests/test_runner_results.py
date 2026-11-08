import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode.tools import runners


class _FakeStream:
    def __init__(self, data, block):
        self.data, self.block = data, block

    async def read(self, n):
        if self.data:
            data, self.data = self.data, b""
            return data
        if self.block:
            await asyncio.Event().wait()
        return b""


class _FakeProcess:
    def __init__(self, *, block=False, output=b"ok", code=0):
        self.returncode = None if block else code
        self.pid = 12345
        self.output = output
        self.block = block
        self.killed = False
        self.stdout = _FakeStream(output, block)

    async def communicate(self):
        if self.block:
            await asyncio.Event().wait()
        return self.output, b""

    async def wait(self):
        if self.block:
            await asyncio.Event().wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def poll(self):
        return self.returncode


class RunnerResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_snippets_use_distinct_scratch_files(self):
        created = []
        async def fake_spawn(*args, **kwargs):
            path = Path(args[1])
            content = path.read_text(encoding="utf-8")
            created.append((path, content))
            return _FakeProcess(output=content.encode())
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn):
            first, second = await asyncio.gather(runners.run_py("print('a')"), runners.run_py("print('b')"))
        self.assertNotEqual(created[0][0], created[1][0])
        self.assertIn("print('a')", first)
        self.assertIn("print('b')", second)
        self.assertFalse(created[0][0].exists())
        self.assertFalse(created[1][0].exists())

    async def test_colored_traceback_reaches_model_and_card_without_escape_codes(self):
        # FORCE_COLOR from the TUI makes Python 3.13+ color tracebacks; raw ESC codes broke the card frame.
        colored = b'  File "x.py", line 6, in \x1b[35m<module>\x1b[0m\n\x1b[1;31m~~~^^^\x1b[m\n'
        async def fake_spawn(*args, **kwargs):
            return _FakeProcess(output=colored, code=1)
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn):
            result = await runners.run_py("raise SystemExit(1)")
        self.assertNotIn("\x1b", result)
        self.assertIn("in <module>\n~~~^^^", result)
        from maxedcode.ui.widgets import Card
        card = Card("py_run", {"code": "x"})
        card.finish(False, colored.decode())
        self.assertNotIn("\x1b", card.res)

    async def test_cancelling_python_runner_kills_child_process(self):
        child = _FakeProcess(block=True)
        async def fake_spawn(*args, **kwargs):
            return child
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(runners, "_kill", side_effect=lambda p: p.kill()):
            task = asyncio.create_task(runners.run_py("while True: pass"))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(child.killed)

    async def test_python_timeout_keeps_partial_output_and_says_what_to_do(self):
        # Real case (LCB 3748, abc393_e): a bare "timeout" made the model rerun the same heavy brute-force check.
        async def run(output):
            child = _FakeProcess(block=True, output=output)
            async def fake_spawn(*args, **kwargs):
                return child
            with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
                 patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
                 patch.object(runners, "_kill", side_effect=lambda p: p.kill()), \
                 patch.object(runners, "PY_TIMEOUT", 0.05):
                result = await runners.run_py("check()")
            self.assertTrue(child.killed)
            return result
        silent = await run(b"")
        self.assertTrue(silent.startswith("Error: timeout"), silent)
        self.assertIn("Nothing was printed", silent)
        self.assertIn("Do not rerun it as is", silent)
        partial = await run(b"case 1 ok\ncase 2 ok\n")
        self.assertIn("case 2 ok", partial)
        self.assertIn("partial output above", partial)

    async def test_main_loop_snippet_moves_to_the_background_on_timeout(self):
        # Like dsh's promote-on-timeout: a rerun from scratch cost another full minute (LCB 3748, abc393_e).
        class Stream:
            def __init__(self):
                self.q = asyncio.Queue()

            async def read(self, n):
                return await self.q.get()

        class Proc:
            def __init__(self):
                self.pid, self.returncode, self.stdout, self.done = 4321, None, Stream(), asyncio.Event()

            async def wait(self):
                await self.done.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.done.set()

        child = Proc()
        child.stdout.q.put_nowait(b"case 1 ok\n")

        async def fake_spawn(*args, **kwargs):
            return child
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(runners, "_kill", side_effect=lambda p: self.fail("a promoted snippet was killed")), \
             patch.object(runners, "PY_TIMEOUT", 0.05):
            runners.BG.clear(); runners.REPORTED.clear(); runners.BG_NOTIFICATIONS.clear()
            result = await runners.run_py("check()", promote=True)
            hid = result.split("background id ")[1].split("]")[0]
            self.assertIn("case 1 ok", result)
            self.assertIn(f'bg_kill("{hid}")', result)
            self.assertIn("still running", runners.bg_output(hid))
            child.stdout.q.put_nowait(b"case 2 ok\n")
            child.stdout.q.put_nowait(b"")
            child.returncode = 0
            child.done.set()
            out = await runners.bg_wait(hid, 2)
            self.assertIn("finished, exit code 0", out)
            self.assertIn("case 1 ok", out)
            self.assertIn("case 2 ok", out)
            runners.BG.clear(); runners.REPORTED.clear(); runners.BG_NOTIFICATIONS.clear()
            runners.BG_LIMIT.clear(); runners.BG_READERS.clear(); runners.BG_STARTED.clear()

    async def test_silent_main_loop_snippet_is_killed_not_moved_to_the_background(self):
        # LCB abc395_e: snippets silent for the whole minute never finished; waiting on them cost ~480 s.
        child = _FakeProcess(block=True, output=b"")
        async def fake_spawn(*args, **kwargs):
            return child
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(runners, "_kill", side_effect=lambda p: p.kill()), \
             patch.object(runners, "PY_TIMEOUT", 0.05):
            runners.BG.clear()
            result = await runners.run_py("check()", promote=True)
            self.assertFalse(runners.BG)
        self.assertTrue(child.killed)
        self.assertTrue(result.startswith("Error: timeout"), result)
        self.assertIn("Nothing was printed", result)

    async def test_finished_snippet_with_a_child_holding_stdout_does_not_wait_forever(self):
        # The snippet exits, a process it started keeps the pipe open: the read must stay under the deadline.
        child = _FakeProcess(output=b"parent done\n", code=0)
        child.stdout = _FakeStream(b"parent done\n", block=True)
        async def fake_spawn(*args, **kwargs):
            return child
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(runners, "_kill", side_effect=lambda p: None), \
             patch.object(runners, "PY_TIMEOUT", 0.05):
            result = await asyncio.wait_for(runners.run_py("start_server()"), 5)
        self.assertIn("parent done", result)
        self.assertIn("The code finished (exit code 0)", result)
        self.assertNotIn("Error: timeout", result)

    async def test_spawn_oserror_is_not_reported_as_exit_code(self):
        with patch.object(runners.shell, "ENABLED", False), \
             patch.object(runners.asyncio, "create_subprocess_exec", side_effect=OSError("not installed")):
            result = await runners.run_ps("Write-Output hi")
        self.assertIn("could not start PowerShell", result)
        self.assertNotIn("exited with code", result)

    async def test_background_job_timeout_marks_killed(self):
        class BgProc:
            pid = 123
            returncode = None
            def poll(self): return self.returncode
            def kill(self): self.returncode = -9
        proc = BgProc()
        with tempfile.TemporaryDirectory() as tmp, patch.object(runners, "HOME", Path(tmp)), \
             patch.object(runners.subprocess, "Popen", return_value=proc), \
             patch.object(runners.shell, "ENABLED", False), \
             patch.object(runners, "BG_TIMEOUT", 0.001):
            runners.BG.clear(); runners.BG_STARTED.clear(); runners.KILLED.clear()
            started = runners.bg_start("slow command")
            task_id = started.split()[2].rstrip(":")
            # started long ago, not sleep(0.01): a coarse clock (~15 ms ticks on the CI runner) read 0 elapsed
            for hid in runners.BG_STARTED:
                runners.BG_STARTED[hid] -= 10
            result = runners.bg_output(task_id)
        self.assertIn("killed", result)
        self.assertTrue(proc.returncode == -9)
        runners.BG.clear(); runners.BG_STARTED.clear(); runners.KILLED.clear()


class SystemChangeNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_that_changes_user_path_is_reported(self):
        states = iter([({"path": (r"C:\a", 2)}, set()), ({"path": (r"C:\a;C:\tmp\x", 2)}, {r"D:\Desk\app.lnk"})])
        with patch.object(runners, "system_state", lambda: next(states)), \
                patch.object(runners, "_run_py", return_value="ok"):
            out = await runners.run_py("print(1)")
        self.assertIn("[system change]", out)
        self.assertIn(r"+C:\tmp\x", out)
        self.assertIn("desktop +app.lnk", out)

    async def test_unchanged_system_adds_nothing(self):
        state = ({"path": (r"C:\a", 2)}, set())
        with patch.object(runners, "system_state", lambda: state), patch.object(runners, "_run_ps", return_value="ok"):
            self.assertEqual(await runners.run_ps("Get-Date"), "ok")

    def test_flattened_path_is_reported(self):
        before = ({"path": (r"%USERPROFILE%\bin", 2)}, set())
        with patch.object(runners, "system_state", lambda: ({"path": (r"%USERPROFILE%\bin", 1)}, set())):
            self.assertIn("value type", runners.system_note(before))


if __name__ == "__main__":
    unittest.main()
