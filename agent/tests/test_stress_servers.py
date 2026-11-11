"""Stress: model servers that answer anything, addresses typed in any form, launch.json written from many places
at once, GGUF headers of random bytes, settings with random values (servers.py, launch.py, appsettings.py).

Every look at a server must end in its time with a plain answer (no traceback), whatever the server sends:
broken JSON, wrong types, errors, a closed connection, a stall. Seeded: a failure prints its seed and repeats.
MAXEDCODE_STRESS=<n> runs n times the rounds (a long manual run); the default stays a few seconds."""
import json
import os
import random
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-stress-servers-")
os.environ.setdefault("AGENT_HOME", _HOME.name)

from maxedcode import launch, servers  # noqa: E402

launch.home = lambda: Path(_HOME.name)
ROUNDS = max(1, int(os.environ.get("MAXEDCODE_STRESS") or 1))
SEED = int(os.environ.get("MAXEDCODE_STRESS_SEED") or time.time())
KINDS = ["", "strata", "llamacpp", "lmstudio", "ollama", "vllm", "koboldcpp", "jan", "tgwui"]


def junk(rnd, depth=0):
    """A random JSON value: what a broken or foreign server may send where a dict was expected."""
    pick = rnd.randrange(10 if depth < 3 else 6)
    if pick == 0:
        return None
    if pick == 1:
        return rnd.choice([0, -1, 1, 2 ** 63, 4096, 32768, 1.5, float("1e308")])
    if pick == 2:
        return rnd.choice(["", "loaded", "not-loaded", "loading", "ok", "\u0000", "Ж" * 50, "x" * 5000, "8080"])
    if pick == 3:
        return rnd.choice([True, False])
    if pick in (4, 5):
        return [junk(rnd, depth + 1) for _ in range(rnd.randrange(4))]
    keys = ["id", "name", "model", "state", "type", "data", "models", "loaded", "max_context", "status",
            "loaded_context_length", "context_length", "n_ctx", "default_generation_settings", "version",
            "build_info", "value", "max_model_len", "parameters", "capabilities", "model_info"]
    return {rnd.choice(keys): junk(rnd, depth + 1) for _ in range(rnd.randrange(6))}


class Chaos:
    """A server on a free port whose every answer is drawn from `rnd`: valid-looking, junk, an error code, a
    closed connection, a stall past the client's timeout, a body much shorter than its Content-Length."""

    def __init__(self, seed):
        self.rnd = random.Random(seed)
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                with fake.lock:
                    mode = fake.rnd.randrange(12)
                    body = json.dumps(junk(fake.rnd), allow_nan=False) if mode < 6 else ""
                    code = fake.rnd.choice([200, 200, 200, 400, 401, 404, 500, 503])
                    stall = fake.rnd.random() * 0.4
                if mode == 6:                       # closed without a word
                    self.close_connection = True
                    with_suppress(lambda: self.connection.shutdown(socket.SHUT_RDWR))
                    return
                if mode == 7:                       # a stall past a short timeout
                    time.sleep(stall)
                    body = "{}"
                if mode == 8:
                    body = "{\"data\": [{\"id\": \"m\", "      # cut JSON
                if mode == 9:
                    body = "<html>not an API</html>"
                if mode == 10:
                    data = b'{"data": []}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data) + 50))     # lies: the rest never comes
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if mode == 11:
                    body = json.dumps({"data": [{"id": "m" * 2000, "state": "loaded", "type": "llm",
                                                 "loaded_context_length": "32768"}] * 200})
                data = body.encode("utf-8", "surrogatepass")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with_suppress(self.handle_one)

            def do_POST(self):
                with_suppress(lambda: self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                with_suppress(self.handle_one)

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


def with_suppress(fn):
    try:
        fn()
    except Exception:
        pass


def online(test):
    """servers.http refuses while MAXEDCODE_OFFLINE=1 (set by the runner): these tests ask their own server."""
    p = patch.dict(os.environ, {"MAXEDCODE_OFFLINE": ""})
    p.start()
    test.addCleanup(p.stop)


class ChaosServers(unittest.TestCase):
    def setUp(self):
        online(self)
        self.chaos = Chaos(SEED)
        self.addCleanup(self.chaos.stop)

    def bounded(self, fn, seconds, what):
        start = time.monotonic()
        try:
            out = fn()
        except urllib.error.HTTPError as e:     # model_ids says "a key is needed" this way: allowed
            if e.code not in (401, 403):
                raise
            out = None
        took = time.monotonic() - start
        self.assertLess(took, seconds, f"{what} took {took:.1f} s (seed {SEED})")
        return out

    def test_every_look_at_a_server_ends_with_a_plain_answer(self):
        base = self.chaos.base
        for _ in range(12 * ROUNDS):
            kind = random.Random(SEED + _).choice(KINDS)
            with self.subTest(kind=kind, seed=SEED):
                d = self.bounded(lambda: servers.describe(base, kind=kind, timeout=0.3), 12, f"describe {kind}")
                self.assertEqual(set(d) >= {"kind", "ctx", "model", "online", "use", "loaded_ctx", "note"}, True)
                self.assertTrue(d["ctx"] is None or isinstance(d["ctx"], int), d)
                self.assertTrue(d["model"] is None or isinstance(d["model"], str), d)
                self.assertIsInstance(d["note"], str)
                self.assertIsInstance(self.bounded(lambda: servers.kind_of(base, timeout=0.3), 6, "kind_of"), str)
                on = self.bounded(lambda: servers.loaded_models(base, kind=kind or "lmstudio", timeout=0.3), 3, "loaded")
                self.assertTrue(on is None or all(isinstance(m[0], str) for m in on), on)
                ids = self.bounded(lambda: servers.model_ids(base, timeout=0.3), 3, "model_ids")
                self.assertTrue(ids is None or all(isinstance(i, str) for i in ids), ids)
                self.bounded(lambda: servers.loaded_model(base, kind=kind, timeout=0.3), 6, "loaded_model")
                self.bounded(lambda: servers.context(base, kind=kind, model="m", timeout=0.3), 6, "context")

    def test_strata_health_and_the_lm_studio_wait_survive_junk(self):
        base = self.chaos.base
        for _ in range(10 * ROUNDS):
            h = launch.strata_health(base)
            self.assertTrue(h is None or isinstance(h, dict))
            with self.subTest(seed=SEED):
                launch._ready("strata", base)
        log = Path(_HOME.name) / "lms-stress.log"
        log.write_text("Model loaded successfully", encoding="utf-8")

        class Done:
            def poll(self):
                return 0
        with patch.object(launch, "LMS_LAG", 0), patch.object(launch.time, "sleep", lambda s: None):
            for _ in range(10 * ROUNDS):
                hit = launch.lms_wait(base, "m", Done(), log, lambda *a, **k: None)
                self.assertTrue(hit is None or isinstance(hit[0], str))

    def test_a_port_that_is_not_a_model_server_is_not_listed(self):
        for _ in range(5 * ROUNDS):
            x = servers._probe_other(int(self.chaos.base.split(":")[2].split("/")[0]), "", 0.3)
            self.assertTrue(x is None or isinstance(x.get("models"), list))

    def test_pick_model_with_whatever_the_server_listed(self):
        rnd = random.Random(SEED)
        for _ in range(500 * ROUNDS):
            on = None if rnd.random() < 0.2 else [(str(junk(rnd)), rnd.choice([None, 0, 4096, 32768, 10 ** 9]))
                                                  for _ in range(rnd.randrange(4))]
            want = rnd.choice([None, "", "m", "M:latest", "Ж", str(junk(rnd))])
            model, window, note = servers.pick_model(want, on, rnd.choice(KINDS))
            self.assertIsInstance(note, str)
            if on:
                self.assertIn(model, [m[0] for m in on])


class Addresses(unittest.TestCase):
    """An address typed in any form: never a traceback, and what comes out is an http(s) URL or nothing."""
    PIECES = ["", " ", "http://", "https://", "HTTP://", "ftp://", "localhost", "127.0.0.1", "192.168.0.10",
              "[::1]", "::1", "pc-name", "пк", "host.example.com", ":", ":8080", "1234", "99999", "-1", "/v1",
              "/v1/chat/completions", "/models", "?a=b", "#x", "\\", "\"", "'", "%20", "@", "user:pw@", "\t",
              "\u0000", "😀", "../", "C:\\x", "api.openai.com", "x" * 300]

    def test_any_address(self):
        rnd = random.Random(SEED)
        for _ in range(3000 * ROUNDS):
            addr = "".join(rnd.choice(self.PIECES) for _ in range(rnd.randrange(1, 6)))
            with self.subTest(addr=addr, seed=SEED):
                p = servers.split_addr(addr)
                if p:
                    self.assertIn(p[0], ("http", "https"))
                    self.assertTrue(p[1])
                for url in servers.candidates(addr):
                    self.assertRegex(url, r"^https?://")
                base = launch.norm_base(addr, rnd.choice(list(launch.KINDS) + [launch.OTHER]))
                self.assertIsInstance(base, str)
                servers.cloud_kind(addr)
                servers.root(addr)
                with patch.dict(os.environ, {"MAXEDCODE_OFFLINE": "1"}):
                    servers.port_down(addr)
                launch._port(addr)

    def test_hosts_that_never_answer_are_left_quickly(self):
        """A typed address that nothing serves: find_base gives up in its time, whatever the form."""
        online(self)
        s = socket.socket()                     # bound, never listening: refused, and no one else gets the port
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        start = time.monotonic()
        # (a full ".../v1" address is one candidate: kept as typed, without a request)
        for addr in (f"127.0.0.1:{port}", f"localhost:{port}/api", f"http://127.0.0.1:{port}/api/"):
            self.assertIsNone(servers.find_base(addr, timeout=0.5))
        self.assertLess(time.monotonic() - start, 20)


class LaunchState(unittest.TestCase):
    """launch.json is written by the window, the start job and the CLI at once: it stays readable JSON and no
    writer raises. (The start job and the catalog of the window wrote it at the same moment.)"""
    def setUp(self):
        launch.save_state({})

    def test_many_writers_at_once(self):
        errors = []
        rnd = random.Random(SEED)

        def worker(n):
            try:
                for i in range(25 * ROUNDS):
                    pick = (n + i) % 5
                    if pick == 0:
                        launch.remember("lmstudio", model=f"m{n}", ctx=32768, opts={"gpu": "max"})
                    elif pick == 1:
                        launch.save_way("other", f"w{n}", f"cmd{n} --x", ":5001")
                    elif pick == 2:
                        launch.remember_quiet(lms_models=[{"id": f"m{n}-{i}"}])
                    elif pick == 3:
                        launch.keep_ours({"kind": "strata", "server": "strata", "base": "http://127.0.0.1:8080/v1"})
                    else:
                        launch.load_state()
                        launch.ways("other")
            except Exception as e:      # noqa: BLE001 - any error is the finding
                errors.append(repr(e))
        with patch.object(launch, "listener", lambda port: 1):
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [], f"seed {SEED}")
        data = json.loads(launch.state_file().read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertFalse(list(launch.state_file().parent.glob("launch*.tmp")), "temporary files left behind")
        del rnd

    def test_a_broken_file_is_an_empty_state_not_a_crash(self):
        rnd = random.Random(SEED)
        for _ in range(50 * ROUNDS):
            raw = bytes(rnd.randrange(256) for _ in range(rnd.randrange(200)))
            choice = rnd.choice([raw, b"[]", b"null", b'"x"', b"{\"ways\": 5}", b"{\"ways\": [1, null, {}]}",
                                 b"{\"paths\": []}", b"{\"ctx\": \"x\"}", b"{\"ours\": [1]}", b"\xef\xbb\xbf{}"])
            launch.state_file().write_bytes(choice)
            with self.subTest(content=choice[:60]):
                self.assertIsInstance(launch.load_state(), dict)
                launch.own_ways()
                launch.ours()
                launch.ways("other")
                launch.ways("strata", cli=True)
                launch.saved_path("llamacpp")
                launch.remember("ollama", model="m", ctx=32768)
                self.assertIsInstance(json.loads(launch.state_file().read_text(encoding="utf-8")), dict)


class GgufHeaders(unittest.TestCase):
    """A .gguf file of any bytes: gguf_ctx answers a number or None, quickly (it is read for every model shown)."""
    def test_random_headers(self):
        rnd = random.Random(SEED)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "m.gguf")
            for _ in range(300 * ROUNDS):
                kv = []
                for _ in range(rnd.randrange(5)):
                    key = rnd.choice([b"general.architecture", b"llama.context_length", b"x" * rnd.randrange(50)])
                    t = rnd.randrange(14)
                    val = rnd.choice([struct.pack("<I", rnd.randrange(2 ** 32)), struct.pack("<Q", rnd.randrange(2 ** 64)),
                                      struct.pack("<IQ", rnd.randrange(14), rnd.randrange(2 ** 64)), b""])
                    kv.append(struct.pack("<Q", rnd.choice([len(key), rnd.randrange(2 ** 64)])) + key + struct.pack("<I", t) + val)
                head = b"GGUF" + struct.pack("<IQQ", 3, 0, rnd.choice([len(kv), rnd.randrange(2 ** 64)]))
                path.write_bytes(rnd.choice([head + b"".join(kv), head, b"GGUF", b"", os.urandom(rnd.randrange(64))]))
                start = time.monotonic()
                ctx = launch.gguf_ctx(path)
                self.assertTrue(ctx is None or isinstance(ctx, int))
                self.assertLess(time.monotonic() - start, 2, f"seed {SEED}")


class SettingsValues(unittest.TestCase):
    """The window's settings form with any values: a ValueError naming the field, or saved; the files stay JSON."""
    def setUp(self):
        from maxedcode import appsettings
        self.s = appsettings
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.files = {"USER_SETTINGS": root / "settings.json", "CONFIG_FILE": root / "config.json",
                      "MEMORY_FILE": root / "MEMORY.md", "RECENT_FILE": root / "recent.json"}
        for name, path in self.files.items():
            p = patch.object(appsettings, name, path)
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(appsettings.config, "STATE_FILE", root / "state.json"),
                  patch.object(servers, "find_base", lambda addr, key="", timeout=2: addr)):
            p.start()
            self.addCleanup(p.stop)

    def test_random_values(self):
        rnd = random.Random(SEED)
        fields = list(self.s.FIELDS) + ["nope", ""]
        for _ in range(400 * ROUNDS):
            change = {rnd.choice(fields): junk(rnd) for _ in range(rnd.randrange(1, 4))}
            with self.subTest(change=str(change)[:120], seed=SEED):
                try:
                    self.s.apply(change)
                except ValueError:
                    pass
                for name in ("USER_SETTINGS", "CONFIG_FILE", "RECENT_FILE"):
                    path = self.files[name]
                    if path.exists():
                        self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), (dict, list))


if __name__ == "__main__":
    unittest.main()
