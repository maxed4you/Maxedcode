"""Long commands: per-call timeout, waiting on background tasks, no orphan children after a timeout."""
import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import os
if os.name != "nt":   # Windows PowerShell suite; the bash tool is covered by test_posix_shell.py
    raise unittest.SkipTest("Windows PowerShell suite")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxedcode.tools import runners, shell  # noqa: E402


class FakeProc:
    def __init__(self, finish_after=None):
        self.pid = 1
        self.t0 = time.monotonic()
        self.finish_after = finish_after

    def poll(self):
        if self.finish_after is not None and time.monotonic() - self.t0 >= self.finish_after:
            return 0
        return None


class BgBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name in ("BG", "BG_STARTED"):
            getattr(runners, name).clear()
        for name in ("KILLED", "REPORTED", "POLLED"):
            getattr(runners, name).clear()
        runners.BG_NOTIFICATIONS.clear()

    def tearDown(self):
        runners.BG.clear()
        runners.BG_STARTED.clear()
        self.tmp.cleanup()

    def add(self, hid, proc, text="log line"):
        log = Path(self.tmp.name) / f"{hid}.log"
        log.write_text(text, encoding="utf-8")
        runners.BG[hid] = (proc, log)
        runners.BG_STARTED[hid] = time.monotonic()


class TimeoutTests(unittest.TestCase):
    def test_long_commands_get_a_longer_default(self):
        self.assertEqual(runners.ps_timeout("git clone https://x/y"), 600)
        self.assertEqual(runners.ps_timeout("python -m pip install requests"), 600)
        self.assertEqual(runners.ps_timeout("Invoke-WebRequest -Uri u -OutFile f"), 600)
        self.assertEqual(runners.ps_timeout("Get-ChildItem"), 120)

    def test_model_timeout_is_capped(self):
        self.assertEqual(runners.ps_timeout("Get-ChildItem", 30), 30)
        self.assertEqual(runners.ps_timeout("Get-ChildItem", 5000), 600)
        self.assertEqual(runners.ps_timeout("Get-ChildItem", "bad"), 120)


class WaitTests(BgBase):
    async def test_wait_returns_when_the_task_ends(self):
        self.add("7", FakeProc(finish_after=0.3), "cloned")
        t0 = time.monotonic()
        out = await runners.bg_wait("7", 30)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("finished, exit code 0", out)
        self.assertIn("cloned", out)

    async def test_first_poll_is_instant_second_poll_waits(self):
        self.add("8", FakeProc())
        with patch.object(runners, "POLL_WAIT", 0.4):
            t0 = time.monotonic()
            self.assertIn("still running", await runners.bg_wait("8"))
            self.assertLess(time.monotonic() - t0, 0.3)
            t0 = time.monotonic()
            await runners.bg_wait("8")
            self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    async def test_seen_result_is_not_notified_again(self):
        self.add("9", FakeProc(finish_after=0))
        await runners.bg_wait("9")
        await runners.watch_background("9")
        self.assertEqual(runners.take_bg_notifications(), [])

    async def test_declining_callback_queues_the_notice(self):
        self.add("10", FakeProc(finish_after=0))

        async def busy(_message):
            return False

        await runners.watch_background("10", busy)
        self.assertEqual(len(runners.take_bg_notifications()), 1)

    async def test_sleep_is_cut_short_by_a_finished_task(self):
        self.add("11", FakeProc(finish_after=0.3), "done")
        with patch.object(runners, "system_state", lambda: None):
            t0 = time.monotonic()
            out = await runners.run_ps("Start-Sleep -Seconds 100")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("background id 11 finished", out)

    async def test_sleep_without_background_tasks_runs_normally(self):
        with patch.object(runners, "system_state", lambda: None), \
                patch.object(runners, "_run_ps", return_value="slept") as run:
            self.assertEqual(await runners.run_ps("Start-Sleep 1"), "slept")
        run.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "Windows process tree")
class OrphanTests(unittest.TestCase):
    def test_timeout_kills_child_processes(self):
        sh = shell.Shell()
        with tempfile.TemporaryDirectory() as d:
            pid_file = Path(d) / "pid.txt"
            code_line = f"import os,time; open(r'{pid_file}','w').write(str(os.getpid())); time.sleep(60)"
            try:
                code, _, _ = sh.run(f'& "{sys.executable}" -c "{code_line}"', timeout=4)
                self.assertEqual(code, shell.Shell.RUN_TIMEOUT)
                pid = pid_file.read_text()
                time.sleep(0.5)
                alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
                self.assertNotIn(pid, alive)
            finally:
                sh.stop()

if __name__ == "__main__":
    unittest.main()
