"""Stress tests of the app window's HTTP server (maxedcode/webapp/server.py) before a release.

The real server runs in a subprocess on a free port with a temporary home (USERPROFILE, HOME, AGENT_HOME): the
real ~\\.maxedcode and ~\\.maxedcode-app are never touched. Dialogs, "Open in", Explorer and the Recycle Bin are
patched away in that process, and a model server start (maxedcode.launch start/stop) runs a stub script instead.
A turn runs the real headless agent against a tiny stdlib OpenAI-compatible fake model, so no real model is used.

Invariants: no request kills the server or gets a dropped connection, /api answers are valid JSON, 401 without
the cookie, static files never come from outside static/, one model start job at a time, a cancelled job stays
cancelled, a turn's watchers all get the same lines, /api/stop ends a turn, nothing is written outside the
temporary home. MAXEDCODE_STRESS=<rounds> multiplies the rounds (default 1, about a minute).
"""
import ctypes
import http.client
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

AGENT_DIR = Path(__file__).resolve().parents[1]
STATIC = AGENT_DIR / "maxedcode" / "webapp" / "static"
ROUNDS = max(1, int(os.environ.get("MAXEDCODE_STRESS") or 1))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid):
    """Whether process `pid` still runs."""
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ── the fake model: chat completions streamed as SSE; "SLOW" in the last user message streams for long ──
class FakeModel:
    def __init__(self):
        self.calls = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ok = self.path.rstrip("/").endswith("/models")
                body = json.dumps({"data": [{"id": "fake"}]} if ok else {"error": "no"}).encode()
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                fake.calls += 1
                last = next((str(m.get("content")) for m in reversed(req.get("messages") or [])
                             if m.get("role") == "user"), "")
                count, delay = (400, 0.025) if "SLOW" in last else (30, 0.05)

                def chunk(delta, finish=None, **extra):
                    return ("data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 1,
                                                   "model": "fake", "choices": [{"index": 0, "delta": delta,
                                                                                 "finish_reason": finish}],
                                                   **extra}) + "\n\n").encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for i in range(count):
                        self.wfile.write(chunk({"content": f"w{i} "}))
                        self.wfile.flush()
                        time.sleep(delay)
                    self.wfile.write(chunk({}, "stop", usage={"prompt_tokens": 10, "completion_tokens": count,
                                                              "total_tokens": 10 + count}))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass                         # the turn was stopped

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass
        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# A stand-in for `python -m maxedcode.launch start|stop`: progress lines tagged with the job's model, then done.
STUB_LAUNCH = r'''
import json, os, sys, time
body = json.loads(sys.stdin.read() or "{}")
tag, steps = str(body.get("model") or "?"), int(body.get("ctx") or 20)
mark = os.path.join(os.environ["STUB_PIDS"], "%d.%s" % (os.getpid(), tag))
open(mark, "w").close()
for i in range(steps):
    print(json.dumps({"text": "%s step %d" % (tag, i), "pct": i * 100 // steps}), flush=True)
    time.sleep(0.05)
print(json.dumps({"done": True, "ok": True, "text": tag + " ready"}), flush=True)
os.remove(mark)
'''

# The server process: no dialog, no Explorer, no Recycle Bin, the stub instead of a real model server start.
BOOT = r'''
import os, shutil, subprocess, sys
sys.path.insert(0, os.environ["AGENT_DIR"])
STUB = os.environ["STUB_LAUNCH"]
_Popen = subprocess.Popen


class Popen(_Popen):
    def __init__(self, args, *a, **kw):
        if isinstance(args, (list, tuple)):
            args = [str(x) for x in args]
            if args[1:3] == ["-m", "maxedcode.launch"] and args[3:4] in (["start"], ["stop"]):
                args = [args[0], STUB] + args[3:]
            elif args and os.path.basename(args[0]).lower() == "explorer.exe":
                args = [sys.executable, "-c", "pass"]
        super().__init__(args, *a, **kw)


subprocess.Popen = Popen


def handle_error(self, request, client_address):
    """An exception that escaped a handler: one line the test can find."""
    kind, error, _ = sys.exc_info()
    sys.stderr.write("ESCAPED %s: %s\n" % (kind.__name__, error))
    sys.stderr.flush()


import http.server
http.server.ThreadingHTTPServer.handle_error = handle_error
from maxedcode.webapp import server
server.pick_folder = lambda start, title="": None
server.pick_file = lambda start: []
server.open_in = lambda path, app: False
server.recycle = lambda p: shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
os.startfile = lambda *a, **k: None
server.main(["server", "--serve", "--port", sys.argv[1]])   # --serve: the bare server on every system (no window, no browser)
'''


class App:
    """The real server in a subprocess with a temporary home."""

    def __init__(self, root, model_base):
        self.root = root
        self.home = root / "home"
        self.pids = root / "stub-pids"
        for d in (self.home, self.pids):
            d.mkdir(parents=True, exist_ok=True)
        (root / "boot.py").write_text(BOOT, encoding="utf-8")
        (root / "stub_launch.py").write_text(STUB_LAUNCH, encoding="utf-8")
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "MAXEDCODE_"))}
        env.update(USERPROFILE=str(self.home), HOME=str(self.home), AGENT_HOME=str(self.home / ".maxedcode"),
                   AGENT_DIR=str(AGENT_DIR), PYTHONPATH=str(AGENT_DIR), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   MAXEDCODE_OFFLINE="1", MAXEDCODE_NO_UPDATE="1", AGENT_BASE_URL=model_base, AGENT_MODEL="fake",
                   AGENT_SERVER="llamacpp", STUB_LAUNCH=str(root / "stub_launch.py"), STUB_PIDS=str(self.pids),
                   MAXEDCODE_MANAGED_SETTINGS=str(root / "no-managed-settings.json"))
        self.proc = subprocess.Popen([sys.executable, str(root / "boot.py"), str(self.port)], cwd=str(self.home),
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW)
        self.err = []
        threading.Thread(target=lambda: [self.err.append(x.decode("utf-8", "replace").rstrip())
                                         for x in iter(self.proc.stderr.readline, b"")], daemon=True).start()
        line = self.proc.stdout.readline().decode("utf-8", "replace").strip()
        threading.Thread(target=self.proc.stdout.read, daemon=True).start()
        if "?t=" not in line:
            time.sleep(0.5)
            raise RuntimeError(f"the server did not start: {line!r}\n" + "\n".join(self.err[-30:]))
        self.token = line.split("?t=", 1)[1]
        status, headers, _ = self.request("GET", "/?t=" + self.token, auth=False)
        assert status == 302, status
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def request(self, method, path, body=None, auth=True, headers=None, timeout=30, raw=None):
        """-> (status, lower-case headers, body bytes); raises on a dropped connection."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            h = dict(headers or {})
            if auth:
                h.setdefault("Cookie", self.cookie)
            data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def api(self, method, path, body=None, **kw):
        status, headers, data = self.request(method, path, body, **kw)
        return status, json.loads(data) if "json" in headers.get("content-type", "") else data

    def raw(self, data, read=True, timeout=10, close_after=None):
        """Bytes straight on the socket -> the reply bytes (until the server closes)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        try:
            for part in (data if isinstance(data, list) else [data]):
                if isinstance(part, (int, float)):
                    time.sleep(part)
                else:
                    s.sendall(part)
            if close_after is not None:
                time.sleep(close_after)
                return b""
            out = b""
            while read:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            s.close()

    def stub_alive(self):
        out = []
        for f in self.pids.iterdir():
            pid = int(f.name.split(".", 1)[0])
            if alive(pid):
                out.append(f.name)
        return out

    def escaped_errors(self):
        """Tracebacks that escaped a handler (socketserver printed them), apart from clients that went away."""
        gone = ("ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "TimeoutError")
        return [x for x in self.err if x.startswith("ESCAPED ") and not x[8:].startswith(gone)]

    def stop(self):
        if self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW)
            self.proc.kill()
        with __import__("contextlib").suppress(Exception):
            self.proc.wait(10)


class StaticScriptTests(unittest.TestCase):
    def test_every_static_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        scripts = sorted(STATIC.glob("*.js"))
        self.assertTrue(any(f.name == "app.js" for f in scripts))
        for f in scripts:
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, creationflags=NO_WINDOW)
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr[-2000:]}")


# ── the model start job in-process, with fake processes whose output the test controls ──
class FakeProc:
    def __init__(self, args):
        self.args, self.q, self.returncode = args, queue.Queue(), None
        self.pid = random.randint(10_000, 99_999)
        self.finished, self.drained = threading.Event(), threading.Event()
        self.stdin = SimpleNamespace(write=lambda s: None, close=lambda: None)
        self.stdout = self.lines()

    def lines(self):
        while True:
            x = self.q.get()
            if x is None:
                self.drained.set()
                return
            if isinstance(x, threading.Event):   # the reader is held here: what follows is "still in the pipe"
                x.wait(5)
                continue
            yield x

    def say(self, **obj):
        self.q.put(json.dumps(obj) + "\n")

    def end(self, code=0):
        if self.returncode is None:
            self.returncode = code
        self.q.put(None)
        self.finished.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.finished.wait(timeout)
        return self.returncode

    def kill(self):
        self.end(1)


class LaunchJobTests(unittest.TestCase):
    """launch_begin / launch_job with fake processes: what the page polls stays true under races."""

    def setUp(self):
        from maxedcode.webapp import server
        self.s, self.procs = server, []

        def popen(args, **kw):
            p = FakeProc(args)
            self.procs.append(p)
            return p
        fake_sub = SimpleNamespace(Popen=popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL, run=subprocess.run)
        # kill_tree as the real one: only a process that still runs is killed (its output may still be in the pipe)
        for p in (patch.object(server, "subprocess", fake_sub), patch.dict(server.LAUNCH, {"job": None}),
                  patch.object(server, "kill_tree", lambda proc: proc.poll() is None and proc.end(1))):
            p.start()
            self.addCleanup(p.stop)

    def settle(self, proc):
        self.assertTrue(proc.drained.wait(5))
        time.sleep(0.05)                     # the pump's last statements after the pipe ends

    def test_a_cancelled_job_stays_cancelled_when_its_last_line_comes_late(self):
        job = self.s.launch_begin({"do": "start", "kind": "strata", "model": "A"})
        p = self.procs[-1]
        p.say(text="A loading", pct=10)
        # the child writes "done" into the pipe just before it is killed: the pump reads it only afterwards
        held = threading.Event()
        p.q.put(held)
        p.say(done=True, ok=True, text="A ready")
        time.sleep(0.1)
        cancelled = self.s.launch_begin({"do": "cancel"})
        self.assertEqual((cancelled["id"], cancelled["result"]), (job["id"], {"ok": False, "error": "cancelled"}))
        held.set()
        self.settle(p)
        now = self.s.launch_job()
        self.assertEqual(now["id"], job["id"])
        self.assertEqual(now["result"], {"ok": False, "error": "cancelled"})

    def test_the_next_jobs_lines_never_mix_into_the_old_one(self):
        a = self.s.launch_begin({"do": "start", "model": "A"})
        pa = self.procs[-1]
        b = self.s.launch_begin({"do": "start", "model": "B"})
        pb = self.procs[-1]
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNotNone(pa.returncode)              # the old start was stopped
        pa.say(text="A late line", pct=50)
        pb.say(text="B step", pct=20)
        pa.end(1)
        time.sleep(0.2)
        now = self.s.launch_job()
        self.assertEqual((now["id"], now["model"], now["lines"], now["result"]), (b["id"], "B", ["B step"], None))
        pb.say(done=True, ok=True, text="B ready")
        pb.end(0)
        self.settle(pb)
        self.assertEqual(self.s.launch_job()["result"]["text"], "B ready")

    def test_racing_starts_and_cancels_leave_one_job(self):
        rnd = random.Random(7)

        def worker(n):
            for i in range(25 * ROUNDS):
                do = rnd.choice(["start", "start", "stop", "cancel"])
                out = self.s.launch_begin({"do": do, "model": f"m{n}-{i}"})
                json.dumps(out)                          # always something the page can take
                job = self.s.launch_job()
                self.assertTrue(job is None or {"id", "do", "lines", "result", "secs", "age"} <= set(job))
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(worker, range(8)))
        running = [p for p in self.procs if p.returncode is None]
        self.assertLessEqual(len(running), 1)
        current = self.s.LAUNCH["job"]
        if running:
            self.assertIs(current["proc"], running[0])
        self.s.launch_begin({"do": "cancel"})
        self.assertFalse([p for p in self.procs if p.returncode is None])
        self.assertEqual(self.s.launch_job()["result"], {"ok": False, "error": "cancelled"})


# ── the real server ──
class WebAppStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="mx-webapp-stress-"))
        cls.uid = uuid.uuid4().hex[:10]
        cls.real_home = Path.home()
        cls.config_json = AGENT_DIR / "config.json"
        cls.config_mtime = cls.config_json.stat().st_mtime if cls.config_json.exists() else None
        cls.static_files = sorted(p.name for p in STATIC.iterdir())
        cls.model = FakeModel()
        cls.app = App(cls.root, cls.model.base)
        cls.work = cls.root / "work"
        cls.work.mkdir()
        (cls.work / "a.txt").write_text("alpha\n", encoding="utf-8")
        (cls.work / "b.png").write_bytes((STATIC / "icon.png").read_bytes())
        cls.static_bodies = {p.read_bytes() for p in STATIC.iterdir() if p.is_file()}

    @classmethod
    def tearDownClass(cls):
        try:
            status, data = cls.app.api("GET", "/api/ping")
            assert status == 200 and isinstance(data, dict), "the server stopped answering"
        finally:
            cls.app.stop()
            cls.model.stop()
            shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.app = type(self).app

    def assertStatic(self, body, what):
        """`body` is one of the static files (as they are now: another change may have edited one)."""
        if body not in self.static_bodies:
            self.assertIn(body, {p.read_bytes() for p in STATIC.iterdir() if p.is_file()},
                          f"{what!r} served something that is not a static file")

    def assertJson(self, status, headers, body, what):
        self.assertIn("json", headers.get("content-type", ""), f"{what}: {status} {body[:300]!r}")
        json.loads(body)

    def test_zz_the_server_survived_and_wrote_nothing_outside_its_home(self):
        status, data = self.app.api("GET", "/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(self.app.proc.poll(), None)
        self.assertEqual(self.app.escaped_errors(), [])
        uid = self.uid
        real_app = self.real_home / ".maxedcode-app"
        self.assertFalse((self.real_home / ".maxedcode" / "projects" / f"stress-{uid}").exists())
        self.assertFalse((real_app / "approvals" / f"a{int(uid, 16) % 10**12}.json").exists())
        with __import__("contextlib").suppress(OSError):
            self.assertNotIn(f"app-stress-{uid}", (real_app / "archived.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in STATIC.iterdir()), self.static_files)
        if self.config_mtime is not None:
            self.assertEqual(self.config_json.stat().st_mtime, self.config_mtime, "agent/config.json was written")

    def test_without_the_cookie_every_route_is_refused(self):
        a = self.app
        paths = ["/", "/index.html", "/app.js", "/api/ping", "/api/sessions", "/api/session?id=app-1",
                 "/api/settings", "/api/launch?job=1", "/api/info", "/api/ls?path=C:\\", "/api/file?path=C:\\x",
                 "/api/raw?path=x.png", "/api/watch?session=x", "/api/pty-list", "/api/nope", "/../server.py",
                 f"/api/sessions?t={a.token}"]
        posts = ["/api/turn", "/api/stop", "/api/fs", "/api/settings", "/api/launch", "/api/upload", "/api/lang",
                 "/api/pty-open", "/api/approve", "/api/nope", "/api/quit", "/api/quit?t=wrong", "/api/quit?t=%C3%A9"]
        bad = [None, "mxapp=", "mxapp=wrong", f"mxapp={a.token[:-1]}", f"mxapp={a.token}x", "other=" + a.token,
               "mxapp=\u00e9\u00e9", f'mxapp="{a.token}"x', "mxapp=a; mxapp", "\x00", ";" * 500]

        def check(args):
            method, path, cookie = args
            h = {} if cookie is None else {"Cookie": cookie}
            status, headers, body = a.request(method, path, {"prompt": "x", "do": "start"} if method == "POST" else None,
                                              auth=False, headers=h)
            self.assertEqual(status, 401, f"{method} {path} cookie={cookie!r}: {status} {body[:200]!r}")
            self.assertLess(len(body), 100)
        jobs = [("GET", p, c) for p in paths for c in bad] + [("POST", p, c) for p in posts for c in bad]
        jobs += [("GET", f"/?t={t}", None) for t in ("", "wrong", a.token[:-1], "%C3%A9%C3%A9", "%E9", a.token + "%00",
                                                     a.token + "%C3%A9")]
        with ThreadPoolExecutor(16) as pool:
            list(pool.map(check, jobs * ROUNDS))
        # the right token on another path is not a login
        status, headers, _ = a.request("GET", f"/api/ping?t={a.token}", auth=False)
        self.assertEqual(status, 401)

    def test_static_files_never_come_from_outside_static(self):
        a = self.app
        server_py = str(AGENT_DIR / "maxedcode" / "webapp" / "server.py")
        tries = ["/../server.py", "/..%2fserver.py", "/%2e%2e/server.py", "/%2e%2e%2fserver.py", "/..\\server.py",
                 "/..%5cserver.py", "/static/../../server.py", "/./../server.py", "/....//....//server.py",
                 "/app.js/../../server.py", "/%252e%252e/server.py", "/..\\..\\local_agent.py",
                 "/../../local_agent.py", "/../../config.json", "/C:/Windows/win.ini", "/C:\\Windows\\win.ini",
                 "/c%3a/windows/win.ini", "/" + server_py, "/" + server_py.replace("\\", "/"),
                 "/\\\\?\\" + server_py, "/\\\\.\\" + server_py, "//etc/passwd", "/con", "/nul", "/aux.js",
                 "/app.js::$DATA", "/index.html%00.js", "/.", "/..", "/static", "/%00", "/index.html/",
                 "/app.js?x=../../server.py", "/app.js#../server.py", "/" + "../" * 30 + "Windows/win.ini"]
        for path in tries * ROUNDS:
            status, headers, body = a.request("GET", path)
            self.assertLess(status, 500, f"{path}: {status} {body[:200]!r}")
            if status == 200:
                self.assertStatic(body, path)
        # raw bytes the client library would not send: a NUL, a bare backslash path, a drive path
        for line in (b"GET /..\\..\\server.py HTTP/1.0", b"GET \\..\\server.py HTTP/1.0",
                     b"GET C:\\Windows\\win.ini HTTP/1.0", b"GET /\x00 HTTP/1.0", b"GET /%2e%2e/%2e%2e/x HTTP/1.0"):
            out = a.raw(line + b"\r\nCookie: " + a.cookie.encode() + b"\r\n\r\n")
            head, _, body = out.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/"), f"{line!r}: dropped ({out[:100]!r})")
            if b" 200 " in head.split(b"\r\n")[0]:
                self.assertStatic(body, line)
        for name in ("/", "/app.js", "/app.css", "/vt.js", "/icon.png"):
            status, headers, body = a.request("GET", name)
            self.assertEqual(status, 200, name)

    def test_concurrent_requests_to_every_route(self):
        a, w, uid = self.app, str(self.work), self.uid
        gets = ["/api/ping", "/api/pty-list", "/api/sessions", "/api/session?id=app-none", "/api/session?id=../x",
                "/api/session", "/api/session?id=" + "x" * 200, "/api/watch?session=nope", "/api/watch",
                "/api/watch?session=x&from=abc", "/api/info", "/api/apps", f"/api/ls?path={w}",
                f"/api/ls?root={w}&hidden=1", "/api/ls?path=relative&root=" + w, f"/api/raw?path={w}\\b.png",
                f"/api/raw?path={w}\\a.txt", f"/api/file?path={w}\\a.txt", f"/api/file?path={w}",
                f"/api/file?path={w}\\missing", f"/api/find?root={w}&q=a", f"/api/find?root={w}",
                f"/api/mtime?path={w}\\a.txt", "/api/mtime?path=%00", "/api/search?q=hello", "/api/search?q=",
                "/api/search?q=%22%27%2A%29%28AND+OR+NEAR", "/api/update", "/api/launch?job=1", "/api/nope",
                "/api/session?id=app-x&cwd=%00", "/api/ls?path=%00", "/api/file?path=%00", "/api/raw?path=%00"]
        posts = [("/api/stop", {"session": "nope"}), ("/api/stop", {}), ("/api/delete", {"session": "../x"}),
                 ("/api/delete", {"session": f"app-gone-{uid}"}), ("/api/archive", {"session": f"app-stress-{uid}"}),
                 ("/api/archive", {"session": f"app-stress-{uid}", "on": False}), ("/api/archive", {"session": "a b"}),
                 ("/api/rename", {"session": "app-none", "title": "t"}), ("/api/rename", {"session": "../x"}),
                 ("/api/fork", {"session": "app-none", "turns": 1}), ("/api/fork", {"session": "app-x", "turns": "x"}),
                 ("/api/fs", {"do": "mkdir", "path": w, "name": "../x"}), ("/api/fs", {"do": "touch", "path": "rel"}),
                 ("/api/fs", {"do": "nope", "path": w}), ("/api/fs", {"do": "delete", "path": w + "\\missing"}),
                 ("/api/new-project", {"name": "CON"}), ("/api/new-project", {"name": ""}),
                 ("/api/workspace", {"path": "relative"}), ("/api/lang", {"lang": "en"}), ("/api/lang", {"lang": "xx"}),
                 ("/api/approve", {"id": "a1", "decision": "maybe"}), ("/api/approve", {"id": "../x", "decision": "allow"}),
                 ("/api/pick-folder", {"start": w}), ("/api/pick-file", {"start": w}), ("/api/open", {"path": w}),
                 ("/api/pty-write", {"id": "nope", "data": "x"}), ("/api/pty-close", {"id": "nope"}),
                 ("/api/browser", {"do": "nope"}), ("/api/launch", {"do": "bogus"}), ("/api/nope", {}),
                 ("/api/turn", {}), ("/api/turn", {"prompt": "x", "session": "../x"}), ("/api/turn", {"prompt": "  "}),
                 ("/api/btw", {}), ("/api/btw", {"session": "app-x"}), ("/api/update", {})]
        counter = iter(range(10**9))
        lock = threading.Lock()

        def one(job):
            kind, item = job
            if kind == "get":
                status, headers, body = a.request("GET", item)
                if item.startswith("/api/") and item != "/api/nope" and not item.startswith("/api/raw?path=" + w + "\\b"):
                    self.assertJson(status, headers, body, item)
            elif kind == "post":
                status, headers, body = a.request("POST", item[0], item[1])
                self.assertJson(status, headers, body, item[0])
            else:                                # writes that must land in the temp folders, under unique names
                with lock:
                    n = next(counter)
                status, data = {
                    "touch": lambda: a.api("POST", "/api/fs", {"do": "touch", "path": w, "name": f"t{n}.txt"}),
                    "mkdir": lambda: a.api("POST", "/api/fs", {"do": "mkdir", "path": w, "name": f"d{n}"}),
                    "upload": lambda: a.api("POST", "/api/upload", raw=f"{uid}-{n}".encode(),
                                            headers={"X-File-Name": random.choice(
                                                ["..%2F..%2Fevil.txt", "..", ".", "a%5Cb.txt", "C:evil", "x.txt:ads",
                                                 "NUL", "con.txt", "%00", "\u00e9.txt", "ok.txt"])}),
                }[item]()
                self.assertLess(status, 500, f"{item}: {data}")
                if item == "upload" and status == 200:
                    attachments = (a.home / ".maxedcode-app" / "attachments").resolve()
                    self.assertIn(attachments, Path(data["path"]).resolve().parents, data)
            return 1
        jobs = [("get", g) for g in gets] + [("post", p) for p in posts] + [("write", k) for k in ("touch", "mkdir",
                                                                                                  "upload")] * 6
        jobs = jobs * 4 * ROUNDS
        random.Random(3).shuffle(jobs)
        with ThreadPoolExecutor(24) as pool:
            self.assertEqual(sum(pool.map(one, jobs)), len(jobs))
        # the heavy routes (each one a maxedcode module in its own process), a few at a time
        heavy = [("GET", "/api/settings", None), ("GET", "/api/cloud", None), ("GET", f"/api/cli?cwd={w}", None),
                 ("GET", "/api/browser", None), ("GET", "/api/launch", None),
                 ("POST", "/api/settings", {"temperature": "99"}), ("POST", "/api/cloud", {"do": "nope"}),
                 ("POST", "/api/cli", {"cwd": w, "do": "nope"}), ("POST", "/api/expand", {"cwd": w, "name": "nope"}),
                 ("POST", "/api/launch", {"do": "way-remove", "id": "nope"}), ("POST", "/api/update", {"check": True})]

        def heavy_one(job):
            status, headers, body = a.request(job[0], job[1], job[2], timeout=120)
            self.assertEqual(status, 200, f"{job}: {body[:300]!r}")
            self.assertJson(status, headers, body, job[1])
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(heavy_one, heavy * ROUNDS))
        # writes that went where they should: the temp home
        self.assertTrue(list(self.work.glob("t*.txt")) and list(self.work.glob("d*")))
        status, data = a.api("POST", "/api/new-project", {"name": f"stress-{uid}"})
        self.assertEqual(status, 200)
        self.assertTrue(Path(data["path"]).is_relative_to(a.home))
        rid = f"a{int(uid, 16) % 10**12}"
        self.assertEqual(a.api("POST", "/api/approve", {"id": rid, "decision": "deny"})[0], 200)
        self.assertTrue((a.home / ".maxedcode-app" / "approvals" / f"{rid}.json").exists())
        self.assertEqual(a.api("POST", "/api/archive", {"session": f"app-stress-{uid}"})[0], 200)
        self.assertIn(f"app-stress-{uid}", (a.home / ".maxedcode-app" / "archived.json").read_text(encoding="utf-8"))

    def test_bodies_of_the_wrong_shape_get_an_answer(self):
        a = self.app
        routes = ["/api/stop", "/api/delete", "/api/archive", "/api/rename", "/api/fork", "/api/fs",
                  "/api/new-project", "/api/workspace", "/api/lang", "/api/browser", "/api/approve", "/api/pick-folder",
                  "/api/open", "/api/pick-file", "/api/pty-write", "/api/pty-resize", "/api/pty-close", "/api/launch",
                  "/api/turn", "/api/btw", "/api/update", "/api/nope"]
        shapes = [b"", b"{", b"not json", b"[]", b"[1,2]", b'"text"', b"123", b"null", b"true", b"\xff\xfe\x00",
                  b'{"a": NaN}', b"{}" * 1000, b'{"session": [1]}', b'{"session": {}}', b'{"id": [1], "data": 5}',
                  b'{"session": "app-x", "turns": [1]}', b'{"do": [1], "path": {"a": 1}, "name": [2]}',
                  b'{"prompt": [1], "mode": [1], "effort": {}}', b'{"prompt": "x", "session": "app-g", "mode": [1]}',
                  b'{"prompt": "x", "session": "app-g", "effort": [1]}', b'{"lang": ["en"]}',
                  b'{"start": [1], "title": {"x": 1}}', b'{"path": "\\u0000"}', b'{"cols": "x", "id": "nope"}',
                  ('{"x": "' + "\u00e9" * 50_000 + '"}').encode(), b'{"a":' * 5000 + b"1" + b"}" * 5000]

        def one(args):
            route, raw = args
            status, headers, body = a.request("POST", route, raw=raw)
            self.assertJson(status, headers, body, f"{route} {raw[:60]!r}")
            return status
        with ThreadPoolExecutor(16) as pool:
            statuses = list(pool.map(one, [(r, s) for r in routes for s in shapes] * ROUNDS))
        self.assertTrue(all(s < 600 for s in statuses))
        status, data = a.api("GET", "/api/ping")
        self.assertEqual(data["running"], [])     # none of them started a turn

    def test_lying_slow_and_vanishing_clients(self):
        a = self.app
        c = a.cookie.encode()

        def post(path, length, body, *extra):
            return [f"POST {path} HTTP/1.0\r\nCookie: ".encode() + c + f"\r\nContent-Length: {length}\r\n".encode()
                    + b"".join(extra) + b"\r\n" + body]
        # Content-Length that is not a number, negative, huge, or longer than the body (the body ends at close)
        for length in ("abc", "-5", "1e3", " 2 ", "99999999999999999999", str(10**15), "0", ""):
            out = a.raw(post("/api/stop", length, b"{}"), timeout=10)
            self.assertTrue(out.startswith(b"HTTP/1.0 "), f"Content-Length {length!r}: {out[:200]!r}")
            self.assertIn(b"json", out.split(b"\r\n\r\n")[0].lower(), length)
        for length in ("abc", "-1", str(10**15), "1e3"):
            out = a.raw(post("/api/upload", length, b"xx"), timeout=10)
            self.assertTrue(out.startswith(b"HTTP/1.0 "), f"upload Content-Length {length!r}: {out[:200]!r}")
        # shorter than said, then the client half-closes: a bad body, not a hang
        s = socket.create_connection(("127.0.0.1", a.port), timeout=10)
        s.sendall(post("/api/stop", 100, b'{"session"')[0])
        s.shutdown(socket.SHUT_WR)
        out = s.recv(65536)
        s.close()
        self.assertTrue(out.startswith(b"HTTP/1.0 400"), out[:200])
        # longer than said: the extra bytes are ignored
        out = a.raw(post("/api/stop", 2, b'{}{"garbage": 1}'))
        self.assertTrue(out.startswith(b"HTTP/1.0 200"), out[:200])
        # a slow client: the request a few bytes at a time
        req = b"GET /api/ping HTTP/1.0\r\nCookie: " + c + b"\r\n\r\n"
        out = a.raw([x for i in range(0, len(req), 7) for x in (req[i:i + 7], 0.01)], timeout=20)
        self.assertTrue(out.startswith(b"HTTP/1.0 200"), out[:200])
        # vanishing clients: nothing, half a request line, headers without the body, the body cut, RST mid-stream
        for parts in ([b""], [b"GET /api/pi"], [b"GET /api/ping HTTP/1.0\r\nCook"],
                      post("/api/stop", 50, b'{"ses'), post("/api/upload", 1000, b"abc")):
            a.raw(parts, close_after=0.05)
        for _ in range(10 * ROUNDS):
            s = socket.create_connection(("127.0.0.1", a.port), timeout=5)
            s.sendall(b"GET /app.js HTTP/1.0\r\nCookie: " + c + b"\r\n\r\n")
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
            s.close()                         # reset, not a clean close, while the server writes
        # garbage that is not HTTP, wrong methods, a request line far too long
        for raw in (b"GARBAGE\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n",
                    b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n", b"GET / HTTP/1.0\r\n" + b"X: y\r\n" * 200 + b"\r\n"):
            out = a.raw(raw, timeout=10)       # an error page (HTTP/0.9 style for one word: no status line)
            self.assertIn(b"Error", out, f"{raw[:40]!r}: {out[:100]!r}")
            self.assertNotIn(b'"id"', out)
        # a TLS hello (no line end ever comes): the client gives up, the server goes on
        a.raw(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + bytes(range(1, 64)), close_after=0.3)
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", "FOO"):
            status, headers, body = a.request(method, "/api/sessions")
            self.assertNotEqual(status, 200, method)
            if method != "HEAD":
                self.assertNotIn(b'"id"', body, method)
        # many idle connections that never send anything do not starve the others
        idle = [socket.create_connection(("127.0.0.1", a.port), timeout=5) for _ in range(50)]
        try:
            t = time.monotonic()
            self.assertEqual(a.api("GET", "/api/ping")[0], 200)
            self.assertLess(time.monotonic() - t, 5)
        finally:
            for s in idle:
                s.close()
        self.assertEqual(a.api("GET", "/api/ping")[0], 200)

    def test_model_start_jobs_over_http(self):
        a = self.app

        def job():
            status, data = a.api("GET", "/api/launch?job=1")
            self.assertEqual(status, 200)
            j = data["job"]
            if j:
                self.assertTrue({"id", "do", "kind", "model", "lines", "result", "secs", "age"} <= set(j), j)
                for line in j["lines"]:
                    self.assertTrue(line.startswith(str(j["model"]) + " "), f"a line of another job: {j}")
                if j["result"] and j["result"].get("text"):
                    self.assertTrue(j["result"]["text"].startswith(str(j["model"]) + " "), j)
            return j

        def wait_done(jid, secs=15):
            end = time.monotonic() + secs
            while time.monotonic() < end:
                j = job()
                if j["id"] != jid or j["result"] is not None:
                    return j
                time.sleep(0.1)
            self.fail(f"job {jid} did not end")
        # a start that finishes
        status, data = a.api("POST", "/api/launch", {"do": "start", "kind": "strata", "model": "ok1", "ctx": 6})
        first = data["job"]
        self.assertEqual((first["model"], first["result"]), ("ok1", None))
        done = wait_done(first["id"])
        self.assertEqual((done["id"], done["result"]), (first["id"], {"done": True, "ok": True, "text": "ok1 ready"}))
        # a start replaced by the next one: the old one is stopped, the new one shows only its own lines
        old = a.api("POST", "/api/launch", {"do": "start", "model": "slowA", "ctx": 400})[1]["job"]
        time.sleep(0.4)
        new = a.api("POST", "/api/launch", {"do": "start", "model": "fastB", "ctx": 8})[1]["job"]
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["model"], "fastB")
        done = wait_done(new["id"])
        self.assertEqual(done["result"]["text"], "fastB ready")
        self.assertEqual(a.stub_alive(), [])
        # a cancelled job stays cancelled
        c = a.api("POST", "/api/launch", {"do": "start", "model": "slowC", "ctx": 400})[1]["job"]
        time.sleep(0.3)
        out = a.api("POST", "/api/launch", {"do": "cancel"})[1]["job"]
        self.assertEqual((out["id"], out["result"]), (c["id"], {"ok": False, "error": "cancelled"}))
        time.sleep(0.6)
        later = job()
        self.assertEqual((later["id"], later["result"]), (c["id"], {"ok": False, "error": "cancelled"}))
        self.assertEqual(a.stub_alive(), [])
        # racing pages: starts, stops and cancels from many threads, ?job=1 polled all along
        stop = threading.Event()
        polls = []

        def poller():
            while not stop.is_set():
                polls.append(job())

        def racer(n):
            rnd = random.Random(n)
            for i in range(4 * ROUNDS):
                do = rnd.choice(["start", "start", "stop", "cancel"])
                status, data = a.api("POST", "/api/launch", {"do": do, "model": f"r{n}x{i}", "ctx": rnd.choice([3, 200])})
                self.assertEqual(status, 200)
                self.assertIn("job", data)
                if do != "cancel":
                    self.assertTrue(data["job"]["model"].startswith("r"), data)
                self.assertLessEqual(len(a.stub_alive()), 2)   # the new one, and at most one on its way out
        pollers = [threading.Thread(target=poller) for _ in range(3)]
        for t in pollers:
            t.start()
        try:
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(racer, range(4)))
        finally:
            stop.set()
            for t in pollers:
                t.join()
        self.assertTrue(polls)
        a.api("POST", "/api/launch", {"do": "cancel"})
        end = time.monotonic() + 5
        while a.stub_alive() and time.monotonic() < end:
            time.sleep(0.1)
        self.assertEqual(a.stub_alive(), [])
        self.assertIsNotNone(job()["result"])

    # ── turns with the fake model ──
    def start_turn(self, prompt, sid=None, cwd=None):
        """POST /api/turn -> (connection, response, first line); the turn's lines go on on the response."""
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=60)
        body = {"prompt": prompt, "cwd": str(cwd or self.work), "mode": "plan"}
        if sid:
            body["session"] = sid
        conn.request("POST", "/api/turn", body=json.dumps(body), headers={"Cookie": self.app.cookie})
        r = conn.getresponse()
        if r.status != 200:
            self.fail(f"turn: {r.status} {r.read()[:300]!r}")
        first = r.readline()
        return conn, r, first

    def watch(self, sid, start=0, limit=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=60)
        try:
            conn.request("GET", f"/api/watch?session={sid}&from={start}", headers={"Cookie": self.app.cookie})
            r = conn.getresponse()
            if r.status != 200:
                return r.status, []
            out = []
            while limit is None or len(out) < limit:
                line = r.readline()
                if not line:
                    break
                out.append(line)
            return 200, out
        finally:
            conn.close()

    def test_turns_watched_reloaded_and_stopped(self):
        a = self.app
        for rnd in range(ROUNDS):
            sid = f"app-stress-{self.uid}-{rnd}"
            conn, r, first = self.start_turn("say something", sid)
            self.assertEqual(json.loads(first)["type"], "session")
            with ThreadPoolExecutor(8) as pool:
                watchers = [pool.submit(self.watch, sid) for _ in range(5)]
                # a page reloads: three lines, gone, the saved chat with running=True, then the rest from there
                status, head = self.watch(sid, 0, limit=3)
                status, chat = a.api("GET", f"/api/session?id={sid}")
                self.assertEqual((status, chat.get("running"), chat.get("prompt")), (200, True, "say something"))
                self.assertIn(sid, a.api("GET", "/api/ping")[1]["running"])
                busy = a.request("POST", "/api/turn", {"prompt": "again", "session": sid, "cwd": str(self.work)})
                self.assertEqual(busy[0], 409)
                self.assertEqual(a.request("POST", "/api/btw", {"session": sid, "question": "q"})[0], 409)
                self.assertEqual(a.request("POST", "/api/delete", {"session": sid})[0], 400)
                status, rest = self.watch(sid, len(head))
                own = [first] + [x for x in iter(r.readline, b"")]
                conn.close()
                seen = [f.result()[1] for f in watchers]
            kinds = [json.loads(x)["type"] for x in own]
            self.assertEqual(kinds[-1], "exit", kinds)
            self.assertEqual(json.loads(own[-1])["code"], 0, own[-1])
            self.assertIn("result", kinds)
            self.assertIn("delta", kinds)
            for lines in seen:
                self.assertEqual(lines, own)
            if status == 200:
                self.assertEqual(head + rest, own)
            # the turn ended: no longer running, saved, listed, its times kept
            self.assertEqual(self.watch(sid)[0], 404)
            self.assertNotIn(sid, a.api("GET", "/api/ping")[1]["running"])
            status, chat = a.api("GET", f"/api/session?id={sid}&cwd={self.work}")
            self.assertEqual(status, 200, chat)
            self.assertEqual([i["role"] for i in chat["items"]], ["user", "assistant"])
            self.assertTrue(chat["times"]["turns"])
            listed = {s["id"]: s for s in a.api("GET", "/api/sessions")[1]}
            self.assertFalse(listed[sid]["running"])
        # a long turn stopped from another page: every watcher ends, the turn is gone
        sid = f"app-stop-{self.uid}"
        conn, r, first = self.start_turn("SLOW answer please", sid)
        with ThreadPoolExecutor(6) as pool:
            watchers = [pool.submit(self.watch, sid) for _ in range(5)]
            own = [first]
            while True:
                line = r.readline()
                self.assertTrue(line, "the turn ended before any delta")
                own.append(line)
                if json.loads(line)["type"] == "delta":
                    break
            t = time.monotonic()
            self.assertEqual(a.api("POST", "/api/stop", {"session": sid}), (200, {"stopped": True}))
            own += [x for x in iter(r.readline, b"")]
            conn.close()
            seen = [f.result()[1] for f in watchers]
        self.assertLess(time.monotonic() - t, 20)
        self.assertEqual(json.loads(own[-1])["type"], "exit")
        for lines in seen:
            self.assertEqual(lines, own)
        self.assertEqual(a.api("POST", "/api/stop", {"session": sid}), (200, {"stopped": False}))
        self.assertNotIn(sid, a.api("GET", "/api/ping")[1]["running"])
        # turns of several chats at once, each with its own watchers
        sids = [f"app-many-{self.uid}-{i}" for i in range(3 * ROUNDS)]

        def whole(sid):
            conn, r, first = self.start_turn(f"hello from {sid}", sid)
            with ThreadPoolExecutor(2) as pool:
                ws = [pool.submit(self.watch, sid) for _ in range(2)]
                own = [first] + [x for x in iter(r.readline, b"")]
                conn.close()
                return sid, own, [w.result()[1] for w in ws]
        with ThreadPoolExecutor(len(sids)) as pool:
            for sid, own, seen in pool.map(whole, sids):
                self.assertEqual(json.loads(own[0])["id"], sid)
                self.assertEqual(json.loads(own[-1]), {**json.loads(own[-1]), "type": "exit", "code": 0})
                for lines in seen:
                    self.assertIn(lines, ([], own))         # a watcher that came after the end gets 404
        self.assertEqual(a.api("GET", "/api/ping")[1]["running"], [])

    def test_terminals(self):
        a = self.app
        status, data = a.api("POST", "/api/pty-open", {"cwd": str(self.work), "cols": 80, "rows": 24})
        self.assertEqual(status, 200, data)
        tid = data["id"]
        self.assertIn(tid, [t["id"] for t in a.api("GET", "/api/pty-list")[1]])
        for bad in ({"id": tid, "cols": "x"}, {"id": tid, "cols": -5, "rows": 0}, {"id": tid, "cols": 10**9}):
            status, headers, body = a.request("POST", "/api/pty-resize", bad)
            self.assertJson(status, headers, body, f"pty-resize {bad}")
        self.assertEqual(a.api("POST", "/api/pty-write", {"id": tid, "data": "echo mx-ok\r"})[0], 200)
        status, headers, body = a.request("GET", f"/api/pty-read?id={tid}&from=abc")
        self.assertJson(status, headers, body, "pty-read from=abc")
        conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=30)
        conn.request("GET", f"/api/pty-read?id={tid}&from=0", headers={"Cookie": a.cookie})
        r = conn.getresponse()
        text, end = "", time.monotonic() + 20
        while "mx-ok" not in text and time.monotonic() < end:
            line = r.readline()
            if not line:
                break
            text += json.loads(line).get("data") or ""
        conn.close()
        self.assertIn("mx-ok", text)
        self.assertEqual(a.api("POST", "/api/pty-close", {"id": tid}), (200, {"ok": True}))
        self.assertEqual(a.api("POST", "/api/pty-write", {"id": tid, "data": "x"})[0], 404)
        self.assertEqual(a.api("GET", f"/api/pty-read?id={tid}")[0], 404)


if __name__ == "__main__":
    unittest.main()
