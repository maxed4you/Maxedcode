"""Tool results stay short: every new token of a result is prefill time on the local server.

Measured on Strata (2026-10-27): a new prompt token cost ~13.5 ms, more than a generated one (~10.8 ms),
so absolute paths in headers, echoed excerpts and "OK" validation lines were a large part of each step.
"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode import config
from maxedcode.tools import fs, runners, undo


class ShortResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-size-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "pkg" / "sample.py"
        self.path.parent.mkdir()
        self.path.write_text("def f():\n    return 1\n", encoding="utf-8")
        self.cwd = contextlib.chdir(self.root)
        self.cwd.__enter__()
        self.addCleanup(self.cwd.__exit__, None, None, None)
        snap = patch.object(undo, "snap")
        snap.start()
        self.addCleanup(snap.stop)

    def test_paths_inside_the_working_directory_are_shown_relative(self):
        self.assertEqual(fs.shown(str(self.path)), str(Path("pkg") / "sample.py"))
        self.assertEqual(fs.shown(str(self.root)), str(self.root))          # the folder itself stays absolute
        outside = Path(tempfile.gettempdir()).resolve() / "elsewhere.txt"
        self.assertEqual(fs.shown(str(outside)), str(outside))
        self.assertTrue(fs.tool_read(str(self.path)).startswith(f"[{Path('pkg') / 'sample.py'}: lines 1-2 of 2.]"))

    def test_exact_edit_and_patch_answer_in_one_line(self):
        result = fs.tool_edit(str(self.path), "return 1", "return 2")
        self.assertEqual(result, f"OK, edited {Path('pkg') / 'sample.py'}.")
        result = fs.tool_patch(str(self.path), [{"old_text": "return 2", "new_text": "return 3"}])
        self.assertEqual(result, f"OK, applied 1 patch block(s) to {Path('pkg') / 'sample.py'}.")

    def test_tolerant_match_still_shows_what_landed(self):
        result = fs.tool_edit(str(self.path), "def f():\n  return 1", "def f():\n  return 2")
        self.assertIn("indentation adjusted", result)
        self.assertIn("now:", result)
        landed = self.path.read_text(encoding="utf-8").splitlines()[1]
        self.assertIn(f"2: {landed}", result)              # the excerpt is the file as written, not new_text

    def test_python_check_reports_only_problems(self):
        with patch.object(fs.shutil, "which", return_value=None), \
             patch.object(fs.importlib.util, "find_spec", return_value=None):
            self.assertEqual(fs._python_check(self.path), "")
        self.path.write_text("def f(:\n", encoding="utf-8")
        self.assertIn("FAILED", fs._python_check(self.path))

    def test_unread_stub_is_replaced_and_its_old_text_shown(self):
        result = fs.tool_write(str(self.path), "def f():\n    return 42\n")
        self.assertTrue(result.startswith("OK, wrote 3 lines"), result)
        self.assertIn("had not read", result)
        self.assertIn("return 1", result)                                   # the replaced stub
        self.assertEqual(self.path.read_text(encoding="utf-8"), "def f():\n    return 42\n")
        self.assertNotIn("had not read", fs.tool_write(str(self.path), "x = 1\n"))   # now it is known

    def test_unread_big_file_is_still_refused(self):
        self.path.write_text("x = 1\n" * 1000, encoding="utf-8")
        self.assertIn("was not read in this session", fs.tool_write(str(self.path), "x = 2\n"))

    def test_a_whole_test_file_is_read_in_one_result(self):
        big = self.root / "big_test.py"
        big.write_text("".join(f"    def test_{i}(self):\n        self.assertEqual(f({i}), {i})\n"
                               for i in range(200)), encoding="utf-8")         # ~12 KB, ~14 KB with numbers
        self.assertGreater(len(big.read_text(encoding="utf-8")), config.TOOL_OUTPUT_LIMIT)
        with patch.object(config, "CTX_WINDOW", 65536):
            out = fs._tool_read_uncached(str(big))
            self.assertLessEqual(len(out), config.output_limit("read_file"))
        self.assertIn("lines 1-400 of 400.]", out)
        self.assertNotIn("Continue", out)

    def test_a_too_long_read_is_cut_by_lines_and_points_at_the_real_file(self):
        big = self.root / "huge.py"
        big.write_text("".join(f"x_{i} = {'a' * 60}\n" for i in range(2000)), encoding="utf-8")
        with patch.object(config, "CTX_WINDOW", 32768):
            out = fs._tool_read_uncached(str(big))
            self.assertLessEqual(len(out), config.output_limit("read_file"))
        header = out.split("\n", 1)[0]
        last = int(header.split("lines 1-")[1].split(" ")[0])
        self.assertIn(f"Continue: start_line={last + 1}.", header)
        self.assertTrue(out.rstrip().endswith(f"{last}: x_{last - 1} = " + "a" * 60))
        self.assertNotIn("tool-output", out)

    def test_logs_keep_their_tail(self):
        log = self.root / "run.log"
        log.write_text("start\n" + "step ok\n" * 1500 + "FINAL ERROR\n", encoding="utf-8")   # > read limit numbered
        with patch.object(config, "CTX_WINDOW", 32768):
            self.assertIn("FINAL ERROR", fs._tool_read_uncached(str(log)))   # the call site shows head + tail

    def test_read_limit_follows_the_window(self):
        with patch.object(config, "CTX_WINDOW", 32768):
            self.assertEqual(config.output_limit("read_file"), config.TOOL_OUTPUT_LIMIT)
        with patch.object(config, "CTX_WINDOW", 65536):
            self.assertGreater(config.output_limit("read_file"), config.TOOL_OUTPUT_LIMIT)
        with patch.object(config, "CTX_WINDOW", 1_000_000):
            self.assertEqual(config.output_limit("read_file"), 24_000)
        self.assertEqual(config.output_limit("powershell"), config.TOOL_OUTPUT_LIMIT)


class NativeStderrRecordTests(unittest.TestCase):
    def test_native_stderr_record_becomes_its_line(self):
        body = ("python : ....E.\nAt line:1 char:1\n+ python -m unittest t 2>&1 | Select-Object -First 5\n+ ~~~~~~\n"
                "    + CategoryInfo          : NotSpecified: (....E.:String) [], RemoteException\n"
                "    + FullyQualifiedErrorId : NativeCommandError\n\n=====\nERROR: test_x")
        self.assertEqual(runners.strip_native_records(body), "....E.\n\n=====\nERROR: test_x")

    def test_powershell_errors_are_kept(self):
        body = ("Get-Item : Cannot find path 'C:\\nope' because it does not exist.\nAt line:1 char:1\n+ Get-Item C:\\nope\n"
                "+ ~~~~~~~~~~~~~~~\n    + CategoryInfo          : ObjectNotFound: (C:\\nope:String) [Get-Item], ItemNotFoundException\n"
                "    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetItemCommand\n")
        self.assertEqual(runners.strip_native_records(body), body)


if __name__ == "__main__":
    unittest.main()
