"""A model start is "done" only when the model really is in memory, and never sooner (maxedcode/launch.py).

Each program is played by a small HTTP server on this PC that answers the way the real one does while it loads
(LM Studio's model list, Strata's /health, llama-server's 503, Ollama's /api/ps) and turns "loaded" after a set
number of questions. The start commands are faked and the clock is a counter: no model is loaded, nothing waits
for real minutes, and the result is the same on every PC."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-loading-tests-")
os.environ.setdefault("AGENT_HOME", _HOME.name)

from maxedcode import launch, servers  # noqa: E402

launch.home = lambda: Path(_HOME.name)
APP_JS = Path(__file__).resolve().parents[1] / "maxedcode" / "webapp" / "static" / "app.js"


class FakeServer:
    """A model server on 127.0.0.1:<free port>. answer(path, body, n) -> (code, json); n counts the requests
    of that path so far (a server "loads" over a few questions)."""

    def __init__(self, answer):
        self.answer, self.counts, self.httpd = answer, {}, None
        self.lock = threading.Lock()

    def start(self, port=0):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, body=None):
                with fake.lock:
                    n = fake.counts[self.path] = fake.counts.get(self.path, 0) + 1
                code, obj = fake.answer(self.path, body, n)
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply()

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                self.reply(json.loads(self.rfile.read(size) or b"{}"))
        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass                     # a client that gave up waiting (a timeout under test) is no error
        self.httpd = Server(("127.0.0.1", port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def port(self):
        return self.httpd.server_address[1]

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Clock:
    """launch.time: monotonic() moves only when the code sleeps, by at least 5 s (the loops' own budgets of
    minutes run out in a few hundred quick steps)."""
    def __init__(self):
        self.t, self.slept = 1000.0, 0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept += 1
        self.t += max(s, 5)
        time.sleep(0.002)


class Proc:
    """A started command: running until `ends_after` polls (None: forever), then exit `code`."""
    def __init__(self, code=0, ends_after=None):
        self.code, self.ends_after, self.polls, self.pid = code, ends_after, 0, 4242

    def poll(self):
        self.polls += 1
        return self.code if self.ends_after is not None and self.polls > self.ends_after else None


class Base(unittest.TestCase):
    kind = ""

    def setUp(self):
        launch.save_state({})
        self.clock = Clock()
        self.said = []
        self.fakes = []
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}/v1"
        for p in (patch.dict(os.environ, {"MAXEDCODE_OFFLINE": ""}), patch.object(launch, "time", self.clock),
                  patch.object(servers, "default_base", lambda kind: self.base),
                  patch.object(launch, "port_of", lambda kind: self.port),
                  # nothing listens yet: no answer at once (Windows takes ~2 s to refuse a closed port)
                  patch.object(servers, "get", lambda url, key="", timeout=3, _get=servers.get:
                               _get(url, key, timeout) if self.fakes else None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: [f.stop() for f in self.fakes])

    def say(self, text, pct=None):
        self.said.append(text)

    def serve(self, answer):
        """The program's server comes up on the port (from a faked start command, or already running)."""
        fake = FakeServer(answer).start(self.port)
        self.fakes.append(fake)
        return fake


# ---------- LM Studio: `lms load` and the model list ----------

def lms_answer(state):
    """/api/v0/models with state["loaded"] = [(id, ctx)], state["loading"] = id listed as not loaded yet."""
    def answer(path, body, n):
        rows = [{"id": m, "type": "llm", "state": "loaded", "loaded_context_length": c} for m, c in state["loaded"]]
        if state.get("loading"):
            rows.append({"id": state["loading"], "type": "llm", "state": "not-loaded"})
        if path.startswith("/api/v0/models"):
            return 200, {"data": rows}
        if path.startswith("/v1/models"):
            return 200, {"data": [{"id": r["id"]} for r in rows]}
        return 404, {}
    return answer


class LmStudioLoad(Base):
    def setUp(self):
        super().setUp()
        self.state = {"loaded": [], "loading": None}
        self.serve(lms_answer(self.state))
        self.cmds = []
        self.log = launch.log_file("lmstudio")
        for p in (patch.object(launch, "lms_cli", lambda: "lms"),
                  patch.object(launch, "run", lambda c, *a, **k: self.cmds.append(c) or (0, "--parallel")),
                  patch.object(launch, "LMS_LAG", 10)):
            p.start()
            self.addCleanup(p.stop)

    def loads(self, proc, on_sleep):
        """`lms load` is spawned as proc; on_sleep(n) changes the server / the log at the n-th second."""
        def spawn(cmd, log=None, **kw):
            self.cmds.append(cmd)
            self.state["loading"] = cmd[2]
            return proc
        real = self.clock.sleep

        def sleep(s):
            real(s)
            on_sleep(self.clock.slept)
        self.clock.sleep = sleep
        return patch.object(launch, "spawn", spawn)

    def test_done_as_soon_as_the_model_is_in_memory_while_lms_load_stays_open(self):
        """The bug: `lms load` printed "Model loaded successfully" and stayed open; the window said
        "loading" for 15 minutes over a model that was ready."""
        proc = Proc(ends_after=None)

        def step(n):
            if n == 3:
                self.log.write_text("Loading gemma 100%\nModel loaded successfully in 5.61s.\n", encoding="utf-8")
                self.state["loaded"], self.state["loading"] = [("gemma", 32768)], None
        with self.loads(proc, step):
            r = launch.lms_start("gemma", 32768, self.say)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["model"], "gemma")
        self.assertLess(self.clock.slept, 6)          # seconds, not the 900 s budget

    def test_not_done_while_it_still_loads_even_when_the_list_says_loaded(self):
        """A model listed as loaded while `lms load` still counts its percent is not ready: done only at the end."""
        proc = Proc(ends_after=6)

        def step(n):
            self.state["loaded"] = [("gemma", 32768)]          # listed at once
            self.log.write_text(f"Loading gemma {min(90, n * 15)}%", encoding="utf-8")
        with self.loads(proc, step):
            r = launch.lms_start("gemma", 32768, self.say)
        self.assertTrue(r["ok"], r)
        self.assertGreater(proc.polls, 6)            # it waited for the load to end
        self.assertTrue(any(t.startswith("loading gemma") for t in self.said))

    def test_the_list_may_lag_behind_a_load_that_ended(self):
        proc = Proc(ends_after=1)

        def step(n):
            if n == 2:          # one second after lms load ended
                self.state["loaded"], self.state["loading"] = [("gemma", 32768)], None
        with self.loads(proc, step):
            r = launch.lms_start("gemma", 32768, self.say)
        self.assertTrue(r["ok"], r)

    def test_a_failed_load_is_said_at_once_with_its_words(self):
        proc = Proc(code=1, ends_after=1)

        def step(n):
            self.log.write_text("Error: not enough memory to load the model", encoding="utf-8")
        with self.loads(proc, step):
            r = launch.lms_start("gemma", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("not enough memory", r["error"])
        self.assertLess(self.clock.slept, 4)

    def test_nothing_appears_after_the_load_ended_is_a_failure_not_a_hang(self):
        with self.loads(Proc(ends_after=1), lambda n: None):
            r = launch.lms_start("gemma", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertLess(self.clock.slept, 20)

    def test_the_users_own_copy_does_not_count_as_the_new_load(self):
        """The same model loaded by hand with another context: a reload from here (force) is done only when
        `lms load` is over, not at once because the user's copy is listed."""
        self.state["loaded"] = [("gemma", 4096)]
        proc = Proc(ends_after=5)

        def step(n):
            self.log.write_text("Model loaded successfully", encoding="utf-8")   # an old line: the user's copy
            if n == 5:
                self.state["loaded"] = [("gemma", 4096), ("gemma:2", 32768)]
        with self.loads(proc, step):
            r = launch.lms_start("gemma", 32768, self.say, force=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["model"], "gemma:2")


# ---------- Strata: /health "loaded" ----------

def strata_answer(loaded_after, model="iq3_xxs"):
    def answer(path, body, n):
        if path == "/health":
            return 200, {"status": "ok", "max_context": 32768, "model": model, "loaded": n > loaded_after,
                         "service": "strata"}
        if path.startswith("/v1/models"):
            return 200, {"data": [{"id": model}]}
        return 404, {}
    return answer


class StrataLoad(Base):
    def setUp(self):
        super().setUp()
        for p in (patch.object(launch, "strata_cli", lambda: (["strata"], None)),
                  patch.object(launch, "remember", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def spawn_server(self, answer, proc=None):
        started = []

        def spawn(cmd, **kw):
            started.append(cmd)
            if answer:
                self.serve(answer)
            return proc or Proc()
        return started, patch.object(launch, "spawn", spawn)

    def test_done_only_when_the_engine_is_in_memory(self):
        started, sp = self.spawn_server(strata_answer(loaded_after=4))
        with sp:
            r = launch.strata_start("iq3_xxs", 32768, self.say)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.fakes[0].counts["/health"], 5)     # four "not yet" answers were waited out
        self.assertTrue(any("loading iq3_xxs into memory" in t for t in self.said))

    def test_an_old_strata_that_does_not_close_is_not_taken_for_the_new_one(self):
        """The bug: the previous Strata (started from here) still answered "loaded", and the new start was done
        at once - with the old model."""
        self.serve(strata_answer(loaded_after=0, model="old"))
        started, sp = self.spawn_server(None)
        with sp, patch.object(launch, "mine", lambda *a, **k: {"kind": "strata", "base": self.base}), \
                patch.object(launch, "release", lambda o, say: True), \
                patch.object(launch, "wait_down", lambda base, s: servers.port_down(base)):
            r = launch.strata_start("iq3_xxs", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("still answers", r["error"])
        self.assertEqual(started, [])

    def test_a_strata_started_by_hand_is_left_alone(self):
        self.serve(strata_answer(loaded_after=0))
        started, sp = self.spawn_server(None)
        with sp, patch.object(launch, "mine", lambda *a, **k: None):
            r = launch.strata_start("iq3_xxs", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("outside maxedcode", r["error"])
        self.assertEqual(started, [])

    def test_a_launch_that_ended_with_nothing_running_fails_soon(self):
        """`strata launch` exited 0 and --status shows nothing: no 15-minute wait."""
        started, sp = self.spawn_server(None, Proc(code=0, ends_after=0))
        with sp, patch.object(launch, "strata_starting", lambda cli, src: False):
            r = launch.strata_start("iq3_xxs", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("did not start", r["error"])
        self.assertLess(self.clock.slept, 20)

    def test_the_hand_over_exit_keeps_waiting_while_status_says_starting(self):
        answers = iter([True, True, True])
        proc = Proc(code=0, ends_after=0)
        started, sp = self.spawn_server(None, proc)

        def starting(cli, src):
            ok = next(answers, None)
            if ok is None:                   # the runtime's server comes up after three looks
                if not self.fakes:
                    self.serve(strata_answer(loaded_after=0))
                return True
            return ok
        with sp, patch.object(launch, "strata_starting", starting):
            r = launch.strata_start("iq3_xxs", 32768, self.say)
        self.assertTrue(r["ok"], r)


# ---------- llama.cpp: 503 while the model loads ----------

def llama_answer(ready_after):
    def answer(path, body, n):
        if path == "/health":
            return (200, {"status": "ok"}) if n > ready_after else (503, {"error": {"code": 503, "message": "Loading model"}})
        return (200, {"data": [{"id": "m"}]}) if path.startswith("/v1/models") else (404, {})
    return answer


class LlamaLoad(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.model = Path(self.dir.name, "m.gguf")
        self.model.write_bytes(b"GGUF")
        self.started = []
        for p in (patch.object(launch, "llama_cli", lambda: "llama-server"),
                  patch.object(launch, "run", lambda c, *a, **k: (0, "--fit")),
                  patch.object(launch, "remember", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def spawns(self, answer, proc=None):
        def spawn(cmd, **kw):
            self.started.append(cmd)
            if answer:
                self.serve(answer)
            return proc or Proc()
        return patch.object(launch, "spawn", spawn)

    def test_done_only_when_health_says_ok(self):
        with self.spawns(llama_answer(ready_after=3)):
            r = launch.llama_start(str(self.model), 32768, self.say)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.fakes[0].counts["/health"], 4)

    def test_a_server_that_ended_before_it_was_ready_fails_at_once(self):
        with self.spawns(None, Proc(code=0, ends_after=0)):
            r = launch.llama_start(str(self.model), 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertLess(self.clock.slept, 3)

    def test_an_old_llama_server_that_does_not_close_is_not_taken_for_the_new_one(self):
        self.serve(llama_answer(ready_after=0))
        with self.spawns(None), patch.object(servers, "kind_of", lambda *a, **k: "llamacpp"), \
                patch.object(launch, "mine", lambda *a, **k: {"kind": "llamacpp", "base": self.base}), \
                patch.object(launch, "release", lambda o, say: True), \
                patch.object(launch, "wait_down", lambda base, s: servers.port_down(base)):
            r = launch.llama_start(str(self.model), 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("still answers", r["error"])
        self.assertEqual(self.started, [])


# ---------- a start script (any program) ----------

class ScriptLoad(Base):
    def test_an_old_server_on_the_port_that_does_not_close_fails_the_script_start(self):
        self.serve(strata_answer(loaded_after=0))
        ran = []
        with patch.object(launch, "console", lambda command, **k: ran.append(command)), \
                patch.object(servers, "kind_of", lambda *a, **k: "strata"), \
                patch.object(launch, "mine", lambda *a, **k: {"kind": "strata", "base": self.base}), \
                patch.object(launch, "release", lambda o, say: True), \
                patch.object(launch, "wait_down", lambda base, s: servers.port_down(base)):
            r = launch.script_start("strata", {"command": "run-iq3.bat", "base": self.base, "name": "iq3"}, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("still answers", r["error"])
        self.assertEqual(ran, [])

    def test_a_strata_script_is_done_when_the_engine_is_in_memory(self):
        ran = []

        def console(command, **k):
            ran.append(command)
            self.serve(strata_answer(loaded_after=3))
        with patch.object(launch, "console", console), patch.object(launch, "remember", lambda *a, **k: None):
            r = launch.script_start("strata", {"command": "run-iq3.bat", "base": self.base, "name": "iq3"}, self.say)
        self.assertTrue(r["ok"], r)
        self.assertEqual(ran, ["run-iq3.bat"])
        self.assertGreaterEqual(self.fakes[0].counts["/health"], 4)


# ---------- Ollama ----------

class OllamaLoad(Base):
    def test_a_load_that_outlasts_the_answer_counts_once_ollama_has_it(self):
        """On a slow disk /api/generate timed out while Ollama went on loading: "did not load" over a model
        that was there a moment later."""
        def answer(path, body, n):
            if path == "/api/generate":
                time.sleep(0.6)                      # longer than the request waits
                return 200, {"done": True}
            if path == "/api/ps":
                return 200, {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "context_length": 32768}]}
            if path == "/api/version":
                return 200, {"version": "0.9"}
            return 404, {}
        self.serve(answer)
        with patch.dict(launch.WAIT, {"ollama": 0.2}), patch.object(launch, "ollama_cli", lambda: "ollama"), \
                patch.object(launch, "mine", lambda *a, **k: None), patch.object(launch, "remember", lambda *a, **k: None):
            r = launch.ollama_start("qwen3:8b", 32768, self.say)
        self.assertTrue(r["ok"], r)

    def test_a_real_error_stays_an_error(self):
        def answer(path, body, n):
            if path == "/api/generate":
                return 404, {"error": "model 'nope' not found"}
            if path == "/api/ps":
                return 200, {"models": []}
            return (200, {"version": "0.9"}) if path == "/api/version" else (404, {})
        self.serve(answer)
        with patch.object(launch, "ollama_cli", lambda: "ollama"), patch.object(launch, "mine", lambda *a, **k: None):
            r = launch.ollama_start("nope", 32768, self.say)
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])


# ---------- after a start: the agent's connection is that server ----------

class ConnectionAfterStart(unittest.TestCase):
    """A model started on the Start models tab is the agent's server at once: Connection shows its address and
    that it was started there, with nothing to set by hand."""
    def setUp(self):
        from maxedcode import appsettings
        self.s = appsettings
        launch.save_state({})
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.user, self.cfg, self.state = root / "settings.json", root / "config.json", root / "state.json"
        for p in (patch.object(appsettings, "USER_SETTINGS", self.user), patch.object(appsettings, "CONFIG_FILE", self.cfg),
                  patch.object(appsettings, "MEMORY_FILE", root / "MEMORY.md"),
                  patch.object(appsettings, "RECENT_FILE", root / "recent.json"),
                  patch.object(appsettings.config, "STATE_FILE", self.state),
                  patch.object(appsettings, "server_models", return_value=["gemma"]),
                  patch.object(launch, "listener", lambda port: 111),
                  patch.dict(os.environ, {"MAXEDCODE_OFFLINE": ""})):
            p.start()
            self.addCleanup(p.stop)

    def start(self, kind, base, model):
        with patch.dict(launch.START, {kind: lambda *a, **k: {**launch._done(kind, base, model), "serve": True}}), \
                patch.object(launch, "mine", lambda *a, **k: None), \
                patch.object(servers, "describe", lambda *a, **k: {"kind": kind, "ctx": 32768, "model": model}):
            return launch.start(kind, model, 32768)

    def test_the_started_server_and_its_model_are_saved_for_the_agent(self):
        r = self.start("lmstudio", "http://127.0.0.1:1234/v1", "google/gemma-4-12b")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["saved"])
        cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual((cfg["base_url"], cfg["server"], cfg["model"]),
                         ("http://127.0.0.1:1234/v1", "lmstudio", "google/gemma-4-12b"))

    def test_strata_and_llama_cpp_serve_one_model_so_none_is_pinned(self):
        self.cfg.write_text('{"base_url": "http://127.0.0.1:1234/v1", "model": "old-lmstudio-model"}', encoding="utf-8")
        r = self.start("strata", "http://127.0.0.1:8080/v1", None)
        self.assertTrue(r["ok"], r)
        cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:8080/v1")
        self.assertNotIn("model", cfg)              # a model id of the previous server would be sent to Strata

    def test_connection_says_the_server_was_started_here(self):
        self.start("lmstudio", "http://127.0.0.1:1234/v1", "gemma")
        self.assertEqual(self.s.started_here("http://127.0.0.1:1234/v1")["label"], "LM Studio")
        self.assertEqual(self.s.started_here("http://127.0.0.1:1234")["kind"], "lmstudio")   # however it is spelled
        self.assertIsNone(self.s.started_here("http://192.168.0.5:1234/v1"))
        launch.forget_ours()
        self.assertIsNone(self.s.started_here("http://127.0.0.1:1234/v1"))

    def test_the_settings_answer_carries_it(self):
        self.start("lmstudio", "http://127.0.0.1:1234/v1", "gemma")
        with patch.object(self.s.config, "BASE_URL", "http://127.0.0.1:1234/v1"), \
                patch.object(self.s.config, "resolve_server", return_value={"ctx": 32768, "model": "gemma", "kind": "lmstudio"}):
            data = self.s.get()
        self.assertEqual(data["server"]["started_here"]["kind"], "lmstudio")
        self.assertEqual(data["effective"]["base_url"], "http://127.0.0.1:1234/v1")

    def test_the_window_shows_the_address_and_where_it_was_started(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("t('addrFrom') + ' ' + shortAddr(eff.base_url)", js)
        self.assertEqual(js.count("t('addrFrom')"), 1)       # said once: in the card, not again under the field
        self.assertIn("server.started_here", js)
        self.assertIn("j.result.saved === false", js)


# ---------- what the window shows ----------

class Shown(unittest.TestCase):
    def catalog(self, health):
        base = "http://127.0.0.1:8080/v1"
        models = {"strata": lambda: [{"id": "iq3_xxs", "name": "IQ3", "ctx": 32768, "max_ctx": None}],
                  "lmstudio": lambda: [], "ollama": lambda: [], "llamacpp": lambda: []}
        with patch.object(servers, "installed", lambda: []), patch.object(launch, "status",
                                                                         lambda: {"strata": {"base": base, "model": "iq3_xxs", "ctx": 32768, "online": True}}), \
                patch.dict(launch.MODELS, models), patch.dict(launch.CLI, {k: (lambda: None) for k in launch.KINDS}), \
                patch.object(launch, "strata_presets", lambda: []), patch.object(launch, "strata_scripts", lambda: []), \
                patch.object(launch, "strata_health", lambda b: health), patch.object(launch, "mine", lambda *a, **k: None), \
                patch.object(launch, "gguf_dirs", lambda: []):
            entry = next(e for e in launch.catalog()["servers"] if e["kind"] == "strata")
        return entry

    def test_a_strata_whose_engine_is_not_in_memory_is_not_shown_as_loaded(self):
        e = self.catalog({"loaded": False, "model": "iq3_xxs"})
        self.assertTrue(e["loaded"]["asleep"])
        self.assertTrue(e["models"][0]["asleep"])
        e = self.catalog({"loaded": True, "model": "iq3_xxs"})
        self.assertFalse(e["loaded"]["asleep"])
        self.assertTrue(e["models"][0]["loaded"])
        self.assertFalse(e["models"][0]["asleep"])

    def test_the_window_says_asleep_instead_of_loaded(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("runAsleep:", js)
        self.assertIn("m.asleep ? 'runAsleep' : 'runLoaded'", js)
        self.assertIn("lo.asleep ?", js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_window_script_parses(self):
        r = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
