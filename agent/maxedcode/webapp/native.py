"""The app window on macOS and Linux (Windows has its own exe, desktop/app/maxedcode.exe, which starts the server
itself: nothing here runs there).

`python local_agent.py --app` (and `maxed app`) does what the exe does:

* the server: the one already running on the port is reused (its URL with the token is kept in
  ~/.maxedcode-app/app-url-<port>.txt and still accepted), else a new one is started in the background
  (`--app --serve`, its own session, output in ~/.maxedcode-app/server-<port>.log);
* the window: a native one by pywebview when it is installed and can start (macOS WKWebView; Linux pywebview's
  own pick, then Qt, then GTK, each tried in a process of its own so a backend that fails shows nothing), else
  the default browser, said in one calm line (what went wrong is in ~/.maxedcode-app/window.log);
* closing the window: as closing the exe's window, POST /api/quit: a running turn goes on, the server exits once
  no turn runs and no page has come back for a while.

The page gets what only a desktop window can give through `window.pywebview.api` (Bridge): the folder and file
dialogs, real paths of dropped files and folders, external links in the system browser, notifications.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .. import plat

APP_HOME = Path.home() / ".maxedcode-app"
AGENT_DIR = Path(__file__).resolve().parents[2]
AGENT = AGENT_DIR / "local_agent.py"
TITLE = "maxedcode"
SIZE = (1320, 860)
MIN_SIZE = (720, 480)
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NOREDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def say(text):
    print(f"maxedcode app: {text}", flush=True)


def icon():
    """desktop/maxedcode.png: next to the agent folder in the repo, inside it in an installed copy."""
    for d in (AGENT_DIR / "desktop", AGENT_DIR.parent / "desktop"):
        p = d / "maxedcode.png"
        if p.is_file():
            return str(p)
    return None


def port_of(argv, default=3077):
    if "--port" in argv:
        return int(argv[argv.index("--port") + 1])
    return default


def has_display(env=None):
    """Linux needs an X11 or Wayland session for any window; macOS always has one."""
    env = os.environ if env is None else env
    return sys.platform == "darwin" or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def webview_module():
    """pywebview, or None when it is not installed (an optional package: the browser is used then)."""
    try:
        import webview
        return webview
    except Exception:
        return None


def choose(argv, env=None):
    """How to show the page: "window" (pywebview), "browser" (the default browser) or "none" (--no-window)."""
    env = os.environ if env is None else env
    if "--no-window" in argv:
        return "none"
    if "--browser" in argv or env.get("MAXEDCODE_APP_BROWSER") == "1":
        return "browser"
    if not has_display(env):
        return "browser"
    return "window" if webview_module() is not None else "browser"


# ---------- the server ----------

def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def token_accepted(url):
    """A valid token answers with the redirect that sets the cookie; a stale one gets 401."""
    try:
        with NOREDIRECT.open(url, timeout=3) as r:
            return r.status == 302
    except urllib.error.HTTPError as e:
        e.close()
        return e.code == 302
    except (OSError, ValueError):
        return False


def url_file(port):
    return APP_HOME / f"app-url-{port}.txt"


def ensure_server(port, wait=90):
    """The page URL with its token: the running server's, or a new one started in the background. None + a
    reason printed when it cannot be had."""
    if listening(port):
        try:
            known = url_file(port).read_text(encoding="utf-8").strip()
        except OSError:
            known = ""
        if f"127.0.0.1:{port}/" in known and token_accepted(known):
            return known
        say(f"port {port} is taken by another program: start with --port <other>")
        return None
    APP_HOME.mkdir(parents=True, exist_ok=True)
    log = APP_HOME / f"server-{port}.log"
    err = APP_HOME / f"server-{port}.log.err"
    cmd = [sys.executable, str(AGENT), "--app", "--serve", "--port", str(port)]
    with open(log, "wb") as out, open(err, "wb") as errs:
        proc = subprocess.Popen(cmd, cwd=str(Path.home()), stdin=subprocess.DEVNULL, stdout=out, stderr=errs,
                                close_fds=True, start_new_session=True,
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    start = time.monotonic()
    end, told = start + wait, False
    while time.monotonic() < end:
        time.sleep(0.15)
        if not told and time.monotonic() - start > 3:
            # the first start of a fresh install can take a while (macOS checks the new files once)
            say("starting ...")
            told = True
        try:
            m = re.search(r"maxedcode app: (http\S+)", log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            url_file(port).write_text(m.group(1), encoding="ascii")
            return m.group(1)
        if proc.poll() is not None:
            break
    with_err = ""
    try:
        with_err = err.read_text(encoding="utf-8", errors="replace").strip()[-1500:]
    except OSError:
        pass
    say("the server did not start" + (f":\n{with_err}" if with_err else f" (see {log})"))
    return None


def quit_server(url, browser=False):
    """The window closed: the server exits once idle (a running turn goes on). `browser`: the page is a browser
    tab whose closing nobody reports, so the server keeps that wish even when the page loads again and exits
    once no page has asked anything for a while (an open page pings every 10 s)."""
    try:
        quit_url = url.replace("/?t=", "/api/quit?t=") + ("&browser=1" if browser else "")
        req = urllib.request.Request(quit_url, data=b"", method="POST")
        with NOPROXY.open(req, timeout=3):
            pass
    except (OSError, ValueError):
        pass


# ---------- the window ----------

class Bridge:
    """window.pywebview.api for the page. Public methods only: pywebview exposes every public attribute, so the
    window and the drop queue are private."""

    def __init__(self):
        self._window = None
        self._drops = []                  # [(monotonic time, [paths])] of the last drops
        self._dropped = threading.Condition()

    def info(self):
        return {"shell": "pywebview", "platform": sys.platform}

    def _dialog(self, kind, start, **kw):
        import webview
        kinds = getattr(webview, "FileDialog", None)
        value = getattr(kinds, kind, None) if kinds else None
        if value is None:
            value = getattr(webview, {"FOLDER": "FOLDER_DIALOG", "OPEN": "OPEN_DIALOG"}[kind])
        start = str(start or "")
        if not os.path.isdir(start):
            start = str(Path.home())
        return self._window.create_file_dialog(value, directory=start, **kw) or ()

    def pick_folder(self, start="", title=""):
        """The system "choose a folder" dialog over the window. -> a path or None."""
        try:
            got = self._dialog("FOLDER", start)
        except Exception:
            return plat.pick_folder(start, title)
        got = [got] if isinstance(got, str) else list(got)
        return str(got[0]) if got else None

    def pick_files(self, start=""):
        """The system "open files" dialog, several files allowed. -> [paths]."""
        try:
            got = self._dialog("OPEN", start, allow_multiple=True)
        except Exception:
            return plat.pick_files(start)
        return [str(x) for x in ([got] if isinstance(got, str) else got)]

    def open_url(self, url):
        """An outside address in the system browser (the window itself only shows maxedcode)."""
        url = str(url or "")
        if urlsplit(url).scheme not in ("http", "https", "mailto"):
            return False
        return plat.open_path(url)

    def notify(self, title, text=""):
        return plat.notify(title, text)

    def _on_drop(self, event):
        """pywebview's DOM drop event: the real paths of what was dropped (the page itself only gets names)."""
        try:
            files = (event.get("dataTransfer") or {}).get("files") or []
            paths = [str(f.get("pywebviewFullPath") or "") for f in files]
        except Exception:
            paths = []
        with self._dropped:
            self._drops.append((time.monotonic(), paths))
            del self._drops[:-4]
            self._dropped.notify_all()

    def take_drop(self, count=0, timeout=1.5):
        """The paths of the drop the page is handling now: pywebview reports it to Python about when the page sees
        it, so the newest one from the last moments counts (an older or doubled report is dropped).
        -> [paths] or None when this pywebview gives no paths."""
        asked = time.monotonic()
        end = asked + timeout
        with self._dropped:
            while not any(t >= asked - timeout for t, _ in self._drops) and time.monotonic() < end:
                self._dropped.wait(max(0.01, end - time.monotonic()))
            fresh = [paths for t, paths in self._drops if t >= asked - timeout]
            self._drops.clear()
        paths = fresh[-1] if fresh else []
        if not any(paths) or (count and len(paths) != int(count)):
            return None
        return paths


def _watch_drops(window, bridge):
    """Subscribes to drop events (pywebview 5+ DOM API); an older pywebview simply has no paths for drops."""
    try:
        from webview.dom import DOMEventHandler
        window.dom.document.events.drop += DOMEventHandler(bridge._on_drop, prevent_default=False,
                                                           stop_propagation=False)
    except Exception:
        pass


def run_window(url, gui=None, on_shown=None):
    """Shows the page in a native window and returns when the user closes it. Raises when pywebview cannot make a
    window here (no Qt/GTK bindings, no display). `gui`: pywebview's backend ("qt", "gtk"; None = its own pick)."""
    import webview
    bridge = Bridge()
    window = webview.create_window(TITLE, url, js_api=bridge, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE,
                                   text_select=True)
    if window is None:
        raise RuntimeError("pywebview made no window")
    bridge._window = window
    # every page load: a reloaded page has lost the listener (a doubled report of one drop is harmless, take_drop
    # uses the newest)
    window.events.loaded += lambda *_: _watch_drops(window, bridge)
    if on_shown:
        window.events.shown += lambda *_: on_shown()
    store = APP_HOME / "webview"
    store.mkdir(parents=True, exist_ok=True)
    # a profile of its own that persists: the page keeps its folders, projects and settings in localStorage
    options = {"private_mode": False, "storage_path": str(store)}
    if gui:
        options["gui"] = gui
    pic = icon()
    if pic and sys.platform.startswith("linux"):     # GTK / Qt only (a PNG crashes the WinForms backend)
        options["icon"] = pic
    while True:
        try:
            webview.start(**options)
            return
        except TypeError as e:        # an older pywebview without one of the options: drop it and try again
            bad = next((k for k in list(options) if k != "gui" and k in str(e)), None)
            if not bad:
                raise
            options.pop(bad)


def guis():
    """The pywebview backends to try in turn: its own pick first; on Linux then Qt (what the installer puts into
    the venv) and GTK (when the system has it). macOS has one, Cocoa."""
    return [None] if sys.platform == "darwin" else [None, "qt", "gtk"]


def window_process(url, gui=None):
    """One try at the window in a process of its own: a backend that cannot start (missing libraries, no display)
    may abort the whole process, and nothing of it must reach the user's screen; its output goes to
    ~/.maxedcode-app/window.log. -> True when the window was shown (it has been closed again by now)."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "maxedcode.webapp.native", "--window", url] + (["--gui", gui] if gui else [])
    env = {**os.environ, "PYTHONPATH": str(AGENT_DIR) + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    shown = False
    try:
        with open(APP_HOME / "window.log", "ab") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} gui={gui or 'default'}\n".encode())
            log.flush()
            proc = subprocess.Popen(cmd, cwd=str(AGENT_DIR), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=log)
            with proc.stdout:
                for line in proc.stdout:
                    if line.strip() == b"shown":
                        shown = True
            proc.wait()
    except OSError:
        return False
    return shown


def show_window(url):
    """The native window, trying each backend in turn. -> False when none could show it (the browser is next)."""
    return any(window_process(url, gui) for gui in guis())


def _window_main(argv):
    """`python -m maxedcode.webapp.native --window <url> [--gui qt|gtk]`: the window process (window_process)."""
    url = argv[argv.index("--window") + 1]
    gui = argv[argv.index("--gui") + 1] if "--gui" in argv else None
    run_window(url, gui, on_shown=lambda: print("shown", flush=True))
    return 0


def launch(argv):
    """`--app` on macOS / Linux: the server (reused or started) and its window. -> exit code."""
    port = port_of(argv)
    how = choose(argv)
    url = ensure_server(port)
    if not url:
        return 1
    if how == "none":
        say(url)
        return 0
    if how == "window" and show_window(url):
        quit_server(url)              # closed: as the exe's window, the server exits once idle
        return 0
    if not plat.open_path(url):
        # no browser here (an SSH session): the address is printed, the server stays until stopped
        say(f"open this address in a browser: {url}")
        return 0
    say("opened in your web browser")
    quit_server(url, browser=True)
    return 0


if __name__ == "__main__":
    sys.exit(_window_main(sys.argv))
