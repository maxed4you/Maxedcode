"""PowerShell pre-check: multi-line commands, unknown cmdlets/parameters, safe auto-fixes.

Real failures from sessions: multi-line foreach/here-string hung the persistent shell for 120 s;
`Select-First`, `Get-Childir`, `Get-Volume -DrivePath`, `ls -la`, `$env.JAVA_HOME`, `"$d: $n"`
ran half a command and left the model guessing.
"""
import asyncio
import unittest
from unittest.mock import patch

import os
if os.name != "nt":   # Windows PowerShell suite; the bash tool is covered by test_posix_shell.py
    raise unittest.SkipTest("Windows PowerShell suite")

from maxedcode.tools import psprep, runners, shell


def run(cmd, persistent=True):
    with patch.object(shell, "ENABLED", persistent):
        return asyncio.run(asyncio.wait_for(runners.run_ps(cmd), 60))


class AutoFixTests(unittest.TestCase):
    def test_env_dot_is_fixed_outside_single_quotes(self):
        self.assertEqual(psprep.autofix('$env.HOME; "$env.TEMP\\x"')[0], '$env:HOME; "$env:TEMP\\x"')
        self.assertEqual(psprep.autofix("'$env.HOME'")[0], "'$env.HOME'")

    def test_variable_followed_by_colon(self):
        self.assertEqual(psprep.autofix('"$d: $n"')[0], '"${d}: $n"')
        for keep in ("$env:PATH", "$script:x", "$global:y = 1", "$t::Round(1.5)", "'$d: x'"):
            self.assertEqual(psprep.autofix(keep)[0], keep)

    def test_and_and_chain(self):
        self.assertEqual(psprep.autofix("cd x && npm test")[0], "cd x; if ($?) { npm test }")
        self.assertEqual(psprep.autofix("a || b")[0], "a; if (-not $?) { b }")
        # inside a string, a script block or mixed operators: left alone
        for keep in ("'a && b'", "if ($a) { b && c }", "a && b || c"):
            self.assertEqual(psprep.autofix(keep)[0], keep)

    def test_calculated_property_without_key(self):
        # Real M20 run: the same `@{N='MB';={...}}` was refused by the pre-check twice in a row.
        self.assertEqual(psprep.autofix("select Name, @{N='MB';={$_.WS/1MB}}")[0], "select Name, @{N='MB'; E={$_.WS/1MB}}")
        for keep in ("@{N='MB'; E={1}}", "'a;={b}'", '"a;={b}"'):
            self.assertEqual(psprep.autofix(keep)[0], keep)

    def test_here_string_is_not_touched(self):
        cmd = "$t = @'\n$env.X && $d: y\n'@"
        self.assertEqual(psprep.autofix(cmd)[0], cmd)


class ExplainTests(unittest.TestCase):
    def test_unknown_command_gets_a_hint(self):
        msg = psprep.explain(psprep.PRE_MARK + "unknown command: Select-First")
        self.assertIn("NOT run", msg)
        self.assertIn("Select-Object -First", msg)

    def test_bash_flag_gets_a_hint(self):
        msg = psprep.explain(psprep.PRE_MARK + "Remove-Item has no parameter -rf")
        self.assertIn("Remove-Item -Recurse -Force", msg)

    def test_normal_output_is_not_a_precheck_failure(self):
        self.assertIsNone(psprep.explain("hello\nworld"))


class LiveShellTests(unittest.TestCase):
    def test_multiline_runs_in_both_shells(self):
        cmd = "foreach ($i in 1..2) {\n  \"n=$i\"\n}\n$t = @'\nx\n'@\n$t"
        for persistent in (True, False):
            out = run(cmd, persistent)
            self.assertIn("n=2", out)
            self.assertTrue(out.rstrip().endswith("x"), out)

    def test_each_statement_keeps_its_own_columns(self):
        # Real S2 run: `gci | select Name,Length; gci -Recurse | select FullName` printed the second
        # list as blank rows under the first table's columns, so the model searched again.
        cmd = ("[pscustomobject]@{Name='a.txt'; Length=1} | Select-Object Name,Length; "
               "[pscustomobject]@{FullName='C:\\x\\b.txt'} | Select-Object FullName; Get-Item C:\\nope_123")
        for persistent in (True, False):
            out = run(cmd, persistent)
            self.assertIn("a.txt", out)
            self.assertIn("C:\\x\\b.txt", out)
            self.assertNotIn("Out-Default", out)

    def test_silenced_errors_are_not_a_failure(self):
        # Real S2/M12 runs: `Get-Command a, b -ErrorAction SilentlyContinue` came back as
        # "exited with code 1" and the model re-checked a result it already had.
        for cmd in ("Get-Command nopeprog123, Get-Date -ErrorAction SilentlyContinue | Select-Object Name",
                    "Get-Item C:\\nope_123 2>$null", "Get-ChildItem C:\\nope_123 -ea 0 | Select-Object Name"):
            self.assertNotIn("exited with code", run(cmd), cmd)
        self.assertIn("exited with code 1", run("Get-Item C:\\nope_123"))
        self.assertIn("exited with code 1", run("Get-Item C:\\nope_123 -ea 0 | Remove-Item"))
        self.assertIn("exited with code 2", run("cmd /c exit 2 2>$null"))

    def test_unknown_cmdlet_runs_nothing(self):
        out = run("Set-Variable -Name __maxedcode_probe -Value 1 -Scope Global; Select-First 3")
        self.assertIn("NOT run", out)
        self.assertIn("Select-Object -First", out)
        self.assertNotIn("1", run("$global:__maxedcode_probe"))

    def test_probing_for_a_program_is_not_rejected(self):
        # Real M1 run: `if (Get-Command ollama ...) { ollama --version }` was refused as "unknown command".
        guarded = run("if (Get-Command nopeprog123 -ErrorAction SilentlyContinue) { nopeprog123 --version } else { 'missing' }")
        self.assertEqual(guarded.strip(), "missing")
        tried = run("try { nopeprog123 --version } catch { 'no prog' }")
        self.assertEqual(tried.strip(), "no prog")
        quiet = run("nopeprog123 --version 2>$null; 'after'")
        self.assertIn("after", quiet)
        self.assertNotIn("NOT run", quiet)

    def test_unknown_parameter_and_bash_flag(self):
        self.assertIn("-DrivePath", run("Get-Volume -DrivePath C"))
        self.assertIn("Get-ChildItem -Force", run("ls -la"))

    def test_syntax_error_is_reported_with_the_line(self):
        out = run("$m = @{pi='a'; gemini-cli='b'}")
        self.assertIn("syntax error", out)
        self.assertIn("quote hashtable keys", out)

    def test_background_command_is_prechecked(self):
        with patch.object(shell, "ENABLED", True):
            out = runners.bg_start("Select-First 3")
        self.assertIn("NOT run", out)
        self.assertNotIn("Background id", out)

    def test_state_and_exit_codes(self):
        run("$__maxedcode_keep = 42")
        self.assertEqual(run('"v=$__maxedcode_keep"').strip(), "v=42")
        self.assertIn("exited with code 3", run('python -c "import sys; sys.exit(3)"'))
        self.assertIn("exited with code 1", run("throw 'boom'"))
        self.assertIn("quiet ok", run("Get-Item 'Z:\\no\\such' -ErrorAction SilentlyContinue; 'quiet ok'"))
        self.assertIn("exited with code 5", run("if ($true) { exit 5 }"))
        self.assertEqual(run("Write-Output 'Привет'").strip(), "Привет")

    def test_piped_stdin_has_no_bom_and_keeps_cyrillic(self):
        # Real LCB run: `"4 4" | python solution.py` gave the program "﻿4 4", the int() parse failed on a
        # correct solution, and the model spent three steps working around PowerShell.
        cmd = "'4 4 й' | python -c \"import sys; print(sys.stdin.buffer.read().hex())\""
        for persistent in (True, False):
            self.assertEqual(run(cmd, persistent).strip(), "34203420d0b90d0a", persistent)   # "4 4 й\r\n"


if __name__ == "__main__":
    unittest.main()
