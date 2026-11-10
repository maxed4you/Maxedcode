"""macOS / Linux support outside the shell tool: what each POSIX branch picks, tested on any OS.

The POSIX branches look at `os.name` / `sys.platform` when they run, so the tests hand each module a stand-in
`os` (name "posix") or patch `sys.platform`; the real `os.name` is never changed (pathlib builds its paths
from it). Tests that need a real pseudo terminal or real file modes run only on macOS / Linux.
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-posix-tests-")
os.environ["AGENT_HOME"] = _HOME.name

from maxedcode import chrome, cloud, filewatch, plat, servers, util       # noqa: E402
from maxedcode.webapp import native, server                              # noqa: E402

POSIX = os.name != "nt"


class PosixOs:
    """`os` as a module on macOS / Linux sees it: only the name differs, everything else is the real os."""
    name = "posix"

    def __getattr__(self, key):
        return getattr(os, key)


def which_only(*names):
    """shutil.which that finds only these programs."""
    return lambda name, *a, **k: f"/usr/bin/{name}" if name in names else None


class PlatTests(unittest.TestCase):
    def test_clipboard_commands_per_desktop(self):
        with patch.object(plat, "IS_WIN", False), patch.object(plat, "IS_MAC", True):
            self.assertEqual(plat.clipboard_commands(), (["pbcopy"], ["pbpaste"]))
        with patch.object(plat, "IS_WIN", False), patch.object(plat, "IS_MAC", False):
            with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), \
                    patch.object(plat.shutil, "which", which_only("wl-copy", "xclip")):
                self.assertEqual(plat.clipboard_commands()[0], ["wl-copy"])
            with patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}), \
                    patch.object(plat.shutil, "which", which_only("wl-copy", "xclip")):
                self.assertEqual(plat.clipboard_commands()[0], ["xclip", "-selection", "clipboard"])
            with patch.object(plat.shutil, "which", which_only("xsel")):
                self.assertEqual(plat.clipboard_commands()[1], ["xsel", "--clipboard", "--output"])
            with patch.object(plat.shutil, "which", which_only()):
                self.assertEqual(plat.clipboard_commands(), (None, None))
                self.assertFalse(plat.copy("text"))          # silently unavailable
                self.assertIsNone(plat.paste())

    def test_copy_sends_utf8_to_the_command(self):
        got = {}

        class Proc:
            def __init__(self, cmd, **kw):
                got["cmd"] = cmd

            def communicate(self, data, timeout=None):
                got["data"] = data

        with patch.object(plat, "IS_WIN", False), patch.object(plat, "IS_MAC", True), \
                patch.object(plat.subprocess, "Popen", Proc):
            self.assertTrue(plat.copy("привет"))
        self.assertEqual(got, {"cmd": ["pbcopy"], "data": "привет".encode("utf-8")})

    def test_open_and_reveal_pick_the_desktop_command(self):
        runs = []
        with patch.object(plat, "_detached", lambda cmd, cwd=None: runs.append(cmd) or True), \
                patch.object(plat, "IS_WIN", False):
            with patch.object(plat, "IS_MAC", True):
                plat.open_path("https://example.com")
                plat.reveal("/x/y.txt")
            with patch.object(plat, "IS_MAC", False), patch.object(plat.shutil, "which", which_only("xdg-open")):
                plat.open_path("/x/y.txt")
                plat.reveal("/x/y.txt")              # no gdbus: its folder opens
            with patch.object(plat, "IS_MAC", False), patch.object(plat.shutil, "which", which_only()):
                self.assertFalse(plat.open_path("/x"))
        self.assertEqual(runs[0], ["open", "https://example.com"])
        self.assertEqual(runs[1][:2], ["open", "-R"])
        self.assertEqual(runs[2], ["/usr/bin/xdg-open", "/x/y.txt"])
        self.assertEqual(runs[3][0], "/usr/bin/xdg-open")
        self.assertEqual(len(runs), 4)

    def test_trash_never_deletes_for_good(self):
        calls = []
        ok = SimpleNamespace(returncode=0)
        with patch.dict(sys.modules, {"send2trash": None}), patch.object(plat, "IS_MAC", False), \
                patch.object(plat.shutil, "which", which_only("gio")), \
                patch.object(plat.subprocess, "run", lambda cmd, **k: calls.append(cmd) or ok):
            plat.trash("/tmp/x")
        self.assertEqual(calls[0][:2], ["gio", "trash"])
        with patch.dict(sys.modules, {"send2trash": None}), patch.object(plat, "IS_MAC", False), \
                patch.object(plat.shutil, "which", which_only()):
            with self.assertRaises(OSError):
                plat.trash("/tmp/x")

    def test_dialogs_use_finder_zenity_or_kdialog(self):
        asked = []

        def dialog(cmd):
            asked.append(cmd[0])
            return None if cmd[0] == "zenity" else ["/home/u/proj/"]
        with patch.object(plat, "_dialog", dialog), patch.object(plat, "IS_MAC", False):
            self.assertEqual(plat.pick_folder("/home/u"), "/home/u/proj")
        self.assertEqual(asked, ["zenity", "kdialog"])
        with patch.object(plat, "_dialog", lambda cmd: asked.append(cmd[0]) or []), patch.object(plat, "IS_MAC", True):
            self.assertIsNone(plat.pick_folder("/Users/u"))          # cancelled
            self.assertEqual(plat.pick_files("/Users/u"), [])
        self.assertEqual(asked[-1], "osascript")

    def test_tui_copy_goes_through_plat_off_windows(self):
        with patch.object(util.sys, "platform", "linux"), patch.object(plat, "copy", lambda t: t == "x") as _:
            self.assertTrue(util.to_clip("x"))
        with patch.object(util.sys, "platform", "linux"):
            self.assertFalse(util.shift_down())


class CloudKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name) / "home"
        self.patches = [patch.object(cloud, "os", PosixOs()), patch.object(cloud, "KEY_PATH", home / "k.dpapi"),
                        patch.object(cloud, "KEYS_DIR", home / "cloud_keys"),
                        patch.dict(os.environ, {"MAXEDCODE_CLOUD_API_KEY": ""})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_without_keyring_a_private_file(self):
        with patch.object(cloud, "_keyring", lambda: None):
            cloud._store_key("sk-default")
            cloud._store_key("sk-second", "acc2")
            self.assertEqual(cloud.get_api_key(), "sk-default")
            self.assertEqual(cloud.get_api_key("default"), "sk-default")
            self.assertEqual(cloud.get_api_key("acc2"), "sk-second")
            f = cloud._posix_key_path("acc2")
            self.assertEqual(f.name, "acc2.key")
            self.assertEqual(f.read_bytes(), b"sk-second")
            self.assertEqual(list(f.parent.glob("*.tmp")), [])
            if POSIX:
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(f.parent.stat().st_mode), 0o700)
            cloud._forget_key("acc2")
            self.assertFalse(f.exists())
            self.assertEqual(cloud.get_api_key("acc2"), "")

    def test_keyring_when_it_works(self):
        store = {}
        ring = SimpleNamespace(set_password=lambda s, n, k: store.__setitem__((s, n), k),
                               get_password=lambda s, n: store.get((s, n)),
                               delete_password=lambda s, n: store.pop((s, n)))
        with patch.object(cloud, "_keyring", lambda: ring):
            cloud._store_key("sk-a", "acc")
            self.assertEqual(store, {("maxedcode-cloud", "api-key-acc"): "sk-a"})
            self.assertFalse(cloud._posix_key_path("acc").exists())
            self.assertEqual(cloud.get_api_key("acc"), "sk-a")
            cloud._forget_key("acc")
            self.assertEqual(store, {})

    def test_broken_keyring_falls_back_to_the_file(self):
        def refuse(*a):
            raise RuntimeError("locked")
        ring = SimpleNamespace(set_password=refuse, get_password=lambda s, n: None, delete_password=refuse)
        with patch.object(cloud, "_keyring", lambda: ring):
            cloud._store_key("sk-b")
            self.assertEqual(cloud.get_api_key(), "sk-b")

    def test_windows_keeps_dpapi(self):
        """On Windows the key file is still the DPAPI blob at the old path (old keys keep working)."""
        if POSIX:
            self.skipTest("DPAPI exists only on Windows")
        with patch.object(cloud, "os", os):
            cloud._store_key("sk-win")
            self.assertTrue(cloud.KEY_PATH.is_file())
            self.assertNotIn(b"sk-win", cloud.KEY_PATH.read_bytes())
            self.assertEqual(cloud.get_api_key(), "sk-win")


class ChromeManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [patch.object(chrome, "os", PosixOs()), patch.object(chrome.Path, "home", lambda: self.home),
                        patch.object(chrome, "DIR", self.home / ".maxedcode-app" / "browser")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def check(self, platform, folders):
        with patch.object(chrome.sys, "platform", platform):
            for f in folders[:2]:                       # two browsers installed
                (self.home / f).mkdir(parents=True)
            chrome.register()
            launcher = chrome.DIR / "host.sh"
            text = launcher.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"))
            self.assertIn(str(chrome.HOST), text)
            if POSIX:
                self.assertTrue(os.access(launcher, os.X_OK))
            for f in folders[:2]:
                m = json.loads((self.home / f / "NativeMessagingHosts" / f"{chrome.NAME}.json").read_text("utf-8"))
                self.assertEqual(Path(m["path"]), launcher)
                self.assertEqual(m["allowed_origins"], [f"chrome-extension://{chrome.EXTENSION_ID}/"])
            self.assertFalse((self.home / folders[2]).exists())      # not installed: nothing written there
            self.assertTrue(chrome.registered())
            chrome.unregister()
            self.assertFalse(chrome.registered())
            self.assertFalse(launcher.exists())

    def test_linux_folders(self):
        self.check("linux", [".config/google-chrome", ".config/BraveSoftware/Brave-Browser", ".config/chromium"])

    def test_macos_folders(self):
        lib = "Library/Application Support/"
        self.check("darwin", [lib + "Google/Chrome", lib + "Microsoft Edge", lib + "Chromium"])

    def test_no_browser_yet_gets_chrome_and_chromium(self):
        with patch.object(chrome.sys, "platform", "linux"):
            chrome.register()
        for f in (".config/google-chrome", ".config/chromium"):
            self.assertTrue((self.home / f / "NativeMessagingHosts" / f"{chrome.NAME}.json").is_file())


class ServersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.patches = [patch.object(servers, "os", PosixOs()), patch.object(servers.Path, "home", lambda: self.home),
                        patch.object(servers, "POSIX_BIN", (str(self.bin),)),
                        patch.object(servers.shutil, "which", lambda *a, **k: None),
                        patch.dict(os.environ, {"STRATA_DIR": ""})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def exe(self, *parts):
        p = self.home.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_linux_finds_servers_in_their_own_places(self):
        self.exe(".lmstudio", "bin", "lms")
        self.exe("bin", "ollama")
        self.exe("bin", "llama-server")
        self.exe("Strata", "strata.py")
        self.exe("Strata", "bin", "strata")
        with patch.object(servers.sys, "platform", "linux"), patch.object(servers, "env_value", lambda n: ""):
            found = {x["kind"]: x for x in servers.installed()}
        self.assertEqual(set(found), {"strata", "lmstudio", "ollama", "llamacpp"})
        self.assertEqual(found["lmstudio"]["start"][1:], ["server", "start"])
        self.assertEqual(found["ollama"]["start"][1:], ["serve"])
        self.assertIsNone(found["strata"]["start"])             # it asks questions in its own terminal
        self.assertNotIn("Start menu", found["ollama"]["how"])

    def test_macos_ollama_app(self):
        app = self.home / "Applications" / "Ollama.app"
        app.mkdir(parents=True)
        with patch.object(servers.sys, "platform", "darwin"), patch.object(servers, "env_value", lambda n: ""), \
                patch.object(servers, "_app", lambda name: app if name == "Ollama.app" else None):
            found = {x["kind"]: x for x in servers.installed()}
        self.assertEqual(found["ollama"]["start"], ["open", "-a", str(app)])

    def test_linux_user_env_file(self):
        env_d = self.home / ".config" / "environment.d" / "90-maxedcode.conf"
        with patch.object(servers.sys, "platform", "linux"), patch.object(servers, "ENV_D", env_d), \
                patch.dict(os.environ, {"OLLAMA_KEEP_ALIVE": ""}):
            servers.set_user_env({"OLLAMA_CONTEXT_LENGTH": 32768, "OLLAMA_KEEP_ALIVE": "30m"})
            os.environ.pop("OLLAMA_KEEP_ALIVE")
            self.assertEqual(servers.env_value("OLLAMA_KEEP_ALIVE"), "30m")
        self.assertIn("OLLAMA_CONTEXT_LENGTH=32768\n", env_d.read_text(encoding="utf-8"))

    def test_start_gets_a_session_of_its_own(self):
        got = {}
        entry = {"kind": "ollama", "start": ["ollama", "serve"]}
        with patch.object(servers, "installed", lambda: [entry]), patch.object(servers, "offline", lambda: False), \
                patch.object(servers.subprocess, "Popen", lambda cmd, **kw: got.update(kw)), \
                patch.object(servers, "model_ids", lambda *a, **k: []):
            self.assertEqual(servers.start("ollama"), servers.default_base("ollama"))
        self.assertTrue(got["start_new_session"])
        self.assertEqual(got["creationflags"], 0)


class FileWatchTests(unittest.TestCase):
    def test_posix_paths_named_in_commands(self):
        with patch.object(filewatch, "_ABS_PATH", filewatch._POSIX_PATH), patch.object(filewatch, "os", PosixOs()):
            got = filewatch.named_files('echo hi > /tmp/a.txt; cat "/home/u/my file.txt"; ls -l/x a/b; '
                                        'python -c "open(\'/etc/hosts\')"')
        self.assertEqual(got[:2], ["/tmp/a.txt", "/home/u/my file.txt"])
        self.assertNotIn("/x", got)
        self.assertFalse(any(p.endswith("a/b") for p in got))


class PtyTests(unittest.TestCase):
    def test_the_platform_terminal_is_selected(self):
        from maxedcode.webapp import pty
        self.assertTrue(pty.Pty.__module__.endswith("conpty" if os.name == "nt" else "posixpty"))
        self.assertTrue(callable(pty.shell))

    @unittest.skipIf(os.name == "nt", "a real pseudo terminal: macOS / Linux only")
    def test_posix_shell_runs_and_resizes(self):
        from maxedcode.webapp.posixpty import Pty
        with tempfile.TemporaryDirectory() as d:
            term = Pty(d, 80, 24, command=["/bin/sh"])
            try:
                term.resize(100, 30)
                term.write("stty size; echo mx-$((40+2)); pwd\n")
                # wait for the last command's output too (pwd), not just the first line: on a busy CI Mac it
                # came in a later read than "mx-42"
                where = os.path.basename(os.path.realpath(d))
                text, offset, end = "", 0, time.monotonic() + 20
                while not ("mx-42" in text and where in text.split("mx-42", 1)[1]) and time.monotonic() < end:
                    offset, more, _ = term.since(offset, timeout=1)
                    text += more
                self.assertIn("mx-42", text)
                self.assertIn("30 100", text)
                self.assertIn(where, text)
                self.assertTrue(term.alive())
            finally:
                term.close()
            self.assertFalse(term.alive())


class AppWindowTests(unittest.TestCase):
    """--app on macOS / Linux: a pywebview window when it can be had, else the default browser."""

    def test_choose_window_or_browser(self):
        env = {"DISPLAY": ":0"}
        with patch.object(native, "webview_module", lambda: object()):
            self.assertEqual(native.choose(["--app"], env), "window")
            self.assertEqual(native.choose(["--app", "--browser"], env), "browser")
            self.assertEqual(native.choose(["--app"], {**env, "MAXEDCODE_APP_BROWSER": "1"}), "browser")
            self.assertEqual(native.choose(["--app", "--no-window"], env), "none")
            with patch.object(native.sys, "platform", "linux"):
                self.assertEqual(native.choose(["--app"], {}), "browser")       # no X11 / Wayland session
            with patch.object(native.sys, "platform", "darwin"):
                self.assertEqual(native.choose(["--app"], {}), "window")
        with patch.object(native, "webview_module", lambda: None):
            self.assertEqual(native.choose(["--app"], env), "browser")

    def launch(self, how, shown=True, opened=True):
        calls = []
        with patch.object(native, "choose", lambda argv: how), \
                patch.object(native, "ensure_server", lambda port: "http://127.0.0.1:3077/?t=abc"), \
                patch.object(native, "show_window", lambda url: calls.append(("window", url)) or shown), \
                patch.object(native, "quit_server", lambda url, browser=False: calls.append(("quit", browser))), \
                patch.object(native.plat, "open_path", lambda url: calls.append(("browser", url)) or opened), \
                patch.object(native, "say", lambda t: calls.append(("say", t))):
            self.assertEqual(native.launch(["--app"]), 0)
        return calls

    def test_window_close_tells_the_server_as_the_exe_does(self):
        self.assertEqual(self.launch("window"), [("window", "http://127.0.0.1:3077/?t=abc"), ("quit", False)])

    def test_a_window_that_cannot_start_falls_back_to_the_browser_calmly(self):
        calls = self.launch("window", shown=False)
        self.assertEqual([c[0] for c in calls], ["window", "browser", "say", "quit"])
        self.assertEqual(calls[2], ("say", "opened in your web browser"))     # one calm line, no error text
        self.assertEqual(calls[-1], ("quit", True))       # a tab: the server exits once no page asks

    def test_without_pywebview_the_browser(self):
        url = "http://127.0.0.1:3077/?t=abc"
        self.assertEqual(self.launch("browser"), [("browser", url), ("say", "opened in your web browser"),
                                                  ("quit", True)])
        self.assertEqual(self.launch("browser", opened=False)[0], ("browser", url))

    def test_backends_are_tried_in_turn(self):
        tried = []
        with patch.object(native, "window_process", lambda url, gui: tried.append(gui) or gui == "gtk"), \
                patch.object(native.sys, "platform", "linux"):
            self.assertTrue(native.show_window("u"))
        self.assertEqual(tried, [None, "qt", "gtk"])
        tried.clear()
        with patch.object(native, "window_process", lambda url, gui: tried.append(gui) or False), \
                patch.object(native.sys, "platform", "darwin"):
            self.assertFalse(native.show_window("u"))
        self.assertEqual(tried, [None])

    def test_a_failing_window_process_stays_off_screen(self):
        """A backend that cannot start reports "not shown"; what it printed goes to the log, not the screen."""
        with tempfile.TemporaryDirectory() as d, patch.object(native, "APP_HOME", Path(d)):
            shown = native.window_process("http://127.0.0.1:1/?t=x", "no-such-gui")
            log = (Path(d) / "window.log").read_text(encoding="utf-8", errors="replace")
        self.assertFalse(shown)
        self.assertIn("gui=no-such-gui", log)
        self.assertIn("Error", log)                 # no pywebview here, or an unknown backend: a traceback

    def test_server_main_hands_posix_app_to_the_launcher(self):
        with patch.object(server, "os", PosixOs()), patch.object(native, "launch", lambda argv: 7):
            with self.assertRaises(SystemExit) as stop:
                server.main(["local_agent.py", "--app"])
        self.assertEqual(stop.exception.code, 7)

    def test_dropped_paths_reach_the_page(self):
        bridge = native.Bridge()
        bridge._on_drop({"dataTransfer": {"files": [{"name": "a", "pywebviewFullPath": "/home/u/a"},
                                                    {"name": "b", "pywebviewFullPath": "/home/u/b"}]}})
        self.assertEqual(bridge.take_drop(2), ["/home/u/a", "/home/u/b"])
        self.assertIsNone(bridge.take_drop(1, timeout=0.05))           # taken: nothing left
        threading.Timer(0.05, bridge._on_drop, args=({"dataTransfer": {"files": [{"name": "c"}]}},)).start()
        self.assertIsNone(bridge.take_drop(1, timeout=1))               # an old pywebview: no paths
        bridge._on_drop({"dataTransfer": {"files": [{"pywebviewFullPath": "/x"}]}})
        self.assertIsNone(bridge.take_drop(2))                          # not this drop

    def test_bridge_dialogs_and_links(self):
        bridge = native.Bridge()
        asked = {}

        class Window:
            def create_file_dialog(self, kind, directory="", **kw):
                asked.update(kind=kind, directory=directory, **kw)
                return ("/home/u/proj",)
        bridge._window = Window()
        fake = SimpleNamespace(FOLDER_DIALOG=20, OPEN_DIALOG=10)
        with patch.dict(sys.modules, {"webview": fake}):
            self.assertEqual(bridge.pick_folder(str(Path.home()), "Add"), "/home/u/proj")
            self.assertEqual(asked["kind"], 20)
            self.assertEqual(bridge.pick_files("/nowhere"), ["/home/u/proj"])
            self.assertEqual(asked, {"kind": 10, "directory": str(Path.home()), "allow_multiple": True})
        with patch.object(native.plat, "open_path", lambda url: True):
            self.assertTrue(bridge.open_url("https://example.com"))
            self.assertFalse(bridge.open_url("file:///etc/passwd"))
        self.assertNotIn("_window", [k for k in dir(bridge) if not k.startswith("_")])

    def test_reuses_a_running_server_and_quits_it_like_the_exe(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d, patch.object(native, "APP_HOME", Path(d)):
                url = f"http://127.0.0.1:{port}/?t={server.TOKEN}"
                native.url_file(port).write_text(url, encoding="ascii")
                self.assertEqual(native.ensure_server(port), url)
                native.url_file(port).write_text(url[:-3] + "bad", encoding="ascii")
                with patch.object(native, "say", lambda t: None):
                    self.assertIsNone(native.ensure_server(port))       # someone else's server on the port
                native.quit_server(url, browser=True)
                self.assertTrue(server.QUIT_WHEN_IDLE.is_set())
                self.assertTrue(server.QUIT_STICKY.is_set())
        finally:
            server.QUIT_WHEN_IDLE.clear()
            server.QUIT_STICKY.clear()
            httpd.shutdown()
            httpd.server_close()


class AppServerPosixTests(unittest.TestCase):
    def test_folder_dialogs_trash_and_open_in(self):
        runs = []
        with patch.object(server, "os", PosixOs()), \
                patch.object(server.plat, "pick_folder", lambda start, title: "/picked"), \
                patch.object(server.plat, "pick_files", lambda start: ["/a", "/b"]), \
                patch.object(server.plat, "trash", lambda p: runs.append(("trash", str(p)))), \
                patch.object(server.plat, "open_path", lambda p: runs.append(("open", str(p))) or True), \
                patch.object(server.plat, "_detached", lambda cmd, cwd=None: runs.append(("run", cmd, cwd)) or True):
            self.assertEqual(server.pick_folder(_HOME.name, "Add"), "/picked")
            self.assertEqual(server.pick_file(_HOME.name), ["/a", "/b"])
            server.recycle("/tmp/x")
            with patch.object(server.sys, "platform", "linux"), \
                    patch.object(server.shutil, "which", which_only("xdg-open", "code", "konsole")):
                apps = server.open_apps()
                self.assertEqual(set(apps), {"explorer", "vscode", "terminal"})
                self.assertTrue(server.open_in(_HOME.name, "explorer"))
                self.assertTrue(server.open_in(_HOME.name, "vscode"))
                self.assertTrue(server.open_in(_HOME.name, "terminal"))
                self.assertFalse(server.open_in(_HOME.name, "gitbash"))
        self.assertEqual(runs[0], ("trash", "/tmp/x"))
        self.assertEqual(runs[1][0], "open")
        self.assertEqual(runs[2], ("run", ["/usr/bin/code", server.folder(_HOME.name)], None))
        self.assertEqual(runs[3][1][:2], ["/usr/bin/konsole", "--workdir"])


if __name__ == "__main__":
    unittest.main()
