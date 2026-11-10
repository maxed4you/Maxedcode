"""Tiny OpenAI-compatible stub: 1st chat call -> a shell tool call, then a final text answer. Logs tool names seen."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
LOG = sys.argv[2] if len(sys.argv) > 2 else "stub_log.jsonl"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "stub-model", "object": "model"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        tools = [t["function"]["name"] for t in req.get("tools") or []]
        msgs = req.get("messages") or []
        tool_results = [m for m in msgs if m.get("role") == "tool"]
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tools": tools, "tool_results": [str(m.get("content"))[:300] for m in tool_results]}) + "\n")
        shell = "powershell" if "powershell" in tools else "bash"
        if not tool_results:
            cmd = "Write-Output ('hello-from-' + $PSVersionTable.PSEdition)" if shell == "powershell" else "echo hello-from-bash"
            delta = {"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": shell, "arguments": json.dumps({"command": cmd})}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Done: " + str(tool_results[-1].get("content"))[:80]}
            finish = "stop"
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "stub-model"}
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ch in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                       {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}):
                self.wfile.write(b"data: " + json.dumps({**base, **ch}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            msg = dict(delta)
            self._json({**base, "object": "chat.completion", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
