"""Stable built-in tool catalog shared by the TUI and headless agent.

Tool-call APIs still require valid JSON parameter schemas, so compact mode shortens natural-
language metadata while retaining the typed properties and required fields. The builtin
ordering is fixed; plan/act/auto are enforced at execution time rather than by changing this
prefix.
"""
from copy import deepcopy
import os
from .. import config
from ..config import COMPACT_CATALOG
from .schemas import BUILTIN

_SHORT = {
    "powershell": "Run a PowerShell command; use run_in_background for long jobs.",
    "bash": "Run a bash command; use run_in_background for long jobs.",
    "read_file": "Read a file or folder tree; use start_line/end_line or offset/limit to page. An image (png/jpg) is shown to you as a picture.",
    "edit_file": "Replace one exact existing text fragment.",
    "patch": "Apply multiple exact replacements to one file atomically.",
    "apply_diff": "Apply a unified diff to one existing file.",
    "grep": "Search files; returns matching paths, line numbers and short previews.",
    "glob": "List paths matching a glob; output is capped at 100 paths.",
    "write_file": "Write a text file, creating parent folders if needed.",
    "py_run": "Run Python code in a separate process (60 second limit).",
    "tool_search": "Load an MCP tool schema by its exact advertised name.",
    "py_source": "Show source code for a Python symbol.",
    "web_search": "Search the web for titles, links and snippets.",
    "fetch_url": "Fetch a web page and return readable text.",
    "ask_user": "Ask the user a concise question with optional choices.",
    "browser": "Do a task in the user's own browser (logins, tabs); a browser helper does it and reports. Helper: action=... steps.",
    "subagent": "Run a focused read-only investigation; background=true returns an id immediately.",
    "cloud_code": "Cloud model writes one specified piece of code from a spec (never project code); action=list to pick the model, note to save what a model is good for; every draft is approved.",
    "bg_output": "Read a background job's output; wait=N blocks until it ends (max N s).",
    "bg_kill": "Stop a background job.",
    "todo_write": "Create or update the dynamic task checklist for substantial work.",
    "remember": "Save a memory note for future sessions.",
    "skill": "Load the complete text of a named skill.",
    "exit_plan_mode": "Present a plan and request approval before implementation.",
    "code_mode": "Run restricted async Python orchestration; external actions must use tools.<name>(...).",
    "finish": "Finish the current turn with a final response.",
}


def builtin_catalog(include_search=True):
    result = deepcopy(BUILTIN)
    if not include_search:
        result = [spec for spec in result if spec.get("function", {}).get("name") != "tool_search"]
    if not config.CODE_MODE:
        result = [spec for spec in result if spec.get("function", {}).get("name") != "code_mode"]
    if not config.BROWSER_TOOL:
        result = [spec for spec in result if spec.get("function", {}).get("name") != "browser"]
    # Stable outer schema order is part of the cacheable system+tools prefix.
    result.sort(key=lambda spec: spec.get("function", {}).get("name", ""))
    if COMPACT_CATALOG:
        for spec in result:
            function = spec.get("function", {})
            name = function.get("name", "")
            if name in _SHORT:
                function["description"] = _SHORT[name]
            params = function.get("parameters", {}).get("properties", {})
            # Keep required/type/enum metadata; descriptions are redundant with the signature.
            for prop in params.values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = prop["description"].split(". ", 1)[0].rstrip(".")
    return result


def compact_tool_catalog(specs, excluded=None):
    """Render the small, stable signature inventory consumed by CodeMode.

    OpenCode's CodeMode gets compact signatures separately from the full outer JSON
    schemas. The Python port keeps those JSON schemas for API compatibility, while this
    inventory gives the model exact inner-tool names and argument keys without repeating
    long natural-language schema descriptions.
    """
    excluded = set(excluded or {"code_mode", "finish", "exit_plan_mode", "tool_search"})
    lines = []
    for spec in sorted(specs, key=lambda x: x.get("function", {}).get("name", "")):
        fn = spec.get("function", {})
        name = fn.get("name", "")
        if not name or name in excluded:
            continue
        params = fn.get("parameters", {}) or {}
        properties = params.get("properties", {}) or {}
        required = set(params.get("required", []) or [])
        args = []
        for key, schema in properties.items():
            if not isinstance(schema, dict):
                schema = {}
            typ = schema.get("type", "any")
            if isinstance(typ, list):
                typ = "|".join(map(str, typ))
            if typ == "array":
                item_type = (schema.get("items") or {}).get("type", "any")
                typ = f"{item_type}[]"
            if "enum" in schema:
                enum = "/".join(str(x) for x in schema["enum"][:5])
                typ = f"{typ}:{enum}"
            args.append(f"{key}{'!' if key in required else ''}:{typ}")
        description = " ".join(str(fn.get("description", "")).split())
        if ". " in description:
            description = description.split(". ", 1)[0]
        if len(description) > 115:
            description = description[:112].rstrip() + "..."
        suffix = f" — {description}" if description else ""
        lines.append(f"- tools.{name}({', '.join(args)}){suffix}")
    return "\n".join(lines)


# Optional tools a user can switch off: env first, then settings.json. Both default to on, so the TUI,
# headless runs and the desktop window send the same tool list: the list is rendered into the system
# turn, and a different one there made the server re-read the whole prompt on every CLI <-> window switch.
OPTIONAL_TOOLS = {
    "subagent": ("AGENT_ENABLE_SUBAGENT", "enable_subagent_tool"),
    "remember": ("AGENT_ENABLE_REMEMBER", "enable_remember_tool"),
}


def disabled_tools():
    """Names of optional tools explicitly switched off (fixed for the process, so the prefix stays stable)."""
    off = set()
    for name, (env_name, setting_name) in OPTIONAL_TOOLS.items():
        value = os.environ.get(env_name)
        if value is None:
            value = config.SETTINGS.get(setting_name)
        if value is not None and str(value).strip().lower() in {"0", "false", "no", "off"}:
            off.add(name)
    return off


def session_catalog(mcp=None, allowed=None, include_search=True):
    result = builtin_catalog(include_search=include_search)
    off = disabled_tools() - set(allowed or ())
    if off:
        result = [spec for spec in result if spec.get("function", {}).get("name") not in off]
    if mcp is not None:
        extra = mcp.specs() if callable(getattr(mcp, "specs", None)) else []
        result.extend(sorted(deepcopy(extra), key=lambda x: x.get("function", {}).get("name", "")))
    if allowed:
        result = [spec for spec in result if spec.get("function", {}).get("name") in allowed]
    result.sort(key=lambda spec: spec.get("function", {}).get("name", ""))
    # Keep the outer tool list order fixed. Only the CodeMode description includes this
    # compact inner inventory, matching OpenCode's separation of compact catalog/full schema.
    signatures = compact_tool_catalog(result)
    for spec in result:
        fn = spec.get("function", {})
        if fn.get("name") == "code_mode":
            fn["description"] = (str(fn.get("description", "")).rstrip() +
                                  "\n\nAvailable inner tools and exact arguments:\n" + signatures)
    return result
