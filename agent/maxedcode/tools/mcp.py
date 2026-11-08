"""MCP servers: config, startup, calls."""
from contextlib import suppress
import asyncio
import json
import os
import shutil
from ..config import DEFER_TOOLS, HOME, MCP_DEFER, MCP_FILE

MCP_CALL_TIMEOUT = float(os.environ.get("AGENT_MCP_TIMEOUT", "120"))   # one tool call at most (DeepSeek Harness: 60)
MCP_RESTARTS = 3          # reconnects of a dropped server per session
MCP_CONNECT_WAIT = 15     # seconds a call waits for a reconnecting server

# ───────────────────────── MCP ─────────────────────────
class MCP:
    def __init__(self):
        self.srv = {}  # name -> {status, tools, client, stop}
        self.selected = set()  # deferred MCP schemas requested by the model

    def _all_specs(self):
        out = []
        for n, st in self.srv.items():
            # The schemas of a server that dropped stay: the tool list is part of the cached prefix, and a
            # call to such a tool reconnects the server (call()).
            for t in st["tools"]:
                sch = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
                out.append({"type": "function", "function": {"name": tool_name(n, t.name),
                            "description": (t.description or "")[:500], "parameters": sch}})
        return out

    @property
    def deferred_active(self):
        return DEFER_TOOLS and len(self._all_specs()) > MCP_DEFER

    def names(self):
        return sorted(x["function"]["name"] for x in self._all_specs())

    def tool_search(self, name):
        """Activate and describe one exact MCP tool schema for the next model request."""
        query = str(name or "").strip()
        all_specs = self._all_specs()
        matches = [x for x in all_specs if x["function"]["name"] == query]
        if not matches:
            matches = [x for x in all_specs if x["function"]["name"].split("__", 2)[-1] == query]
        if len(matches) != 1:
            names = self.names()
            hint = ", ".join(names[:40]) + (f", … ({len(names)} total)" if len(names) > 40 else "")
            return f"Error: expected one exact MCP tool name; matches={len(matches)}. Available names: {hint or 'none'}"
        spec = matches[0]
        full = spec["function"]["name"]
        self.selected.add(full)
        return "MCP tool schema loaded for the next model turn:\n" + json.dumps(spec, ensure_ascii=False, indent=2)

    @staticmethod
    def cfg():
        with suppress(Exception):
            return json.loads(MCP_FILE.read_text(encoding="utf-8")).get("mcpServers", {})
        return {}

    @staticmethod
    def save(servers):
        HOME.mkdir(parents=True, exist_ok=True)
        MCP_FILE.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(self, name, c):
        self.stop(name)
        self.srv[name] = {"status": "connecting…", "tools": [], "client": None, "stop": asyncio.Event(),
                          "cfg": c, "restarts": 0}
        asyncio.create_task(self._run(name, c))

    async def _reconnect(self, name):
        """Start a dropped server again (at most MCP_RESTARTS times) and wait until it is up or failed."""
        st = self.srv[name]
        if st["restarts"] >= MCP_RESTARTS or not st.get("cfg"):
            return False
        st["restarts"] += 1
        st.update(status="reconnecting…", stop=asyncio.Event())
        asyncio.create_task(self._run(name, st["cfg"]))
        for _ in range(int(MCP_CONNECT_WAIT * 10)):
            if st["client"] or str(st["status"]).startswith("error"):
                break
            await asyncio.sleep(0.1)
        return bool(st["client"])

    def stop(self, name):
        if name in self.srv:
            self.srv.pop(name)["stop"].set()

    async def _run(self, name, c):
        st = self.srv[name]
        errlog = None
        try:
            try:   # import only on the first server: the mcp package takes ~0.4 s to load, and there may be no servers
                from mcp import Client as MCPClient, StdioServerParameters
                from mcp.client.stdio import stdio_client
            except ImportError:
                raise RuntimeError('the mcp 2.x package is required: pip install -U "mcp>=2"') from None
            if c.get("url"):
                target = c["url"]
            else:
                cmd = c["command"]
                params = StdioServerParameters(command=shutil.which(cmd) or cmd, args=c.get("args", []),
                                               env={**os.environ, **c.get("env", {})})
                # Inside the TUI sys.stderr is Textual's capture (fileno -1): the child cannot inherit it
                # (OSError: Bad file descriptor), and the real console would garble the screen. Use a file.
                HOME.mkdir(parents=True, exist_ok=True)
                errlog = open(HOME / f"mcp-{name}.log", "a", encoding="utf-8")
                target = stdio_client(params, errlog=errlog)
            async with MCPClient(target) as cl:
                tools = (await cl.list_tools()).tools
                st.update(tools=tools, client=cl, status=f"ok, tools: {len(tools)}")
                await st["stop"].wait()
        except Exception as e:
            st["status"] = f"error: {type(e).__name__}: {e}"[:200]
        finally:
            st["client"] = None
            if errlog:
                errlog.close()

    def specs(self):
        """Return all schemas normally, or only schemas explicitly loaded via tool_search."""
        all_specs = self._all_specs()
        if self.deferred_active:
            return sorted([x for x in all_specs if x["function"]["name"] in self.selected],
                          key=lambda x: x["function"]["name"])
        return sorted(all_specs, key=lambda x: x["function"]["name"])

    async def call(self, full, args):
        # By the exact generated name: "__" inside a server name broke full.split("__", 2).
        n, tool = next(((sn, t.name) for sn, s in self.srv.items() for t in s["tools"] if tool_name(sn, t.name) == full),
                       (full.split("__", 2) + ["", ""])[1:3])
        st = self.srv.get(n)
        if st and not st["client"] and st["tools"]:
            await self._reconnect(n)
        if not st or not st["client"]:
            return f"Error: MCP server {n} is not connected ({st['status'] if st else 'not configured'})"
        real = tool
        try:
            # A hung server used to hold the turn until Esc (and forever in headless).
            r = await asyncio.wait_for(st["client"].call_tool(real, args), MCP_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            return f"Error: MCP tool {full} did not answer in {MCP_CALL_TIMEOUT:g} s; nothing more is known about its effect."
        txt = "\n".join(_content_text(c) for c in r.content)
        return ("Tool error: " if getattr(r, "is_error", False) else "") + txt


def tool_name(server, tool):
    """A valid, unique function name for an MCP tool: [a-zA-Z0-9_-] only, at most 64 characters.

    A dot or a space in a server/tool name gave an invalid function name, and the plain [:64] cut made two
    long names one (Cline: sanitize, then cut with a hash suffix)."""
    import hashlib
    import re
    raw = f"mcp__{server}__{tool}"
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    if len(name) > 64:
        name = name[:55] + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return name


def _content_text(c):
    """One MCP content item as text. An image was str(c) with its whole base64 body in the model context."""
    text = getattr(c, "text", None)
    if isinstance(text, str):
        return text
    if getattr(c, "type", "") in ("image", "audio"):
        data = getattr(c, "data", "") or ""
        return f"[{c.type} {getattr(c, 'mimeType', '') or ''}, ~{len(data) * 3 // 4:,} bytes; not shown]"
    s = str(c)
    return s if len(s) <= 2000 else s[:2000] + f" …[{len(s):,} characters]"
