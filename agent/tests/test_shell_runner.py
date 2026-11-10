"""Regression coverage for persistent PowerShell failure handling."""
import asyncio
import ast
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import AsyncMock, patch

import os
if os.name != "nt":   # Windows PowerShell suite; the bash tool is covered by test_posix_shell.py
    raise unittest.SkipTest("Windows PowerShell suite")

from maxedcode.tools import runners, shell


class _Stdin:
    def __init__(self):
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        pass


class _DeadProcess:
    def __init__(self):
        self.stdin = _Stdin()

    def poll(self):
        return 1

    def kill(self):
        pass


class PersistentShellTests(unittest.TestCase):
    def test_dead_shell_returns_immediately_instead_of_waiting_for_timeout(self):
        instance = shell.Shell()
        instance.alive = True
        instance.proc = _DeadProcess()
        instance.out = Queue()
        instance.err = Queue()
        instance.out.put(None)  # equivalent to stdout EOF from the pump thread

        started = time.monotonic()
        result = instance.run("python missing.py", timeout=120)
        elapsed = time.monotonic() - started

        self.assertEqual(result[0], shell.Shell.RUN_LOST)
        self.assertLess(elapsed, 0.5)
        self.assertIn("ended before its completion marker", result[1])

    def test_persistent_wrapper_records_nonzero_status_without_exit(self):
        async def exercise():
            with patch.object(shell, "ENABLED", True), \
                 patch.object(shell, "protected", return_value=False), \
                 patch.object(shell.SHELL, "run", return_value=(2, "command error", "")) as run:
                answer = await runners.run_ps("python missing.py")
                wrapper = run.call_args.args[0]
                self.assertIn("$global:__maxedcode_run_exit", wrapper)
                self.assertNotIn("exit [int]$__maxedcode_native_exit", wrapper)
                self.assertNotIn("exit 1", wrapper)
                self.assertIn("exited with code 2", answer)

        asyncio.run(exercise())

    def test_native_stderr_with_exit_code_zero_is_success(self):
        # PS 5.1 turns `native 2>&1` stderr lines into NativeCommandError and sets $? = false:
        # a passing unittest run (it reports on stderr) used to come back as "exited with code 1".
        with patch.object(shell, "ENABLED", False):
            passing = asyncio.run(runners.run_ps('python -c "import sys; sys.stderr.write(\'ok\\n\')" 2>&1'))
            failing = asyncio.run(runners.run_ps('python -c "import sys; sys.exit(2)" 2>&1'))
            cmdlet = asyncio.run(runners.run_ps("Get-Item 'Z:\\no\\such.txt' 2>&1"))
        self.assertFalse(passing.startswith("Error"), passing)
        self.assertIn("exited with code 2", failing)
        self.assertIn("exited with code 1", cmdlet)

    def test_nested_powershell_bypasses_the_persistent_shell(self):
        # A child powershell reads the live shell's stdin and swallows the completion marker (120 s hang).
        self.assertTrue(shell.nested_shell("powershell -NoProfile -File .\\run.ps1 -Auto"))
        self.assertTrue(shell.nested_shell("cd x; pwsh.exe -c 'gci'"))
        self.assertFalse(shell.nested_shell("Get-Content powershell_notes.txt"))
        self.assertFalse(shell.nested_shell(".\\run.ps1 -Auto"))
        # cmd and batch files read the same pipe: `set /p` / `pause` hang until the timeout (real T3 run).
        self.assertTrue(shell.nested_shell('cmd /c "C:\\t\\menu.bat"'))
        self.assertTrue(shell.nested_shell("Set-Location C:\\t; .\\start.bat ask"))
        self.assertTrue(shell.nested_shell("& 'C:\\t\\build.cmd'"))
        self.assertFalse(shell.nested_shell("Get-Content C:\\t\\notes.txt"))
        self.assertFalse(shell.nested_shell("git status; cmdlet-notes"))

        async def exercise():
            with patch.object(shell, "ENABLED", True), \
                 patch.object(shell.SHELL, "run") as live, \
                 patch.object(runners.asyncio, "create_subprocess_exec",
                              AsyncMock(side_effect=OSError("one-shot used"))):
                answer = await runners.run_ps("powershell -NoProfile -File x.ps1")
                live.assert_not_called()
                self.assertIn("one-shot used", answer)

        asyncio.run(exercise())

    def test_native_child_reads_eof_instead_of_the_shell_protocol_pipe(self):
        # Real case (LCB abc393_e): `"5 2" | Measure-Command { python sol.py }` — Measure-Command does not
        # forward the pipeline, python read the live shell's command pipe, hung 120 s and ate later commands.
        from maxedcode.tools import psprep
        live = shell.Shell()
        try:
            # Through the real wrapper: it resets [Console]::InputEncoding on every command, which must not
            # make the shell itself read commands from NUL.
            def run(cmd):
                return live.run(psprep.wrap(cmd), timeout=20)
            code_line = 'python -c "import sys; print(repr(sys.stdin.read()))"'
            started = time.monotonic()
            bare = run(code_line)
            measured = run('"5 2" | Measure-Command { python -c "import sys; sys.stdin.read()" } | '
                           'Select-Object -ExpandProperty TotalSeconds')
            piped = run('"5 2" | ' + code_line)
            after = run("$x = 6; $x * 7")
            elapsed = time.monotonic() - started
        finally:
            live.stop()
        self.assertEqual(bare[1].strip(), "''", bare)                # EOF, not the protocol lines
        self.assertGreaterEqual(measured[0], 0, measured)            # finished, not RUN_TIMEOUT
        self.assertIn("5 2", piped[1])                               # explicit pipe input still arrives
        self.assertEqual(after[1].strip(), "42", after)              # later commands are not swallowed
        self.assertLess(elapsed, 30)

    def test_timed_out_command_moves_to_the_background_and_the_next_one_gets_a_new_shell(self):
        # DeepSeek Harness moves a timed-out command to a job; killing it made the model rerun it from scratch.
        async def exercise():
            runners.BG.clear(); runners.REPORTED.clear(); runners.BG_NOTIFICATIONS.clear()
            first = await runners.run_ps("Write-Output start; Start-Sleep 3; Write-Output end", timeout=1, promote=True)
            self.assertIn("NOT stopped", first)
            self.assertIn("start", first)
            hid = first.split("background id ")[1].split("]")[0]
            nxt = await runners.run_ps("Write-Output next-command", timeout=20, promote=True)
            done = await runners.bg_wait(hid, 15)
            listing = await runners.run_ps("Get-ChildItem . -Recurse | Out-Null; Start-Sleep 3", timeout=1, promote=True)
            plain = await runners.run_ps("Start-Sleep 3", timeout=1)        # subagents: stopped as before
            return nxt, done, listing, plain

        try:
            with patch.object(shell, "ENABLED", True), patch.object(shell, "protected", return_value=False):
                nxt, done, listing, plain = asyncio.run(exercise())
        finally:
            shell.SHELL.stop()
            runners.BG.clear(); runners.REPORTED.clear(); runners.BG_NOTIFICATIONS.clear()
            runners.BG_LIMIT.clear(); runners.BG_STARTED.clear()
        self.assertEqual(nxt.strip(), "next-command")              # the shell slot was free at once
        self.assertIn("finished, exit code 0", done)
        self.assertIn("end", done)
        self.assertIn("timed out", listing)                        # a stalled recursive listing is still stopped
        self.assertIn("timed out", plain)

    def test_script_error_reports_the_file_and_line_that_failed(self):
        # Real case: a dot-sourced common.ps1 assigns a string to the caller's [switch]$CleanRun.
        # PowerShell only says "At line:1 char:N"; the wrapper adds where it really failed.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "run.ps1").write_text('[CmdletBinding()]\nparam([switch]$CleanRun)\n. "$PSScriptRoot\\common.ps1"\n"unreachable"\n', encoding="utf-8")
            Path(tmp, "common.ps1").write_text("$ErrorActionPreference = 'Stop'\n$Root = $PSScriptRoot\n$CleanRun = Join-Path $Root 'clean_run'\n", encoding="utf-8")
            Path(tmp, "quiet.ps1").write_text("Remove-Item 'Z:\\no\\such\\dir' -ErrorAction SilentlyContinue\n'done'\n", encoding="utf-8")
            with patch.object(shell, "ENABLED", False):
                answer = asyncio.run(runners.run_ps(f'& "{Path(tmp, "run.ps1")}"'))
                quiet = asyncio.run(runners.run_ps(f'& "{Path(tmp, "quiet.ps1")}"'))
        self.assertIn("exited with code 1", answer)
        self.assertRegex(answer, r"\[error origin\] .*common\.ps1:3: \$CleanRun = Join-Path")
        # A suppressed error is still in $Error; reporting it would send the model after a non-problem.
        self.assertNotIn("[error origin]", quiet)
        self.assertIn("done", quiet)

    def test_timeout_is_not_automatically_retried_in_one_shot_shell(self):
        async def exercise():
            with patch.object(shell, "ENABLED", True), \
                 patch.object(shell, "protected", return_value=False), \
                 patch.object(shell.SHELL, "run", return_value=(shell.Shell.RUN_TIMEOUT, "partial output", "")), \
                 patch.object(runners.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=AssertionError("command was retried"))):
                answer = await runners.run_ps("python script_with_possible_side_effects.py")
                self.assertIn("not automatically retried", answer)
                self.assertIn("partial output", answer)

        asyncio.run(exercise())

    def test_silent_timeout_explains_the_stall_instead_of_a_bare_placeholder(self):
        # Real case: Get-ChildItem -Recurse on a Desktop with a folder named NBSP never ended; the model saw
        # only "timed out" twice, decided the shell was broken and repeated the same listing.
        async def exercise(cmd):
            with patch.object(shell, "ENABLED", True), \
                 patch.object(shell, "protected", return_value=False), \
                 patch.object(shell.SHELL, "run", return_value=(shell.Shell.RUN_TIMEOUT, "persistent PowerShell timed out", "")):
                return await runners.run_ps(cmd)

        listing = asyncio.run(exercise("$f=Get-ChildItem $d -Recurse -File -Force; $f.Count"))
        self.assertEqual(listing.count("timed out"), 1, listing)       # no duplicated placeholder
        self.assertIn("Do not rerun it", listing)
        self.assertIn("py_run (os.walk)", listing)
        with patch.object(shell, "ENABLED", True), \
             patch.object(shell, "protected", return_value=False), \
             patch.object(shell.SHELL, "run", return_value=(shell.Shell.RUN_LOST, "Process is terminated due to StackOverflowException.", "")):
            crashed = asyncio.run(runners.run_ps("Get-ChildItem -LiteralPath $p -Recurse -Force -File"))
        self.assertIn("endless recursion", crashed)                  # smaller trees crash instead of timing out
        self.assertIn("py_run (os.walk)", crashed)
        other = asyncio.run(exercise("Start-Sleep 999"))
        self.assertIn("Do not rerun it", other)
        self.assertNotIn("os.walk", other)
        self.assertTrue(runners.RECURSE.search("gci . -r | measure"))
        self.assertFalse(runners.RECURSE.search("Get-Content x -ReadCount 0"))

    def test_powershell_recurse_loops_on_a_folder_named_nbsp(self):
        # Pins the Windows PowerShell 5.1 behaviour the hint above describes: 3 real files, more per depth level.
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for sub, name in (("normal", "a.txt"), ("\xa0", "b.txt"), ("", "c.txt")):
                Path(tmp, sub).mkdir(exist_ok=True)
                Path(tmp, sub, name).write_text("x", encoding="utf-8")
            self.assertEqual(sum(len(files) for _, _, files in os.walk(tmp)), 3)
            p = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-ChildItem -LiteralPath '{tmp}' -Recurse -File -Depth 3).Count"],
                               capture_output=True, text=True, timeout=60)
            self.assertGreater(int(p.stdout.strip() or 0), 3)

    def test_headless_document_loop_does_not_shadow_flags_dictionary(self):
        path = Path(__file__).parents[1] / "maxedcode" / "headless.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        function = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_impl")
        shadowing = [
            node for node in ast.walk(function)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "f"
            and isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Attribute)
            and node.iter.func.attr == "docs_for"
        ]
        self.assertFalse(shadowing, "docs_for loop must not overwrite the flags() dict stored in f")


if __name__ == "__main__":
    unittest.main()
