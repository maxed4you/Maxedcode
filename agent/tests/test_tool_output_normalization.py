import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode import memory
from maxedcode.util import attach_late, normalize_tool_output, tool_content
from maxedcode.tools import fs
from maxedcode.tools.fs import tool_glob, tool_read, tool_write, tool_grep
from maxedcode.tools.web import _clean


class ToolOutputNormalizationTests(unittest.TestCase):
    def test_large_result_is_saved_and_readable_under_protected_temp_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            target_dir = Path(temp)
            with patch("maxedcode.util.tool_output_dir", return_value=target_dir), \
                 patch("maxedcode.tools.fs.tool_output_dir", return_value=target_dir), \
                 patch("maxedcode.tools.fs.permissions.protected", side_effect=lambda _p, **_kw: "AppData protected"):
                original = "head\n" + "body-line\n" * 1500 + "TAIL-SENTINEL"
                preview = normalize_tool_output(original, limit=2000)
                files = list(target_dir.glob("tool-output-*.txt"))
                self.assertEqual(len(files), 1)
                self.assertIn("complete output saved", preview)
                self.assertIn("TAIL-SENTINEL", tool_read(str(files[0])))
                self.assertIn("TAIL-SENTINEL", tool_grep("TAIL-SENTINEL", str(files[0])))
                self.assertIn("protected path", tool_write(str(files[0]), "overwrite"))

    def test_untrusted_files_under_same_directory_are_not_read_exempted(self):
        with tempfile.TemporaryDirectory() as temp:
            target_dir = Path(temp)
            untrusted = target_dir / "notes.txt"
            untrusted.write_text("secret", encoding="utf-8")
            with patch("maxedcode.tools.fs.tool_output_dir", return_value=target_dir), \
                 patch("maxedcode.tools.fs.permissions.protected", side_effect=lambda _p, **_kw: "AppData protected"):
                self.assertIn("protected path", tool_read(str(untrusted)))


class FileMaskTests(unittest.TestCase):
    """Real sessions: grep glob="*.{md,json,ts}" answered "No matches." 13 times (ripgrep not installed, the python
    fallback used fnmatch, which knows neither braces nor lists)."""

    def test_brace_and_list_masks_are_expanded(self):
        self.assertEqual(fs.name_patterns("*.{md,json}"), ["*.md", "*.json"])
        self.assertEqual(fs.name_patterns("*.py,*.ts"), ["*.py", "*.ts"])
        self.assertEqual(fs.name_patterns("src/**/*.{cpp,h}"), ["src/**/*.cpp", "src/**/*.h"])
        self.assertEqual(fs.name_patterns("New folder/*.py"), ["New folder/*.py"])

    def test_grep_and_glob_find_files_through_such_masks(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.md", "b.json", "c.py", "sub/d.ts"):
                p = Path(tmp, name)
                p.parent.mkdir(exist_ok=True)
                p.write_text("needle here\n", encoding="utf-8")
            with patch.object(fs.shutil, "which", return_value=None):      # the python fallback
                found = tool_grep("needle", tmp, glob="*.{md,json,ts}", output_mode="files_with_matches")
                listed = tool_grep("needle", tmp, glob="*.py,*.ts", output_mode="files_with_matches")
            self.assertIn("a.md", found)
            self.assertIn("d.ts", found)
            self.assertNotIn("c.py", found)
            self.assertIn("c.py", listed)
            self.assertNotIn("a.md", listed)
            globbed = tool_glob("**/*.{ts,md}", tmp)
            self.assertIn("d.ts", globbed)
            self.assertIn("a.md", globbed)
            self.assertNotIn("b.json", globbed)


class CompactToolOutputTests(unittest.TestCase):
    """Smaller results for the same information, and images the model can actually see."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.open = patch("maxedcode.tools.fs.permissions.protected", return_value=None)
        self.open.start()
        fs.clear_read_cache()

    def tearDown(self):
        self.open.stop()
        self.temp.cleanup()

    def test_grep_prints_each_path_once(self):
        (self.root / "a.py").write_text("x = 1\nTARGET = 2\ny = 3\nTARGET = 4\n", encoding="utf-8")
        (self.root / "b.py").write_text("TARGET = 5\n", encoding="utf-8")
        with patch("maxedcode.tools.fs.shutil.which", return_value=None):
            out = tool_grep("TARGET", str(self.root), "*.py", 1)
        self.assertEqual(out.count(str(self.root / "a.py")), 1, out)
        self.assertIn("2: TARGET = 2", out)
        self.assertEqual(out.count("3- y = 3"), 1, out)          # overlapping context is shown once
        self.assertIn(str(self.root / "b.py") + "\n1: TARGET = 5", out)

    def test_rg_heading_output_is_grouped(self):
        lines = ["C:\\p\\a.py", "1:import x", "2-ctx", "--", "9:import y", "", "C:\\p\\b.py", "3:import z"]
        self.assertEqual(fs._rg_grouped(lines, 100),
                         "C:\\p\\a.py\n1: import x\n2- ctx\n9: import y\nC:\\p\\b.py\n3: import z")
        self.assertIn("first 2 matches", fs._rg_grouped(lines, 2))

    def test_image_read_attaches_the_picture(self):
        img = self.root / "shot.png"
        img.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4b10000000049454e44ae426082"))
        with patch("maxedcode.tools.fs.NO_VISION", False), patch("maxedcode.util.NO_VISION", False):
            out = tool_read(str(img))
            self.assertTrue(out.startswith("[image "), out)
            self.assertIn("no picture", out)                    # the bare stub never passes for the content
            content = tool_content("read_file", {"path": str(img)}, out)
            self.assertIsInstance(content, list)
            self.assertEqual(content[1]["type"], "image_url")
            self.assertNotIn("no picture", content[0]["text"])
            self.assertEqual(tool_content("grep", {"path": str(img)}, out), out)
            # glob must not inline the stub as if it were the content
            self.assertIn("call read_file", tool_glob("shot.png", str(self.root)))
        msgs = [{"role": "tool", "content": content}]
        attach_late(msgs, ["DOC"])
        self.assertEqual(len(msgs), 1)
        self.assertIn("DOC", msgs[0]["content"][0]["text"])

    def test_vendored_folder_docs_are_not_injected(self):
        (self.root / "AGENTS.md").write_text("root rules", encoding="utf-8")
        vendored = self.root / "third_party" / "lib"
        vendored.mkdir(parents=True)
        (vendored / "AGENTS.md").write_text("vendor rules", encoding="utf-8")
        docs = [body for _f, body in memory.docs_for(str(vendored / "x.c"), self.root)]
        self.assertIn("root rules", docs)
        self.assertNotIn("vendor rules", docs)

    def test_page_text_keeps_rows_and_drops_navigation(self):
        page = ("<html><head><title>T</title><style>x{}</style></head><body><nav>menu menu</nav>"
                "<main><h1>Prices</h1><table><tr><td>a</td><td></td><td>1</td></tr></table>"
                + "<p>" + "body text " * 200 + "</p></main><footer>foot</footer></body></html>")
        text = _clean(page)
        self.assertTrue(text.startswith("T\nPrices\na | 1 |"), text[:80])
        self.assertNotIn("menu", text)
        self.assertNotIn("foot", text)


if __name__ == "__main__":
    unittest.main()
