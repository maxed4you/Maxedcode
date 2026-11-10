"""Installer: arrow-key menus, Strata defaults, setup without a running model server; the Linux / macOS install
(venv, program/bin/maxed, ~/.local/bin/maxed, app menu entries, maxedcode.app) with the platform switched."""
import argparse
import io
import json
import os
import plistlib
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import install

UP, DOWN = "\xe0H", "\xe0P"
# installer/bootstrap.sh against fake pythons / uv: prints 1 ok, 2 ok, 3 ok
BOOTSTRAP_CHECK = r'''#!/bin/sh
# bootstrap.sh with fake pythons and a fake uv: (1) a python whose venv works is taken; (2) a python without venv
# is skipped and uv's managed python is used; (3) nothing works and no uv: exit 1 with instructions.
BOOT="$1"
T=$(mktemp -d)
export HOME="$T/home" TMPDIR="$T/tmp"
mkdir -p "$HOME" "$TMPDIR" "$T/good" "$T/bad" "$T/uvpy/bin"

cat > "$T/good/python3" <<'EOF'
#!/bin/sh
case "$1" in
  -c) case "$2" in *executable*) echo "$0" ;; esac; exit 0 ;;
  -m) if [ "$2" = venv ]; then mkdir -p "$3/bin"; printf '#!/bin/sh\nexit 0\n' > "$3/bin/python"; chmod +x "$3/bin/python"; exit 0; fi ;;
esac
exit 0
EOF
cat > "$T/bad/python3" <<'EOF'
#!/bin/sh
case "$1" in
  -c) case "$2" in *executable*) echo "$0" ;; esac; exit 0 ;;
  -m) echo "ensurepip is not available" >&2; exit 1 ;;
esac
exit 0
EOF
cp "$T/good/python3" "$T/uvpy/bin/python3.12"
mkdir -p "$HOME/.maxedcode/uv"
cat > "$HOME/.maxedcode/uv/uv" <<EOF
#!/bin/sh
[ "\$1 \$2" = "python find" ] && { echo "$T/uvpy/bin/python3.12"; exit 0; }
exit 1
EOF
chmod +x "$T/good/python3" "$T/bad/python3" "$T/uvpy/bin/python3.12" "$HOME/.maxedcode/uv/uv"

base="/usr/bin:/bin"
export MX_PYTHONS=python3
fail=0
out=$(PATH="$T/good:$base" MX_PYFILE= sh "$BOOT" 2>/dev/null); [ "$out" = "$T/good/python3" ] && echo "1 ok" || { echo "1 FAIL: $out"; fail=1; }
out=$(PATH="$T/bad:$base" MX_PYFILE= sh "$BOOT" 2>/dev/null); [ "$out" = "$T/uvpy/bin/python3.12" ] && echo "2 ok" || { echo "2 FAIL: $out"; fail=1; }
rm -rf "$HOME/.maxedcode/uv"
mkdir -p "$T/nonet"; printf '#!/bin/sh\nexit 7\n' > "$T/nonet/curl"; chmod +x "$T/nonet/curl"
PATH="$T/bad:$T/nonet:$base" MX_PYFILE="$T/py.txt" sh "$BOOT" 2>"$T/err.txt"; rc=$?
[ "$rc" = 1 ] && grep -q "python3-venv" "$T/err.txt" && [ ! -s "$T/py.txt" ] && echo "3 ok" || { echo "3 FAIL rc=$rc"; cat "$T/err.txt"; fail=1; }
rm -rf "$T"
exit $fail
'''
WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "the Windows layout (.bat launchers, registry PATH, %VARS%)")


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

    @WINDOWS_ONLY
    def test_silent_install_saves_strata_when_it_is_not_running(self):
        root = Path(self.tmp.name) / "maxedcode"
        self.silent(root)
        cfg = json.loads((root / "program" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(f.name for f in root.iterdir()),
                         ["README.txt", "maxedcode CLI.bat", "maxedcode app.bat", "program"])
        self.assertEqual(cfg["base_url"], install.STRATA_URL)
        self.assertNotIn("model", cfg)   # the agent takes the model Strata has loaded
        self.assertEqual(cfg["ctx"], 32768)

    @WINDOWS_ONLY
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

    @WINDOWS_ONLY
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

    @WINDOWS_ONLY
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

    @WINDOWS_ONLY
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

    @WINDOWS_ONLY
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

    @WINDOWS_ONLY
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


class PosixInstallTests(unittest.TestCase):
    """The Linux / macOS install, run on any OS with the platform switched; pip and the venv are never touched
    (--no-deps), and HOME / XDG_DATA_HOME are temporary folders."""

    macos = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "inst" / "maxedcode"
        env = {"XDG_DATA_HOME": str(self.home / ".local" / "share"), "AGENT_BASE_URL": "",
               "MX_DIRFILE": str(self.tmp / "dir.txt"), "PATH": os.environ.get("PATH", ""), "SHELL": "/bin/bash"}
        for p in (patch.object(install, "WINDOWS", False), patch.object(install, "MACOS", self.macos),
                  patch.object(install, "user_home", return_value=self.home),
                  patch.object(install, "DATA_DIR", self.home / ".maxedcode"),
                  patch.object(install, "LEGACY_DIR", self.home / ".maxedcode"),
                  patch.object(install, "write_strata_settings"), patch.dict("os.environ", env),
                  patch.dict(install.UI, {"errors": [], "sink": None, "vt": False})):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def small_copy(dest):
        """copy_files, the few files these tests look at (the real copy is the same code as on Windows)."""
        (dest / "maxedcode").mkdir(parents=True, exist_ok=True)
        (dest / "desktop").mkdir(exist_ok=True)
        for name in ("install.py", "local_agent.py"):
            (dest / name).write_text("", encoding="utf-8")
        (dest / "maxedcode" / "config.py").write_text("", encoding="utf-8")
        src = Path(install.__file__).resolve().parent
        install.shutil.copy2(src / "maxedcode" / "version.py", dest / "maxedcode" / "version.py")
        install.shutil.copy2(install.SRC_DESKTOP / "maxedcode.png", dest / "desktop" / "maxedcode.png")

    def install(self, *extra):
        with patch.object(install, "copy_files", side_effect=self.small_copy), \
                patch.object(install, "probe", return_value=(False, "port closed")), \
                patch.object(install.servers, "scan", return_value=[]), \
                patch.object(install.servers, "start", side_effect=AssertionError("must not start a server")), \
                patch.object(install, "install_deps", side_effect=AssertionError("--no-deps: no pip")), \
                patch("sys.argv", ["install.py", "--yes", "--dir", str(self.root), "--no-deps", *extra]), \
                redirect_stdout(io.StringIO()):
            install.main()

    def uninstall(self):
        install.ARGS = argparse.Namespace(dir=str(self.root), yes=True, purge=False)
        with redirect_stdout(io.StringIO()):
            return install.run_uninstall()

    def test_layout_launcher_and_readme(self):
        self.install()
        program = self.root / "program"
        self.assertEqual(sorted(f.name for f in self.root.iterdir()), ["README.txt", "program"])
        self.assertFalse(list(self.root.rglob("*.bat")) and list((program / "bin").glob("*.bat")))
        self.assertEqual(sorted(f.name for f in (program / "bin").iterdir()), ["maxed"])
        maxed = program / "bin" / "maxed"
        raw = maxed.read_bytes()
        self.assertTrue(raw.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", raw)
        text = raw.decode("utf-8")
        self.assertIn('PY="$PROGRAM/.venv/bin/python"', text)
        self.assertIn('exec "$PY" "$PROGRAM/local_agent.py" "$@"', text)
        for word, call in (("app", '"$PROGRAM/local_agent.py" --app'), ("configure", "install.py\" --configure"),
                           ("shortcuts", "install.py\" --launchers --no-path"), ("uninstall", "install.py\" --uninstall")):
            self.assertIn(f"  {word}) shift; exec \"$PY\" {call}", text.replace("\"$PROGRAM/install.py", "install.py"))
        self.assertIn(str(program), text)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(maxed.stat().st_mode), 0o755)
        readme = (self.root / "README.txt").read_text(encoding="utf-8")
        self.assertIn("maxed app", readme)
        self.assertIn(f"{self.root}/program/bin/maxed", readme)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', readme)
        self.assertNotIn(".bat", readme)
        cfg = json.loads((program / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["base_url"], install.STRATA_URL)
        self.assertEqual((self.tmp / "dir.txt").read_text(encoding="utf-8"), str(self.root))   # npm learns the folder

    def test_local_bin_link_and_path_hint(self):
        self.install()
        link = self.home / ".local" / "bin" / "maxed"
        self.assertTrue(link.is_symlink() or link.is_file())
        if link.is_symlink():
            self.assertEqual(link.resolve(), (self.root / "program" / "bin" / "maxed").resolve())
        else:                                   # no symbolic links here (Windows without developer mode)
            self.assertIn(str(self.root / "program" / "bin" / "maxed"), link.read_text(encoding="utf-8"))
        self.assertTrue(install.own_launcher(link))
        rc = install.shell_rc()[0]              # ~/.bashrc (~/.bash_profile on macOS): ~/.local/bin is not in PATH
        self.assertIn(install.PATH_MARK + '\nexport PATH="$HOME/.local/bin:$PATH"\n', rc.read_text(encoding="utf-8"))

    def test_another_programs_maxed_is_left_alone(self):
        other = self.home / ".local" / "bin" / "maxed"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\necho something else\n", encoding="utf-8")
        self.install()
        self.assertEqual(other.read_text(encoding="utf-8"), "#!/bin/sh\necho something else\n")
        self.assertEqual(self.uninstall(), 0)
        self.assertTrue(other.exists())

    def test_shell_rc_line_is_added_once_and_taken_out_on_uninstall(self):
        rc = install.shell_rc()[0]
        rc.write_text("alias ll='ls -l'\n", encoding="utf-8")
        self.install()
        self.install()                          # setup again: still one line
        self.assertEqual(rc.read_text(encoding="utf-8").count(install.PATH_MARK), 1)
        self.assertEqual(self.uninstall(), 0)
        self.assertEqual(rc.read_text(encoding="utf-8"), "alias ll='ls -l'\n")

    def test_shell_rc_is_not_touched_when_local_bin_is_in_path(self):
        with patch.dict("os.environ", {"PATH": str(self.home / ".local" / "bin") + os.pathsep + os.environ["PATH"]}):
            self.install()
        self.assertFalse(any(f.exists() for f in install.rc_files()))
        self.assertIn("links ~/.local/bin/maxed", install.option_label("path"))

    def test_the_path_option_says_which_file_it_changes(self):
        with patch.dict("os.environ", {"SHELL": "/bin/zsh"}):
            self.assertIn("in ~/.zshrc", install.option_label("path"))
        with patch.dict("os.environ", {"SHELL": "/usr/bin/fish"}):
            self.assertEqual(install.shell_rc()[1], "fish_add_path $HOME/.local/bin")

    def test_menu_entries_and_icon(self):
        self.install()
        apps = self.home / ".local" / "share" / "applications"
        app = (apps / "maxedcode.desktop").read_text(encoding="utf-8")
        cli = (apps / "maxedcode-cli.desktop").read_text(encoding="utf-8")
        maxed = install.desktop_arg(self.root / "program" / "bin" / "maxed")
        self.assertIn(f"Exec={maxed} app\n", app)
        self.assertIn("Terminal=false", app)
        self.assertIn(f"Exec={maxed}\n", cli)
        self.assertIn("Terminal=true", cli)
        icon = self.home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "maxedcode.png"
        self.assertTrue(icon.is_file())
        self.assertIn(f"Icon={icon}", app)
        self.assertFalse((self.home / "Applications").exists())

    def test_desktop_exec_quoting(self):
        self.assertEqual(install.desktop_arg("/opt/mx/program/bin/maxed"), "/opt/mx/program/bin/maxed")
        self.assertEqual(install.desktop_arg("/home/a b/maxed"), '"/home/a b/maxed"')
        self.assertEqual(install.desktop_arg("/x/$y/100%"), '"/x/\\\\$y/100%%"')

    def test_no_shortcut_no_path(self):
        self.install("--no-shortcut", "--no-path")
        self.assertFalse((self.home / ".local").exists())
        self.assertTrue((self.root / "program" / "bin" / "maxed").exists())

    def test_uninstall_removes_what_it_made_and_keeps_the_data(self):
        self.install()
        (self.home / ".maxedcode").mkdir(exist_ok=True)
        (self.home / ".maxedcode" / "settings.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.uninstall(), 0)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / ".local" / "bin" / "maxed").exists())
        self.assertEqual(list((self.home / ".local" / "share" / "applications").iterdir()), [])
        self.assertFalse((self.home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" /
                          "maxedcode.png").exists())
        self.assertTrue((self.home / ".maxedcode" / "settings.json").exists())

    def test_update_rewrites_the_launcher_and_keeps_the_rest(self):
        self.install()
        maxed = self.root / "program" / "bin" / "maxed"
        maxed.write_text("old\n", encoding="utf-8")
        cfg = (self.root / "program" / "config.json").read_text(encoding="utf-8")
        (self.root / "program" / ".venv").mkdir()
        before = sorted(p.name for p in (self.home / ".local" / "share" / "applications").iterdir())
        install.ARGS = argparse.Namespace(dir=str(self.root))
        with patch.object(install, "deps_ok", return_value=True), redirect_stdout(io.StringIO()), \
                patch.object(install, "copy_files", side_effect=self.small_copy):
            self.assertEqual(install.run_update(), 0)
        self.assertTrue(maxed.read_text(encoding="utf-8").startswith("#!/bin/sh"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(maxed.stat().st_mode), 0o755)
        self.assertEqual((self.root / "program" / "config.json").read_text(encoding="utf-8"), cfg)
        self.assertTrue((self.root / "program" / ".venv").is_dir())
        self.assertEqual(sorted(p.name for p in (self.home / ".local" / "share" / "applications").iterdir()), before)

    def test_program_python_is_the_venv_one(self):
        dest = self.root / "program"
        self.assertEqual(install.program_python(dest), install.sys.executable)
        venv = dest / ".venv" / "bin" / "python"
        venv.parent.mkdir(parents=True)
        venv.write_text("", encoding="utf-8")
        self.assertEqual(install.program_python(dest), str(venv))
        with patch.object(install, "WINDOWS", True):
            self.assertEqual(install.program_python(dest), install.sys.executable)

    def fake_run(self, venv_ok):
        """subprocess.run for ensure_venv: `python -m venv` works or not; a made venv's python has pip."""
        dest, calls = self.root / "program", []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"] and venv_ok:
                (dest / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
                (dest / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
            code = 0 if venv_ok or cmd[1:3] != ["-m", "venv"] else 1
            if cmd[1:3] == ["-m", "pip"] and not (dest / ".venv" / "bin" / "python").exists():
                code = 1
            return install.subprocess.CompletedProcess(cmd, code, "", "" if code == 0 else "ensurepip is not available")
        return dest, calls, run

    def test_venv_is_made_with_the_setup_python(self):
        dest, calls, run = self.fake_run(True)
        with patch.object(install.subprocess, "run", side_effect=run), \
                patch.object(install, "find_uv", side_effect=AssertionError("no uv needed")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(install.ensure_venv(dest), dest / ".venv" / "bin" / "python")
        self.assertIn([install.sys.executable, "-m", "venv", "--clear", str(dest / ".venv")], calls)

    def test_no_venv_module_falls_back_to_uv_and_its_python(self):
        dest, calls, run = self.fake_run(False)
        logged = []

        def uv_venv(cmd, env=None):
            logged.append((cmd, env))
            (dest / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            (dest / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
            return 0
        with patch.object(install.subprocess, "run", side_effect=run), \
                patch.object(install, "find_uv", return_value="/opt/uv/uv") as find, \
                patch.object(install, "run_logged", side_effect=uv_venv), redirect_stdout(io.StringIO()):
            self.assertEqual(install.ensure_venv(dest), dest / ".venv" / "bin" / "python")
        find.assert_called_once_with(download=True)
        cmd, env = logged[0]
        self.assertEqual(cmd, ["/opt/uv/uv", "venv", "--seed", "--python", "3.12", str(dest / ".venv")])
        self.assertEqual(env["UV_PYTHON_PREFERENCE"], "only-managed")
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(self.home / ".maxedcode" / "uv" / "python"))

    def test_missing_venv_module_and_no_uv_names_the_package(self):
        dest, calls, run = self.fake_run(False)
        said = []
        with patch.object(install.subprocess, "run", side_effect=run), \
                patch.object(install, "find_uv", return_value=None), \
                patch.object(install, "say", side_effect=lambda m, k="": said.append((m, k))):
            self.assertIsNone(install.ensure_venv(dest))
        self.assertTrue(any("python3-venv" in m and k == "err" for m, k in said))

    def test_uv_comes_from_its_own_folder_first_and_downloads_only_when_asked(self):
        uv = self.home / ".maxedcode" / "uv" / "uv"
        with patch.object(install.shutil, "which", return_value=None):
            self.assertIsNone(install.find_uv())                  # nothing there, no download asked
            uv.parent.mkdir(parents=True)
            uv.write_text("", encoding="utf-8")
            uv.chmod(0o755)
            if os.access(uv, os.X_OK):
                self.assertEqual(install.find_uv(), str(uv))
        with patch.object(install.shutil, "which", return_value=None), \
                patch.object(install, "uv_home", return_value=self.tmp / "nouv"), \
                patch.object(install.urllib.request, "urlopen", side_effect=install.urllib.error.URLError("offline")), \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(install.find_uv(download=True))     # offline: no uv, setup goes on

    def test_packages_go_in_with_uv_when_it_is_there(self):
        logged = []
        with patch.object(install, "find_uv", return_value="/opt/uv/uv"), \
                patch.object(install, "run_logged", side_effect=lambda cmd, env=None: logged.append(cmd) or 0):
            self.assertEqual(install.pip_install("/v/bin/python", ["rich"]), 0)
        self.assertEqual(logged[0][:5], ["/opt/uv/uv", "pip", "install", "--python", "/v/bin/python"])
        with patch.object(install, "find_uv", return_value=None), \
                patch.object(install, "run_logged", side_effect=lambda cmd, env=None: logged.append(cmd) or 0):
            install.pip_install("/v/bin/python", ["rich"])
        self.assertEqual(logged[1][:4], ["/v/bin/python", "-m", "pip", "install"])

    def test_window_package_is_optional(self):
        said, wanted = [], []
        fail = install.subprocess.CompletedProcess([], 1, "", "ImportError: libEGL.so.1")
        with patch.object(install, "pip_install", side_effect=lambda py, pkgs: wanted.extend(pkgs) or 0), \
                patch.object(install.subprocess, "run", return_value=fail), \
                patch.object(install, "say", side_effect=lambda m, k="": said.append(k)):
            self.assertFalse(install.window_package("/v/bin/python"))
        self.assertEqual(wanted, ["pywebview" if self.macos else "pywebview[qt]"])
        self.assertNotIn("err", said)                              # a warning, never a failed setup

    def test_bootstrap_sh_parses_and_picks_a_python_whose_venv_works(self):
        """installer/bootstrap.sh with fake pythons and a fake uv (no network, a temporary HOME): a python whose venv
        works is taken; one without venv is skipped for uv's own Python; neither: exit 1 and the instructions."""
        script = Path(install.__file__).resolve().parent.parent / "installer" / "bootstrap.sh"
        if not script.exists():
            self.skipTest("no installer/bootstrap.sh in this copy")
        self.assertNotIn(b"\r", script.read_bytes())
        sh = install.shutil.which("sh")          # Git's sh on Windows; never System32\bash.exe (WSL)
        if not sh or "system32" in sh.lower():
            self.skipTest("no POSIX shell here")
        r = install.subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        check = self.tmp / "check.sh"
        check.write_text(BOOTSTRAP_CHECK, encoding="utf-8", newline="\n")
        r = install.subprocess.run([sh, check.as_posix(), script.as_posix()], capture_output=True, text=True,
                                   timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["1", "ok", "2", "ok", "3", "ok"])


class MacInstallTests(PosixInstallTests):
    """The same on macOS: ~/Applications/maxedcode.app instead of the app menu entries."""

    macos = True

    def test_menu_entries_and_icon(self):
        self.install()
        app = self.home / "Applications" / "maxedcode.app" / "Contents"
        script = app / "MacOS" / "maxedcode"
        self.assertIn(f"{self.root / 'program' / 'bin' / 'maxed'}", script.read_text(encoding="utf-8"))
        self.assertTrue(script.read_text(encoding="utf-8").startswith("#!/bin/sh\n"))
        self.assertIn(' app "$@"', script.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleExecutable"], "maxedcode")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertEqual(info["CFBundleShortVersionString"], install.program_version(self.root / "program"))
        self.assertFalse((self.home / ".local" / "share" / "applications").exists())

    def test_uninstall_removes_what_it_made_and_keeps_the_data(self):
        self.install()
        self.assertEqual(self.uninstall(), 0)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / "Applications" / "maxedcode.app").exists())
        self.assertFalse((self.home / ".local" / "bin" / "maxed").exists())

    def test_update_rewrites_the_launcher_and_keeps_the_rest(self):
        self.install()
        install.ARGS = argparse.Namespace(dir=str(self.root))
        bundle = self.home / "Applications" / "maxedcode.app" / "Contents" / "Info.plist"
        before = bundle.read_bytes()
        with patch.object(install, "deps_ok", return_value=True), redirect_stdout(io.StringIO()), \
                patch.object(install, "copy_files", side_effect=self.small_copy):
            self.assertEqual(install.run_update(), 0)
        self.assertEqual(bundle.read_bytes(), before)           # the bundle only calls program/bin/maxed

class PlatformSwitchTests(unittest.TestCase):
    """Each platform writes only its own launchers: the Windows ones are untouched by the Linux / macOS code."""

    def test_windows_launchers_stay_as_they_were(self):
        with tempfile.TemporaryDirectory() as d, patch.object(install, "WINDOWS", True):
            dest = Path(d) / "program"
            (dest / "desktop").mkdir(parents=True)
            (dest / "desktop" / "maxedcode-app.bat").write_text("@echo off\n")
            with patch.object(install, "write_posix_launchers", side_effect=AssertionError("not on Windows")):
                install.write_launchers(dest)
            self.assertEqual(sorted(f.name for f in (dest / "bin").iterdir()),
                             ["configure.bat", "maxed.bat", "shortcuts.bat", "uninstall.bat"])
            self.assertEqual(sorted(f.name for f in Path(d).iterdir()),
                             ["README.txt", "maxedcode CLI.bat", "maxedcode app.bat", "program"])
            self.assertIn(b"\r\n", (dest / "bin" / "maxed.bat").read_bytes())
            self.assertIn("maxedcode app.bat", (Path(d) / "README.txt").read_text(encoding="utf-8"))

    def test_posix_skips_everything_windows_only(self):
        with tempfile.TemporaryDirectory() as d, patch.object(install, "WINDOWS", False), \
                patch.object(install.subprocess, "run", side_effect=AssertionError("nothing to run")):
            dest = Path(d) / "program"
            (dest / "desktop" / "app").mkdir(parents=True)
            (dest / "desktop" / "app" / "maxedcode.cs").write_text("")
            install.app_window(dest, True)                         # no exe, no WebView2, no powershell
            install.rehost(dest)                                   # no host.bat
            self.assertEqual(install.legacy_installs(dest), [])    # no old ~/.maxedcode layout


class PosixKeyTests(unittest.TestCase):
    def test_escape_sequences(self):
        self.assertEqual(install.POSIX_KEYS["[A"], "up")
        self.assertEqual(install.POSIX_KEYS["[B"], "down")
        self.assertEqual(install.POSIX_KEYS["[6~"], "pgdn")
        self.assertEqual(install.lines_up(3), "\x1b[3F" if os.name == "nt" else "\r\x1b[3A")


if __name__ == "__main__":
    unittest.main()
