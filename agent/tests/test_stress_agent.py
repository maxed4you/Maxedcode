"""Stress: the headless agent loop end to end against a chaotic fake OpenAI-compatible model server.

No real model is loaded. A stdlib HTTP server on 127.0.0.1:<free port> answers /v1/models and
/v1/chat/completions (plain JSON and SSE streams) from a per-request script: malformed tool-call JSON, calls
left in the text, cut streams, empty choices, HTTP 5xx/429, a dropped connection, huge and odd content, endless
tool calls. Every run is the real `python local_agent.py --print ... --output-format stream-json` in a temp
folder with a temp home (the real ~/.maxedcode is never touched) and must END: exit code, a final `result`
event, no Python traceback, within a time bound.

The model requests go through the OpenAI SDK, not servers.http, so MAXEDCODE_OFFLINE=1 (set by
run_all_tests.py) does not keep them from the fake; it stays on for the child, except in the one run that
checks the server probe (resolve_server) against the fake.

MAXEDCODE_STRESS=<rounds> multiplies the seeded random rounds for a long manual run (default 1);
MAXEDCODE_STRESS_SEED=<n> picks another seed (a failing round names its seed)."""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

ROUNDS = max(1, int(os.environ.get("MAXEDCODE_STRESS", "1") or 1))
SEED = int(os.environ.get("MAXEDCODE_STRESS_SEED", "20261111"))
RUN_TIMEOUT = 45           # seconds one agent run may take (retry waits of 2 + 5 s included)
FINAL = "STRESS-FINAL-ANSWER: everything is finished and verified."


# ---------------------------------------------------------------------------------------------- the fake server

def final(text=FINAL, **kw):
    return {"text": text, **kw}


def call(name, args, **kw):
    """One tool call; args is a dict (sent as JSON) or a raw string (sent as is)."""
    return {"calls": [(name, args)], **kw}


class Chaos:
    """A model server on 127.0.0.1:<free port>. Each chat request takes the next step of `script`; when the
    script is used up, `default(n)` answers (n = chat requests so far), else the final answer. Service
    requests (the promise self-check, a compaction summary) are answered on the side and take no step."""

    def __init__(self, script=(), default=None):
        self.script, self.default = list(script), default
        self.requests, self.lock, self.chat_n = [], threading.Lock(), 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send_json(self, code, obj, headers=None):
                data = obj if isinstance(obj, bytes) else dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self.send_json(200, {"object": "list", "data": [{"id": "chaos-model", "object": "model"}]})
                self.send_json(404, {"error": "not found"})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b"{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self.send_json(404, {"error": "not found"})
                step = fake.next_step(body)
                fake.answer(self, body, step)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass          # a client that hung up (a dropped stream under test) is no error

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- which answer

    def next_step(self, body):
        msgs = body.get("messages") or []
        last = str((msgs[-1].get("content") if msgs else "") or "")
        with self.lock:
            self.requests.append(body)
            if "<self_check>" in last:
                return final("NO")
            if "<compact_request>" in last or "Summarize the conversation for continuation" in json.dumps(msgs[-2:]):
                return final("# Objective\nstress\n# Important Details\nnone\n# Work State\ndone\n# Next Steps\nanswer")
            self.chat_n += 1
            if self.script:
                return self.script.pop(0)
            return self.default(self.chat_n) if self.default else final()

    # -- how it is sent

    def answer(self, h, body, step):
        if "status" in step:
            return h.send_json(step["status"], step.get("body") or {"error": {"message": f"chaos {step['status']}"}},
                               step.get("headers"))
        if "raw" in step:
            return h.send_json(200, step["raw"])
        if body.get("stream"):
            return self.stream(h, step)
        if step.get("disconnect"):
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", "100000")
            h.end_headers()
            h.wfile.write(b'{"id": "x", "choices": [{"index": 0, "mess')
            h.wfile.flush()
            h.close_connection = True
            with_suppress(lambda: h.connection.shutdown(socket.SHUT_RDWR))
            return
        if step.get("empty_choices"):
            return h.send_json(200, {"id": "c", "object": "chat.completion", "model": "chaos-model", "choices": []})
        if "message" in step:
            return h.send_json(200, {"id": "c", "object": "chat.completion", "model": "chaos-model",
                                     "choices": [{"index": 0, "message": step["message"],
                                                  "finish_reason": step.get("finish", "tool_calls")}]})
        msg = {"role": "assistant", "content": step.get("text", "")}
        if step.get("reasoning"):
            msg["reasoning_content"] = step["reasoning"]
        calls = self.tool_calls(step)
        if calls:
            msg["tool_calls"] = calls
            if "text" not in step:
                msg["content"] = step.get("content")
        finish = step.get("finish", "tool_calls" if calls else "stop")
        h.send_json(200, {"id": "c", "object": "chat.completion", "model": "chaos-model",
                          "choices": [{"index": 0, "message": msg, "finish_reason": None if step.get("cut") else finish}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    def tool_calls(self, step):
        out = []
        for i, (name, args) in enumerate(step.get("calls") or []):
            arg = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            out.append({"id": f"call_{self.chat_n}_{i}", "type": "function", "function": {"name": name, "arguments": arg}})
        return out

    def stream(self, h, step):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()

        def send(obj):
            data = ("data: " + (obj if isinstance(obj, str) else dumps(obj)) + "\n\n").encode("utf-8")
            h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            h.wfile.flush()

        def chunk(delta, finish=None):
            send({"id": "c", "object": "chat.completion.chunk", "model": "chaos-model",
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        if step.get("empty_choices"):
            send({"id": "c", "object": "chat.completion.chunk", "choices": []})
        elif "deltas" in step:
            for d in step["deltas"]:
                chunk(d)
            chunk({}, step.get("finish", "tool_calls"))
        else:
            chunk({"role": "assistant"})
            reasoning = step.get("reasoning") or ""
            for i in range(0, len(reasoning), 40):
                chunk({"content": None, "reasoning_content": reasoning[i:i + 40]})
            text = step.get("text", step.get("content")) or ""
            for i in range(0, len(text), 50):
                chunk({"content": text[i:i + 50]})
                if step.get("disconnect") and i >= 50:
                    break
            if step.get("disconnect"):
                h.close_connection = True
                with_suppress(lambda: h.connection.shutdown(socket.SHUT_RDWR))
                return
            for i, c in enumerate(self.tool_calls(step)):
                args = c["function"]["arguments"]
                chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                       "function": {"name": c["function"]["name"], "arguments": ""}}]})
                for j in range(0, len(args), 7):
                    chunk({"tool_calls": [{"index": i, "function": {"arguments": args[j:j + 7]}}]})
            if not step.get("cut"):
                chunk({}, step.get("finish", "tool_calls" if step.get("calls") else "stop"))
                send({"id": "c", "object": "chat.completion.chunk", "choices": [],
                      "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
        send("[DONE]")
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()


def dumps(obj):
    """JSON as a server sends it: UTF-8 text, a lone surrogate as its escape (the way a broken model emits it)."""
    text = json.dumps(obj, ensure_ascii=False)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return json.dumps(obj)


def with_suppress(fn):
    try:
        fn()
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------- one agent run

class Run:
    def __init__(self, rc, out, err, seconds, work):
        self.rc, self.out, self.err, self.seconds, self.work = rc, out, err, seconds, work
        self.events = []
        for line in out.splitlines():
            if line.startswith("{"):
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass

    @property
    def result(self):
        res = [e for e in self.events if e.get("type") == "result"]
        return res[-1] if res else None

    def tool_results(self, tool=None):
        return [e for e in self.events if e.get("type") == "tool_result" and (tool is None or e.get("tool") == tool)]

    def why(self):
        return f"rc={self.rc} {self.seconds:.1f}s\n--- stderr (tail)\n{self.err[-3000:]}\n--- stdout (tail)\n{self.out[-2000:]}"


ROOTS = []


def tearDownModule():
    import shutil
    for root in ROOTS:
        shutil.rmtree(root, ignore_errors=True)


def agent(server, prompt="Do the stress task.", max_turns=8, env=None, setup=None, offline=True,
          fmt="stream-json", extra=(), root=None):
    """One real headless run against `server` in a fresh temp folder (or the folder of an earlier run: root);
    returns Run (work = the folder, removed when the module's tests are over)."""
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="mx-stress-"))
        ROOTS.append(root)
        for d in ("work dir", "home", "user"):
            (root / d).mkdir()
        if setup:
            setup(root / "work dir")
    work, home, user = root / "work dir", root / "home", root / "user"
    child = {**os.environ,
             "AGENT_HOME": str(home), "USERPROFILE": str(user), "HOME": str(user),
             "MAXEDCODE_NO_UPDATE": "1", "MAXEDCODE_OFFLINE": "1" if offline else "",
             "MAXEDCODE_MANAGED_SETTINGS": str(root / "no-managed.json"),
             "AGENT_CTX": "32768", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
             "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
             "AGENT_MODE": "", "AGENT_APPROVAL_DIR": "", "AGENT_EFFORT": "medium",
             # beat a config.json next to local_agent.py (a git copy may carry the author's LM Studio one)
             "AGENT_MODEL": "chaos-model", "AGENT_SERVER": "llamacpp"}
    for k in ("AGENT_BASE_URL", "AGENT_API_KEY", "AGENT_HEADLESS_STREAM", "HTTP_PROXY", "HTTPS_PROXY",
              "http_proxy", "https_proxy", "ALL_PROXY"):
        child.pop(k, None)
    child.update(env or {})
    child = {k: v for k, v in child.items() if v is not None}
    cmd = [sys.executable, str(AGENT / "local_agent.py"), "--print", prompt, "--output-format", fmt,
           "--base-url", server.base, "--home", str(home), "--yes", "--max-turns", str(max_turns), *extra]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(work), env=child, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=RUN_TIMEOUT)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = "TIMEOUT", e.stdout or b"", e.stderr or b""
    run = Run(rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), time.monotonic() - t0, work)
    run.root = root
    return run


class StressBase(unittest.TestCase):
    def ended(self, run, answer=FINAL):
        """The run ended cleanly: exit 0, a final result, no traceback; `answer` in the result when given."""
        self.assertNotIn("Traceback (most recent call last)", run.err, run.why())
        self.assertEqual(run.rc, 0, run.why())
        self.assertIsNotNone(run.result, run.why())
        self.assertEqual(run.events[-1].get("type"), "result", run.why())
        if answer is not None:
            self.assertIn(answer, str(run.result.get("result")), run.why())

    def go(self, script, default=None, **kw):
        server = Chaos(script, default)
        try:
            return agent(server, **kw), server
        finally:
            server.stop()


def parallel(jobs):
    """Run (name, fn) jobs side by side; -> {name: result}."""
    with ThreadPoolExecutor(max_workers=min(8, max(2, os.cpu_count() or 4))) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs}
        return {name: f.result() for name, f in futures.items()}


# ---------------------------------------------------------------------------------------------- scenarios

CYR = "папка с пробелом"
NAME = "файл данных 1.txt"
CRLF_NAME = "заметки crlf.txt"


def project(work):
    d = work / CYR
    d.mkdir()
    (d / NAME).write_text("alpha line\nbeta строка 🚀\ngamma\n", encoding="utf-8")
    (d / CRLF_NAME).write_bytes("first line\r\nsecond строка\r\nthird line\r\n".encode("utf-8"))


def path_in(run_or_work, name=NAME):
    work = getattr(run_or_work, "work", run_or_work)
    return str(work / CYR / name)


class MalformedToolCalls(StressBase):
    """Broken arguments never crash the loop: each is repaired, rejected with feedback, or stopped."""

    def test_broken_arguments_of_every_kind_end_the_run(self):
        p = "{P}"          # replaced by the real path in the run's folder
        bad = [
            ("read_file", '{"path": "' + p),                                 # cut
            ("read_file", '{"path": "' + p + '"} trailing garbage }}'),       # trailing garbage
            ("read_file", "{'path': '" + p + "'}"),                          # single quotes
            ("read_file", '{"path": ["' + p + '"], "offset": "ten", "limit": {"a": 1}}'),   # wrong types
            ("no_such_tool_xyz", '{"path": "' + p + '"}'),                  # unknown tool
            ("edit_file", "{}"),                                             # missing required args
            ("write_file", '{"path": 42, "content": null}'),                 # wrong types for required args
            ("grep", "[1, 2, 3]"),                                           # an array, not an object
            ("read_file", '"just a string"'),                                # a bare JSON string
            ("read_file", "123"),                                            # a bare number
            ("read_file", ""),                                               # empty arguments
            ("read_file", "null"),                                           # JSON null
            ("", '{"path": "' + p + '"}'),                                   # empty tool name
            ("read_file", '{"path": "' + p + '", "path": "dup"}'),          # duplicate key
            ("read_file", '{"path": "\\ud800' + p + '"}'),                   # lone surrogate escape
            ("read_file", '```json\n{"path": "' + p + '"}\n```'),            # fenced
        ]
        jobs = []
        for i, (name, raw) in enumerate(bad):
            for stream in ("0", "1"):
                def job(name=name, raw=raw, stream=stream):
                    holder = {}

                    def setup(work):
                        project(work)
                        holder["p"] = path_in(work).replace("\\", "\\\\")

                    server = Chaos([], None)
                    first = [True]

                    def default(n):
                        if n <= 2:
                            return call(name, raw.replace("{P}", holder["p"]))
                        return final()
                    server.default = default
                    try:
                        return agent(server, setup=setup, env={"AGENT_HEADLESS_STREAM": stream})
                    finally:
                        server.stop()
                jobs.append((f"{i}:{name}:{raw[:30]!r}:stream={stream}", job))
        for label, run in parallel(jobs).items():
            with self.subTest(label):
                self.ended(run)


class TextCalls(StressBase):
    def test_calls_left_in_the_text_are_run(self):
        def runs(style, stream):
            def job():
                holder = {}

                def setup(work):
                    project(work)
                    holder["p"] = path_in(work)

                def default(n):
                    p = holder["p"]
                    if n == 1:
                        if style == "hermes":
                            return final('<tool_call>\n{"name": "read_file", "arguments": {"path": ' + json.dumps(p) + '}}\n</tool_call>')
                        if style == "qwen":
                            return final(f"<tool_call>\n<function=read_file>\n<parameter=path>\n{p}\n</parameter>\n</function>\n</tool_call>")
                        if style == "bare":
                            return final(json.dumps({"name": "read_file", "arguments": {"path": p}}))
                        if style == "broken":
                            return final("<tool_call><=read_file><parameter=path></function></tool_call>")
                        if style == "in_think":
                            return final("", reasoning='<tool_call>\n{"name": "read_file", "arguments": {"path": ' + json.dumps(p) + '}}\n</tool_call>')
                    return final()
                server = Chaos([], default)
                try:
                    return agent(server, setup=setup, env={"AGENT_HEADLESS_STREAM": stream})
                finally:
                    server.stop()
            return job
        styles = ("hermes", "qwen", "bare", "broken", "in_think")
        res = parallel([(f"{s}:{st}", runs(s, st)) for s in styles for st in ("0", "1")])
        for label, run in res.items():
            with self.subTest(label):
                self.ended(run)
                if not label.startswith(("broken", "in_think")):
                    reads = run.tool_results("read_file")
                    self.assertTrue(reads and "beta" in reads[0]["content"], run.why())


class TransportChaos(StressBase):
    """Server failures: retried (llmretry) and the run still ends with the real answer."""

    def test_failures_then_success(self):
        retried = "(server failed: "
        cases = {        # label: (script, stream, what stderr must say)
            "500-then-ok": ([{"status": 500}, final()], "0", retried),
            "503-429-then-ok": ([{"status": 503}, {"status": 429, "headers": {"Retry-After": "0"}}, final()], "0", "retry 2/"),
            "disconnect-mid-body": ([{"disconnect": True, "text": "x" * 400}, final()], "0", retried),
            "empty-choices": ([{"empty_choices": True}, final()], "0", "BadReply"),
            "garbage-200-body": ([{"raw": b"<html>this is not json"}, final()], "0", "BadReply"),
            "sse-disconnect": ([{"disconnect": True, "text": "partial text " * 40}, final()], "1", retried),
            "sse-cut-no-finish": ([call("glob", {"pattern": "*.txt"}), {"cut": True, "text": "cut here"}, final()], "1", "StreamCut"),
            "sse-empty-choices": ([{"empty_choices": True}, final()], "1", "empty"),
            "sse-500-then-ok": ([{"status": 500}, final()], "1", retried),
            "sse-garbage-200-body": ([{"raw": b"<html>this is not json"}, final()], "1", ""),
        }
        jobs = [(k, lambda v=v, st=st: self.go(list(v), env={"AGENT_HEADLESS_STREAM": st})[0])
                for k, (v, st, _) in cases.items()]
        for label, run in parallel(jobs).items():
            with self.subTest(label):
                self.ended(run)
                self.assertIn(cases[label][2], run.err, run.why())

    def test_a_server_that_stays_down_ends_with_one_line(self):
        """Past the retries: exit 1 and one stderr line (the window shows it), never a traceback."""
        cases = {
            "503": ({"status": 503}, "0"),
            "empty-choices": ({"empty_choices": True}, "0"),
            "garbage": ({"raw": b"not json"}, "0"),
            "disconnect": ({"disconnect": True, "text": "y" * 300}, "0"),
            "sse-disconnect": ({"disconnect": True, "text": "y" * 300}, "1"),
            "sse-500": ({"status": 500}, "1"),
        }
        jobs = [(k, lambda step=step, st=st: self.go([], lambda n: dict(step),
                                                     env={"AGENT_LLM_RETRIES": "1", "AGENT_HEADLESS_STREAM": st})[0])
                for k, (step, st) in cases.items()]
        for label, run in parallel(jobs).items():
            with self.subTest(label):
                self.assertNotIn("Traceback (most recent call last)", run.err, run.why())
                self.assertEqual(run.rc, 1, run.why())
                self.assertIn("model server error", run.err.strip().splitlines()[-1], run.why())

    def test_a_stream_cut_forever_ends(self):
        """Every stream after the first ends without a finish reason: retried, then the one-line error."""
        run, _ = self.go([call("glob", {"pattern": "*.txt"})], lambda n: {"cut": True, "text": "cut"},
                         env={"AGENT_LLM_RETRIES": "1", "AGENT_HEADLESS_STREAM": "1"})
        self.assertNotIn("Traceback (most recent call last)", run.err, run.why())
        self.assertEqual(run.rc, 1, run.why())
        self.assertIn("StreamCut", run.err, run.why())


class RawShapes(StressBase):
    """Tool calls in shapes the OpenAI schema does not allow, as real servers send them."""

    def test_odd_tool_call_shapes(self):
        def tc(fn, **kw):
            return {"id": "call_x", "type": "function", "function": fn, **kw}
        plain = {
            "args-as-object": {"role": "assistant", "content": None,
                               "tool_calls": [tc({"name": "glob", "arguments": {"pattern": "*.txt"}})]},
            "args-null": {"role": "assistant", "content": None, "tool_calls": [tc({"name": "glob", "arguments": None})]},
            "name-null": {"role": "assistant", "content": None, "tool_calls": [tc({"name": None, "arguments": "{}"})]},
            "no-id": {"role": "assistant", "content": None,
                      "tool_calls": [{"type": "function", "function": {"name": "glob", "arguments": "{}"}}]},
            "two-same-id": {"role": "assistant", "content": "",
                            "tool_calls": [tc({"name": "glob", "arguments": '{"pattern": "*.a"}'}),
                                           tc({"name": "glob", "arguments": '{"pattern": "*.b"}'})]},
            "tool-calls-empty-list": {"role": "assistant", "content": FINAL, "tool_calls": []},
            "content-list": {"role": "assistant", "content": [{"type": "text", "text": FINAL}]},
            "function-null": {"role": "assistant", "content": None, "tool_calls": [tc(None)]},
            "content-number": {"role": "assistant", "content": 42},
            "cut-length-array-args": None,
        }
        streamed = {
            "sse-args-split-unicode": [{"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                        "function": {"name": "glob", "arguments": ""}}]}]
                                      + [{"tool_calls": [{"index": 0, "function": {"arguments": ch}}]}
                                         for ch in '{"pattern": "*файл 🚀*"}'],
            "sse-name-in-pieces": [{"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "gl", "arguments": ""}}]},
                                   {"tool_calls": [{"index": 0, "function": {"name": "ob", "arguments": '{"pattern": "*"}'}}]}],
            "sse-index-gap": [{"tool_calls": [{"index": 3, "id": "c3", "function": {"name": "glob", "arguments": "{}"}}]}],
            "sse-no-index": [{"tool_calls": [{"id": "c1", "function": {"name": "glob", "arguments": "{}"}}]}],
            "sse-null-delta-fields": [{"content": None, "tool_calls": None, "role": None}],
            "sse-args-as-object": [{"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "glob",
                                                                                  "arguments": {"pattern": "*"}}}]}],
            "sse-function-null": [{"tool_calls": [{"index": 0, "id": "c1", "function": None}]}],
        }
        jobs = []
        for k, m in plain.items():
            step = ({"calls": [("edit_file", "[1, 2]")], "finish": "length"} if m is None else {"message": m})
            jobs.append((k, lambda step=step: self.go([step])[0]))
        for k, d in streamed.items():
            jobs.append((k, lambda d=d: self.go([{"deltas": d}], env={"AGENT_HEADLESS_STREAM": "1"})[0]))
        for label, run in parallel(jobs).items():
            with self.subTest(label):
                self.ended(run, answer="42" if label == "content-number" else FINAL)


class OddContent(StressBase):
    def test_huge_unicode_nul_and_reasoning(self):
        weird = "emoji 🚀🧪👩‍💻 RTL שלום עליכם مرحبا NUL[\x00] bell[\x07] surrogate-free 𝔘𝔫𝔦 \u202e reversed \u202c"
        holder = {}

        def setup(work):
            project(work)
            holder["work"] = work

        def write_weird(n):
            if n == 1:
                return call("write_file", {"path": str(holder["work"] / "weird ü 名前.txt"), "content": weird})
            return final(FINAL + " " + weird)
        jobs = {
            "huge-1mb": lambda: self.go([final(FINAL + "x" * (1024 * 1024))])[0],
            "huge-1mb-stream": lambda: self.go([final(FINAL + "y" * (1024 * 1024))], env={"AGENT_HEADLESS_STREAM": "1"})[0],
            "weird-text": lambda: self.go([final(FINAL + weird)])[0],
            "weird-text-stream": lambda: self.go([final(FINAL + weird)], env={"AGENT_HEADLESS_STREAM": "1"})[0],
            "weird-args": lambda: self.go([], write_weird, setup=setup)[0],
            "reasoning-stream": lambda: self.go([final(FINAL, reasoning="Let me think about it. " * 200)],
                                                env={"AGENT_HEADLESS_STREAM": "1"})[0],
            "reasoning-plain": lambda: self.go([final(FINAL, reasoning="Thinking step. " * 100)])[0],
            "think-tags": lambda: self.go([final("<think>hidden plan</think>" + FINAL)])[0],
            "only-reasoning-then-answer": lambda: self.go([final("", reasoning="I am thinking"), final()])[0],
            "empty-then-answer": lambda: self.go([final(""), final()])[0],
            "huge-tool-args": lambda: self.go([], lambda n: call("write_file", {
                "path": str(holder["w2"] / "big.txt"), "content": "z" * (1024 * 1024)}) if n == 1 else final(),
                setup=lambda w: holder.update(w2=w))[0],
        }
        res = parallel(list(jobs.items()))
        for label, run in res.items():
            with self.subTest(label):
                self.ended(run)
        f = holder["work"] / "weird ü 名前.txt"
        self.assertTrue(f.exists(), res["weird-args"].why())
        self.assertEqual(f.read_text(encoding="utf-8").replace("\x00", ""), weird.replace("\x00", ""))


class Loops(StressBase):
    def test_same_call_forever_is_stopped(self):
        def looping(step_of, max_turns, stream="0"):
            def job():
                holder = {}

                def setup(work):
                    project(work)
                    holder["p"] = path_in(work)
                return self.go([], lambda n: step_of(holder["p"], n), setup=setup, max_turns=max_turns,
                               env={"AGENT_HEADLESS_STREAM": stream})[0]
            return job
        cases = {     # label: (job, max turns)
            "read-forever": (looping(lambda p, n: call("read_file", {"path": p}), 12), 12),
            "grep-forever-stream": (looping(lambda p, n: call("grep", {"pattern": "beta", "path": str(Path(p).parent)}),
                                            12, "1"), 12),
            "new-call-forever": (looping(lambda p, n: call("glob", {"pattern": f"*{n}*.txt"}), 6), 6),
            "invalid-forever": (looping(lambda p, n: call("edit_file", "{"), 20), 20),
            "text-call-forever": (looping(lambda p, n: final('<tool_call>{"name": "read_file", "arguments": {"path": '
                                                             + json.dumps(p) + '}}</tool_call>'), 8), 8),
            "broken-markup-forever": (looping(lambda p, n: final("<tool_call><function=>"), 8), 8),
            "empty-forever": (looping(lambda p, n: final(""), 8), 8),
            "promise-forever": (looping(lambda p, n: final("Now I will fix the file:"), 8), 8),
        }
        res = parallel([(k, job) for k, (job, _) in cases.items()])
        for label, run in res.items():
            with self.subTest(label):
                self.ended(run, answer=None)
                max_turns = cases[label][1]
                steps = [e for e in run.events if e.get("type") == "assistant"]
                self.assertLessEqual(len(steps), max_turns + 1, run.why())     # + the wrap-up step
                if label == "read-forever":
                    blocked = [t for t in run.tool_results("read_file") if t["content"].startswith("Blocked")]
                    self.assertTrue(blocked, run.why())


class Files(StressBase):
    def test_read_write_edit_grep_on_cyrillic_paths_keep_crlf(self):
        holder = {}

        def setup(work):
            project(work)
            holder["work"] = work

        def default(n):
            w = holder["work"]
            d = w / CYR
            steps = {
                1: call("read_file", {"path": str(d / NAME)}),
                2: call("edit_file", {"path": str(d / CRLF_NAME), "old_text": "second строка", "new_text": "second ИЗМЕНЕНО"}),
                3: call("grep", {"pattern": "ИЗМЕНЕНО", "path": str(d)}),
                4: call("write_file", {"path": str(d / "новый файл.txt"), "content": "одна\nдве\n"}),
                5: call("edit_file", {"path": str(d / CRLF_NAME), "old_text": "third line\nfourth", "new_text": "never"}),
                6: {"calls": [("read_file", {"path": str(d / CRLF_NAME)}), ("glob", {"pattern": "*.txt", "path": str(d)})]},
            }
            return steps.get(n) or final()
        for stream in ("0", "1"):
            with self.subTest(stream=stream):
                run, _ = self.go([], default, setup=setup, env={"AGENT_HEADLESS_STREAM": stream})
                self.ended(run)
                d = holder["work"] / CYR
                data = (d / CRLF_NAME).read_bytes()
                self.assertEqual(data, "first line\r\nsecond ИЗМЕНЕНО\r\nthird line\r\n".encode("utf-8"), run.why())
                self.assertEqual((d / "новый файл.txt").read_text(encoding="utf-8"), "одна\nдве\n")
                greps = run.tool_results("grep")
                self.assertTrue(greps and "ИЗМЕНЕНО" in greps[0]["content"], run.why())
                reads = run.tool_results("read_file")
                self.assertIn("beta строка 🚀", reads[0]["content"], run.why())


class FormatsAndResume(StressBase):
    """A lone surrogate (an unpaired \\ud800 escape from the model) in the answer, in every output format, and a
    session saved with it that --continue resumes; a prompt in several scripts."""

    def test_lone_surrogate_in_every_format_and_resume(self):
        odd = "answer \ud800 half-pair \udfff " + FINAL
        jobs = [(fmt, lambda fmt=fmt: self.go([call("glob", {"pattern": "*\ud800*"}), final(odd)], fmt=fmt)[0])
                for fmt in ("stream-json", "json", "text")]
        res = parallel(jobs)
        for fmt, run in res.items():
            with self.subTest(fmt):
                self.assertNotIn("Traceback (most recent call last)", run.err, run.why())
                self.assertEqual(run.rc, 0, run.why())
                self.assertIn(FINAL, run.out, run.why())
        self.ended(res["stream-json"])
        server = Chaos([final()])
        try:
            again = agent(server, prompt="continue", root=res["stream-json"].root, extra=["--continue"])
        finally:
            server.stop()
        self.ended(again)
        sent = json.dumps(server.requests[0]["messages"], ensure_ascii=False)
        self.assertIn("half-pair", sent, again.why())         # the history came back

    def test_prompt_in_many_scripts(self):
        prompt = "Сделай задачу 🚀 שלום مرحبا 名前 «quotes» & | > %PATH%"
        run, server = self.go([final()], prompt=prompt)
        self.ended(run)
        self.assertIn("שלום", json.dumps(server.requests[0]["messages"], ensure_ascii=False), run.why())


class ServerProbe(StressBase):
    def test_resolve_server_against_the_fake(self):
        """Without AGENT_CTX the run asks the server what it is (servers.describe): the fake answers /models
        only, 404 for the rest (/health, /props), and the run goes on with the default window."""
        run, server = self.go([final()], offline=False, env={"AGENT_CTX": None, "AGENT_SERVER": "strata"})
        self.ended(run)
        self.assertEqual(server.requests[-1].get("model"), "chaos-model", run.why())


# ---------------------------------------------------------------------------------------------- seeded random rounds

def random_step(rng, p):
    """One chaotic reply: a broken or odd tool call, text calls, transport trouble, odd content."""
    pj = json.dumps(p)
    kinds = [
        lambda: call("read_file", {"path": p}),
        lambda: call("read_file", '{"path": ' + pj[:rng.randint(1, len(pj))]),
        lambda: call("read_file", "{'path': " + repr(p) + "}"),
        lambda: call("read_file", '{"path": ' + pj + '}' + rng.choice(["}", "]]", " garbage", ",", "\n\n{"])),
        lambda: call(rng.choice(["Read_File", "readfile", "read-file", "cat", "open_file", "zzz"]), {"path": p}),
        lambda: call("grep", {"pattern": rng.choice(["beta", "[", "(?P<", "\\", "строка", "🚀"]), "path": str(Path(p).parent)}),
        lambda: call("glob", {"pattern": rng.choice(["*.txt", "**/*", "[", "*файл*"]), "path": str(Path(p).parent)}),
        lambda: call("edit_file", {"path": p, "old_text": rng.choice(["beta", "nothing-here", ""]), "new_text": "BETA"}),
        lambda: call("write_file", {"path": str(Path(p).parent / f"out {rng.randint(0, 9)} ü.txt"),
                                    "content": "".join(rng.choice("ab\n\r\t🚀ж\x00") for _ in range(rng.randint(0, 200)))}),
        lambda: call("read_file", {"path": p, "offset": rng.choice([-5, 0, "x", 10 ** 12, None]), "limit": rng.choice([0, -1, "all", 3.5])}),
        lambda: final('<tool_call>\n{"name": "read_file", "arguments": {"path": ' + pj + '}}\n</tool_call>'),
        lambda: final(f"<tool_call>\n<function=read_file>\n<parameter=path>\n{p}\n</parameter>\n</function>\n</tool_call>"),
        lambda: final("<tool_call><function=" + rng.choice(["", "read_file", "?"]) + ">"),
        lambda: {"calls": [("read_file", {"path": p}), ("grep", {"pattern": "a", "path": str(Path(p).parent)})]},
        lambda: {"calls": [("read_file", {"path": p}), ("write_file", '{"path": ')]},
        lambda: {"status": rng.choice([500, 502, 503])},
        lambda: {"status": 429, "headers": {"Retry-After": "0"}},
        lambda: {"empty_choices": True},
        lambda: final("", reasoning="hmm " * rng.randint(1, 50)),
        lambda: final(""),
        lambda: final("Some text with NUL \x00 and RTL \u202e and emoji 🚀 but no answer marker."[:rng.randint(0, 80)]),
        lambda: call("todo_write", rng.choice(['{"todos": "not a list"}', '{"todos": [{"content": 1}]}', "{}"])),
        lambda: call("finish", rng.choice(['{"text": 5}', "{}", '{"text": "' + FINAL + '"}'])),
    ]
    return rng.choice(kinds)()


class RandomRounds(StressBase):
    def test_seeded_random_scripts_always_end(self):
        n = 6 * ROUNDS
        jobs = []
        for i in range(n):
            def job(i=i):
                rng = random.Random(SEED + i)
                stream = rng.choice(["0", "1"])
                holder = {}

                def setup(work):
                    project(work)
                    holder["p"] = path_in(work)
                length = rng.randint(2, 7)

                def default(k):
                    if k <= length:
                        return random_step(rng, holder["p"])
                    return final()
                server = Chaos([], default)
                try:
                    return agent(server, setup=setup, max_turns=rng.randint(3, 10),
                                 env={"AGENT_HEADLESS_STREAM": stream})
                finally:
                    server.stop()
            jobs.append((f"seed={SEED + i}", job))
        for label, run in parallel(jobs).items():
            with self.subTest(label):
                self.assertNotIn("Traceback (most recent call last)", run.err, run.why())
                self.assertNotEqual(run.rc, "TIMEOUT", run.why())
                # a run ends with a result, or (the server failed past the retries) with the one-line error
                if run.result is None:
                    self.assertEqual(run.rc, 1, run.why())
                    self.assertIn("model server error", run.err.strip().splitlines()[-1], run.why())
                else:
                    self.assertEqual(run.rc, 0, run.why())


class ArgumentPipelineFuzz(unittest.TestCase):
    """In-process: the argument path of every tool call (name -> repair -> normalize -> the args the loop uses)
    never raises, and always gives the loop a dict, on thousands of mutated inputs."""

    def test_arguments_that_are_not_text(self):
        """Arguments as an object, null or a number (servers send them so): repair_json never raises."""
        from maxedcode.util import repair_json
        from maxedcode.tool_validation import arguments_text, content_text
        self.assertEqual(repair_json({"a": 1}), {"a": 1})
        for v in (None, 5, 2.5, True, ["a"]):
            self.assertIsNone(repair_json(v))
        self.assertIsNone(repair_json('{"a": {"b"}}'))          # a Python set literal: no JSON value
        self.assertEqual(arguments_text({"p": "ж"}), '{"p": "ж"}')
        self.assertEqual((arguments_text(None), arguments_text("")), ("{}", "{}"))
        self.assertEqual(content_text([{"type": "text", "text": "a"}, {"type": "image_url"}, "b"]), "ab")

    def test_mutated_arguments_never_raise(self):
        from maxedcode.util import repair_json
        from maxedcode.tool_validation import canonical_tool_name, normalize_tool_arguments, text_tool_calls, \
            broken_text_call, sanitize_tool_history
        from maxedcode.tools.catalog import session_catalog
        tools = session_catalog(None, include_search=True)
        names = [t["function"]["name"] for t in tools] + ["", "zz", "Read_File", "mcp__x__y"]
        rng = random.Random(SEED)
        seeds = ['{"path": "C:/a b/файл.txt"}', '{"command": "dir", "timeout": 5}', '{"todos": [{"content": "x", "status": "pending"}]}',
                 '{"path": "x", "old_text": "a", "new_text": "b"}', '{"pattern": "a", "path": "."}', '{"blocks": [{"old_text": "a", "new_text": "b"}]}',
                 '[1, 2]', '"s"', "null", "", "{'a': 'b'}"]
        alphabet = list('{}[]":,\\\' \n\t\x00\ud800🚀жnulltrue0123456789abc') + ['\\u', '\\ud800', '"path"', "</tool_call>", "<parameter="]
        for _ in range(1500 * ROUNDS):
            s = list(rng.choice(seeds))
            for _ in range(rng.randint(0, 6)):
                op = rng.random()
                pos = rng.randint(0, len(s))
                if op < 0.4:
                    s.insert(pos, rng.choice(alphabet))
                elif op < 0.7 and s:
                    del s[min(pos, len(s) - 1)]
                else:
                    s = s[:pos]
            raw = "".join(s)
            name = rng.choice(names)
            try:
                n2 = canonical_tool_name(name, tools, repair_json(raw or "{}"))
                parsed, canonical, err = normalize_tool_arguments(n2, raw, tools, rng.choice([None, "stop", "length", "tool_calls"]))
                args = repair_json(canonical or "{}") or {}
                self.assertIsInstance(args, dict, (name, raw, canonical))
                text_tool_calls(raw, "t", tools)
                broken_text_call(raw)
                sanitize_tool_history([{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                                       {"role": "assistant", "content": raw,
                                        "tool_calls": [{"id": "1", "type": "function", "function": {"name": n2, "arguments": canonical}}]},
                                       {"role": "tool", "tool_call_id": "1", "content": raw}], tools)
            except Exception as e:     # noqa: BLE001 - any exception is the finding
                self.fail(f"{type(e).__name__}: {e} for name={name!r} raw={raw!r}")


if __name__ == "__main__":
    unittest.main()
