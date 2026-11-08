"""The window's "Settings -> CLI commands" page: what the CLI's slash commands show or change, on the same files.

    python -m maxedcode.appcli get [cwd]          JSON: rules, MCP servers, hooks, memory, skills, commands, agents, tools
    python -m maxedcode.appcli set [cwd] < JSON    one action, then the fresh page data

/permissions /allow /deny -> permissions.json; /mcp add|remove -> mcp.json; /hooks -> settings.json (shown only);
/memory -> the files every session loads (+ a project note); /skill /commands /agents -> their folders (shown);
/tools -> the catalog in the prompt prefix (shown). Run in a fresh process, like appsettings: config is read at import.
"""
import json
import re
import sys
from contextlib import suppress
from pathlib import Path

from . import config, hooks, memory, permissions, skills
from .config import MCP_FILE, PERMS_FILE

KINDS = ("allow", "deny", "ask")


def _read(path, default):
    with suppress(OSError, ValueError):
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def _write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rules():
    """Every rule in force; `own` marks the ones in permissions.json (the page edits those; the ones from
    settings.json are shown as they are)."""
    file = _read(PERMS_FILE, {})
    out = {k: [] for k in KINDS}
    for k, values in permissions.load().items():
        mine = [str(x) for x in (file.get(k) or [])]
        for rule in values:
            if not any(r["rule"] == rule for r in out.setdefault(k, [])):
                out[k].append({"rule": rule, "own": rule in mine})
    return out


def mcp_servers():
    servers = (_read(MCP_FILE, {}) or {}).get("mcpServers", {}) or {}
    return [{"name": n, "url": c.get("url") or "", "command": " ".join([str(c.get("command") or "")] + [str(a) for a in c.get("args") or []]).strip()}
            for n, c in servers.items() if isinstance(c, dict)]


def hook_list():
    out = []
    for event in hooks.EVENTS:
        for h in (config.SETTINGS.get("hooks") or {}).get(event) or []:
            cmds = [x.get("command") for x in h.get("hooks") or [] if isinstance(x, dict)] or [h.get("command")]
            out.append({"event": event, "matcher": h.get("matcher") or "", "command": "; ".join(str(c) for c in cmds if c)})
    return out


def tool_list():
    from .tools.catalog import session_catalog
    return [{"name": s["function"]["name"], "description": str(s["function"].get("description") or "").split("\n")[0][:240]}
            for s in session_catalog(None, include_search=True)]


def get(cwd=None):
    cwd = str(cwd or Path.home())
    return {
        "rules": rules(),
        "mcp": mcp_servers(),
        "hooks": hook_list(),
        "memory": [{"label": label, "path": str(f), "lines": n} for label, f, n in memory.loaded(cwd)],
        "skills": [{"name": s["name"], "description": s["description"], "path": str(s["path"])} for s in skills.skills()],
        "commands": [{"name": n, "description": (fm or {}).get("description") or "", "path": str(f)}
                     for n, (fm, _body, f) in sorted(skills.commands().items())],
        "agents": [{"name": a["name"], "description": a.get("description") or "", "path": str(a.get("path") or "")}
                   for a in skills.agents().values()],
        "tools": tool_list(),
        "files": {"rules": str(PERMS_FILE), "mcp": str(MCP_FILE), "settings": str(config.HOME / "settings.json")},
        "cwd": cwd,
    }


def apply(action, cwd=None):
    """One change from the page. ValueError: nothing was written."""
    kind = action.get("do")
    if kind in ("rule-add", "rule-remove"):
        k, rule = action.get("kind"), " ".join(str(action.get("rule") or "").split())
        if k not in KINDS or not rule:
            raise ValueError("a rule needs allow|deny|ask and a tool, e.g. powershell(git *)")
        data = _read(PERMS_FILE, {}) or {}
        values = [str(x) for x in data.get(k) or []]
        if kind == "rule-add" and rule not in values:
            values.append(rule)
        if kind == "rule-remove":
            values = [x for x in values if x != rule]
        data[k] = values
        _write(PERMS_FILE, {x: data.get(x) or [] for x in KINDS})
    elif kind in ("mcp-add", "mcp-remove"):
        data = _read(MCP_FILE, {}) or {}
        servers = data.get("mcpServers") or {}
        name = re.sub(r"[^A-Za-z0-9-]", "-", str(action.get("name") or "").strip())
        if not name:
            raise ValueError("an MCP server needs a name")
        if kind == "mcp-add":
            target = str(action.get("command") or "").strip()
            if not target:
                raise ValueError("an MCP server needs a command or an https:// address")
            if target.startswith("http"):
                servers[name] = {"url": target}
            else:
                import shlex
                parts = [x.strip('"') for x in shlex.split(target, posix=False)]
                servers[name] = {"command": parts[0], "args": parts[1:]}
        else:       # the name as it is in mcp.json: a hand-written "brave_search" was rewritten and never removed
            servers.pop(str(action.get("name") or "").strip(), None) or servers.pop(name, None)
        data["mcpServers"] = servers
        _write(MCP_FILE, data)
    elif kind == "note":
        text = str(action.get("text") or "").strip()
        if not text:
            raise ValueError("an empty note")
        memory.note(text, "project", scope="project", cwd=cwd)
    else:
        raise ValueError(f"unknown action: {kind}")


def expand(name, args=""):
    """A "/name args" typed in the window: a custom command's text or a skill wrapped as the TUI sends it."""
    fm, body = skills.command_text(name, args)
    if body:
        return {"text": body}
    x, body = skills.skill(name, args)
    if body:
        return {"text": f'<skill name="{x["name"]}" loaded_by="user">\n{body}\n</skill>'}
    return {"error": f"no command or skill named {name}"}


def main(argv):
    command = argv[1] if len(argv) > 1 else "get"
    cwd = argv[2] if len(argv) > 2 else None
    if command == "expand":
        data = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(expand(str(data.get("name") or ""), str(data.get("args") or "")), ensure_ascii=False))
        return 0
    if command == "set":
        try:
            apply(json.loads(sys.stdin.read() or "{}"), cwd)
        except (ValueError, OSError) as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
    print(json.dumps(get(cwd), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
