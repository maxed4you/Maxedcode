"""Installer: arrow-key menus, Strata defaults, setup without a running model server."""
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import install

UP, DOWN = "\xe0H", "\xe0P"


class Keys:
    """Stands in for msvcrt: returns the given key presses one by one."""

    def __init__(self, *parts):
        self.q = list("".join(parts))

    def getwch(self):
        return self.q.pop(0)


class MenuTests(unittest.TestCase):
    def setUp(self):
        install.ARGS = argparse.Namespace(yes=False)
        install.UI["vt"] = True

    def tearDown(self):
        install.UI["vt"] = False

    def pick(self, keys, *args, **kw):
        with patch.object(install, "msvcrt", Keys(*keys)), redirect_stdout(io.StringIO()):
            return install.select(*args, **kw)

    def test_arrows_wrap_digits_and_hot_keys(self):
        self.assertEqual(self.pick([DOWN, DOWN, UP, "\r"], "q", ["a", "b", "c"]), 1)
        self.assertEqual(self.pick([UP, "\r"], "q", ["a", "b", "c"]), 2)
        self.assertEqual(self.pick(["3"], "q", ["a", "b", "c"]), 2)
        self.assertEqual(self.pick(["n"], "q", ["Yes", "No"], hot={"y": 0, "n": 1}), 1)

    def test_checkboxes_filter_and_back(self):
        self.assertEqual(self.pick([" ", DOWN, " ", "\r"], "q", ["x", "y"], marks=[True, False]), [False, True])
        models = [f"model-{i}" for i in range(20)] + ["qwen3"]
        self.assertEqual(self.pick(["qwen", "\r"], "q", models), 20)
        self.assertIsNone(self.pick(["\x1b"], "q", ["a"], back=True))

    def test_yes_mode_takes_defaults_without_keys(self):
        install.ARGS = argparse.Namespace(yes=True)
        self.assertEqual(install.select("q", ["a", "b"], 1), 1)
        self.assertEqual(install.select("q", ["a"], marks=[True]), [True])


class StrataDefaultTests(unittest.TestCase):
    def setUp(self):
        # never the real ~\.maxedcode, browser bridge or app window of this PC
        self.tmp = tempfile.TemporaryDirectory()
        self.legacy = Path(self.tmp.name) / "legacy"
        for p in (patch.object(install, "LEGACY_DIR", self.legacy), patch.object(install, "rehost"),
                  patch.object(install, "app_window"), patch.object(install, "browsers_using", return_value=False)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def silent(self, root):
        with patch.object(install, "probe", return_value=(False, "port closed")), \
                patch.object(install.servers, "scan", return_value=[]), \
                patch.object(install.servers, "start", side_effect=AssertionError("must not start a server")), \
                patch.object(install, "write_strata_settings"), \
                patch("sys.argv", ["install.py", "--yes", "--dir", str(root), "--no-deps", "--no-shortcut", "--no-path"]), \
                patch.dict("os.environ", {"AGENT_BASE_URL": ""}), redirect_stdout(io.StringIO()):
            install.main()

    def test_silent_install_saves_strata_when_it_is_not_running(self):
        root = Path(self.tmp.name) / "maxedcode"
        self.silent(root)
        cfg = json.loads((root / "program" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(f.name for f in root.iterdir()),
                         ["README.txt", "maxedcode CLI.bat", "maxedcode app.bat", "program"])
        self.assertEqual(cfg["base_url"], install.STRATA_URL)
        self.assertNotIn("model", cfg)   # the agent takes the model Strata has loaded
        self.assertEqual(cfg["ctx"], 32768)

    def test_maxed_launcher_is_written_without_path_or_shortcut(self):
        root = Path(self.tmp.name) / "maxedcode"
        self.silent(root)
        bat = (root / "program" / "bin" / "maxed.bat").read_text(encoding="utf-8")
        readme = (root / "README.txt").read_text(encoding="utf-8")
        cli = (root / "maxedcode CLI.bat").read_text(encoding="utf-8")
        self.assertIn(r"%~dp0..\local_agent.py", bat)
        self.assertIn("%*", bat)          # maxed --print "prompt" passes its arguments
        self.assertIn("goto app", bat)    # maxed app opens the window
        self.assertIn("goto shortcuts", bat)
        self.assertIn(r"%~dp0program\local_agent.py", cli)
        self.assertIn("maxed shortcuts", readme)
        self.assertTrue((root / "program" / "desktop" / "app" / "maxedcode.ps1").exists())

    def test_old_layout_install_is_moved_and_its_settings_kept(self):
        old = self.legacy
        (old / "maxedcode").mkdir(parents=True)
        (old / "maxedcode" / "config.py").write_text("")
        (old / "local_agent.py").write_text("")
        (old / "start.bat").write_text("")
        (old / "sessions").mkdir()
        (old / "config.json").write_text(json.dumps({"base_url": "http://10.0.0.5:1234/v1", "ctx": 8192}))
        root = Path(self.tmp.name) / "maxedcode"
        with patch.object(install, "ARGS", argparse.Namespace(yes=True)), redirect_stdout(io.StringIO()):
            cur = install.load_cur(root / "program")
        self.assertEqual(cur["base_url"], "http://10.0.0.5:1234/v1")
        self.silent(root)
        self.assertEqual(sorted(f.name for f in old.iterdir()), ["old-program", "sessions"])   # the data stays
        self.assertTrue((old / "old-program" / "start.bat").exists())

    def test_launchers_replace_old_ones(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "program"
            (dest / "desktop").mkdir(parents=True)
            (dest / "desktop" / "maxedcode-app.bat").write_text("@echo off\n")
            for name in ("start.bat", "maxed.bat", "configure.bat"):
                (Path(d) / name).write_text("@echo off\n")
            install.write_launchers(dest)
            root = sorted(f.name for f in Path(d).iterdir())
            self.assertEqual(sorted(f.name for f in (dest / "bin").iterdir()),
                             ["configure.bat", "maxed.bat", "shortcuts.bat", "uninstall.bat"])
        self.assertEqual(root, ["README.txt", "maxedcode CLI.bat", "maxedcode app.bat", "program"])

    def test_path_moves_to_bin(self):
        old = Path(r"C:\Users\you\.maxedcode")
        new = Path(r"C:\Users\you\AppData\Local\Programs\maxedcode\program\bin")
        self.assertEqual(install.path_with_bin(rf"C:\x;{old}", new, [old]), rf"C:\x;{new}")
        self.assertEqual(install.path_with_bin(r"C:\x", new), rf"C:\x;{new}")
        self.assertIsNone(install.path_with_bin(rf"C:\x;{new}", new, [old]))

    def test_old_launchers_in_shared_path_folders_are_retired(self):
        with tempfile.TemporaryDirectory() as d:
            shared, own = Path(d) / "local-bin", Path(d) / "program" / "bin"
            shared.mkdir()
            own.mkdir(parents=True)
            (shared / "maxed.cmd").write_text('python "C:\\Users\\you\\.maxedcode\\local_agent.py" %*\n')
            (shared / "maxed.bat").write_text("@echo off\nsomething-else.exe %*\n")   # not ours: left alone
            (own / "maxed.bat").write_text('python "..\\local_agent.py" %*\n')
            with redirect_stdout(io.StringIO()):
                done = install.retire_old_launchers(own, f"{shared};{own}")
            self.assertEqual(done, [shared / "maxed.cmd"])
            self.assertTrue((shared / "maxed.cmd.old").is_file())
            self.assertTrue((shared / "maxed.bat").is_file())
            self.assertTrue((own / "maxed.bat").is_file())

    def test_installed_server_that_is_off_is_saved_not_started(self):
        x = {"kind": "lmstudio", "label": "LM Studio", "base": "http://127.0.0.1:1234/v1", "running": False,
             "start": ["lms", "server", "start"], "how": "LM Studio: Developer -> Start Server"}
        self.assertNotIn("starts", install.found_hint(x))
        with patch.object(install.servers, "start", side_effect=AssertionError("setup must not start servers")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(install.use_found(x, "k"), (x["base"], "k", None))

    def test_path_is_not_added_twice(self):
        d = "C:\\Users\\you\\.maxedcode"
        self.assertTrue(install.path_contains(f"C:\\x;{d};", d))
        self.assertTrue(install.path_contains(f"C:\\x;{d.lower()}", d))
        self.assertFalse(install.path_contains("C:\\x;C:\\Other", d))
        with patch.dict("os.environ", {"MX_TEST_HOME": "C:\\Users\\you"}):
            self.assertTrue(install.path_contains("%MX_TEST_HOME%\\.maxedcode\\", d))   # REG_EXPAND_SZ entry

    def test_unpinned_model_drops_an_old_one(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            install.write_config(Path(d), {"model": "old", "home": "x"}, install.STRATA_URL, "local", None, 8192)
            cfg = json.loads((Path(d) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg, {"home": "x", "base_url": install.STRATA_URL, "api_key": "local", "ctx": 8192})

    def test_strata_address_is_recognized(self):
        self.assertTrue(install.is_strata(install.candidates("127.0.0.1:8080")[0]))
        self.assertTrue(install.is_strata("http://localhost:8080/v1/"))
        self.assertFalse(install.is_strata("http://127.0.0.1:1234/v1"))


class UninstallTests(unittest.TestCase):
    """maxed uninstall removes only what this install made."""

    def setUp(self):
        # resolved: %TEMP% may be a short 8.3 path (C:\Users\RUNNER~1) and uninstall works with the full one
        self.root = (Path(tempfile.mkdtemp()) / "maxedcode").resolve()
        (self.root / "program" / "maxedcode").mkdir(parents=True)
        (self.root / "program" / "maxedcode" / "config.py").write_text("", encoding="utf-8")
        (self.root / "program" / "bin").mkdir()
        (self.root / "program" / "bin" / "maxed.bat").write_text("@echo off", encoding="utf-8")
        for name in install.ROOT_FILES:
            (self.root / name).write_text("x", encoding="utf-8")
        install.ARGS = argparse.Namespace(dir=str(self.root), yes=True, purge=False)

    def test_inside_knows_the_install_folder(self):
        self.assertTrue(install._inside(str(self.root / "program" / "bin"), self.root))
        self.assertTrue(install._inside(f'"{self.root}\\program\\desktop\\app\\maxedcode.exe"', self.root))
        self.assertFalse(install._inside(str(self.root.parent / "maxedcode-other" / "program"), self.root))

    def test_files_go_and_a_shared_folder_keeps_the_rest(self):
        (self.root / "notes.txt").write_text("mine", encoding="utf-8")   # --dir C:\tools: not ours
        self.assertEqual(install.remove_files(self.root), [])
        self.assertEqual([p.name for p in self.root.iterdir()], ["notes.txt"])
        (self.root / "notes.txt").unlink()
        install.remove_files(self.root)
        self.assertFalse(self.root.exists())

    def test_run_removes_this_install_and_keeps_the_data(self):
        with patch.object(install, "remove_bridge", return_value=False), \
                patch.object(install, "remove_shortcuts", return_value=["maxedcode CLI"]) as links, \
                patch.object(install, "remove_from_path", return_value=1) as path, \
                patch.object(install, "DATA_DIR", self.root.parent / "data"), redirect_stdout(io.StringIO()):
            (self.root.parent / "data").mkdir()
            self.assertEqual(install.run_uninstall(), 0)
        self.assertFalse(self.root.exists())
        self.assertTrue((self.root.parent / "data").exists())            # chats stay without --purge
        links.assert_called_once_with(self.root)
        path.assert_called_once_with(self.root)

    def test_a_folder_without_an_install_is_left_alone(self):
        (self.root / "program" / "maxedcode" / "config.py").unlink()
        with patch.object(install, "remove_files") as files, redirect_stdout(io.StringIO()):
            self.assertEqual(install.run_uninstall(), 1)
        files.assert_not_called()
        self.assertTrue(self.root.exists())


if __name__ == "__main__":
    unittest.main()
