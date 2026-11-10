"""Defensive validation and canonicalization for model-generated tool calls.

Malformed tool-call argument strings must never be copied verbatim into the next API
request. Some OpenAI-compatible servers reject a follow-up request with HTTP 400 when
an earlier assistant tool_calls.arguments value isn't valid JSON.
"""
from __future__ import annotations

import json
from contextlib import suppress
import difflib
import os
import re
import ntpath
from typing import Any

from .shelltool import NAME as SHELL_TOOL, POSIX
from .util import repair_json


def _name_key(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# Keys are _name_key() forms. Only aliases whose arguments match the target's (command, path, pattern).
_NAME_ALIASES = {
    **dict.fromkeys(("runcommand", "executecommand", "shell", "bash", "terminal", "runshell", "cmd",
                     "runterminalcmd", "exec") + (("powershell", "pwsh", "sh", "zsh") if POSIX else ()), SHELL_TOOL),
    **dict.fromkeys(("read", "readfiles", "view", "cat", "openfile"), "read_file"),
    **dict.fromkeys(("search", "searchfiles", "grepsearch", "ripgrep", "rg"), "grep"),
    **dict.fromkeys(("runpython", "python", "pythonexec", "executepython"), "py_run"),
}


# A call the server did not parse stays in the text in the chat template's own format (Qwen XML).
# Without recovery the loop takes that text for the final answer and the user gets raw XML.
_TEXT_CALL = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*(?:</tool_call>|$)", re.S)
_TEXT_PARAM = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
# Hermes JSON inside the same tags: the native format of Qwen2.5/Qwen3/Qwen3-Next and most fine-tunes.
_TEXT_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.S)


def _json_call(obj: Any) -> tuple[str, dict] | None:
    """{"name", "arguments"|"parameters"} or {"function": {...}} -> (name, args); None for other JSON."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name, args = obj.get("name"), obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        args = repair_json(args) if args.strip() else {}
    if not isinstance(name, str) or not name or not isinstance(args, dict):
        return None
    return name, args


def _xml_calls(content: str) -> list[tuple[str, dict]]:
    out = []
    for match in _TEXT_CALL.finditer(content):
        args: dict[str, Any] = {}
        for param in _TEXT_PARAM.finditer(match.group(2)):
            args[param.group(1)] = _param_value(param.group(2))
        out.append((match.group(1), args))
    return out


def _param_value(value: str) -> Any:
    """A parameter's text; JSON-looking values (patch blocks, flags) as the JSON they are."""
    if value.strip()[:1] in ("{", "[") or value.strip() in ("true", "false", "null"):
        with suppress(ValueError):
            return json.loads(value)
    return value


# The same call with the tool name lost: `<2>` or `<=pack>` where `<function=powershell>` belongs (live
# runs: a child took `<tool_call><2><parameter=command>...` for its report; the parent `<=pack>` for patch).
_GARBLED_CALL = re.compile(r"<tool_call>\s*<(?!function=|parameter=)([^<>\n]{0,40})>(.*?)</function>\s*(?:</tool_call>|$)", re.S)


def _garbled_calls(content: str, tool_specs: list[dict] | None) -> list[tuple[str, dict]]:
    """Calls whose name tag is garbled, named by what is left of the name or by the parameters given.
    Only an advertised tool counts, and only when at least one parameter was read: no guessing."""
    names = {s.get("function", {}).get("name") for s in tool_specs or []}
    out = []
    for match in _GARBLED_CALL.finditer(content):
        args: dict[str, Any] = {}
        for param in _TEXT_PARAM.finditer(match.group(2)):
            args[param.group(1)] = _param_value(param.group(2))
        raw = match.group(1).strip().lstrip("=").strip()
        name = canonical_tool_name(raw, tool_specs, args) if args else ""
        if name not in names:
            return []
        out.append((name, args))
    return out


def _bare_calls(content: str, tool_specs: list[dict] | None) -> list[tuple[str, dict]]:
    """A reply that is nothing but a call as JSON (bare or fenced). Only names the model was offered
    count, and only a reply with no prose around it: JSON the user asked for is never run."""
    if not tool_specs:
        return []
    text = content.strip()
    fenced = _FENCE.match(text)
    text = fenced.group(1).strip() if fenced else text
    if text[:1] not in ("{", "["):
        return []
    with suppress(ValueError):
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
        found = [_json_call(x) for x in items]
        names = {s.get("function", {}).get("name") for s in tool_specs}
        if found and all(f and canonical_tool_name(f[0], tool_specs) in names for f in found):
            return found  # type: ignore[return-value]
    return []


_BROKEN_CALL = re.compile(r"<tool_call\b|<function=|</function>|<parameter=", re.I)
BROKEN_CALL_FEEDBACK = ("<tool_validation_feedback>Your last reply contained a tool call that could not be read "
                        "(malformed tags or no tool name), so nothing ran. Make the call again as a normal tool call "
                        "with the tool name and every required argument. If you meant to answer, answer in plain "
                        "text without tool-call tags.</tool_validation_feedback>")


def broken_text_call(visible: str) -> bool:
    """A reply that still carries tool-call markup after text_tool_calls() found no call in it.

    Real X3 run: `<tool_call><=powerShell><parameter=command></function></tool_call>` came back as the
    "answer"; only the self-check request (another ~13 s) noticed it. The markup is the same in every
    language, so this is a structural check, not a phrase list.
    """
    return bool(_BROKEN_CALL.search(visible or ""))


def text_tool_calls(content: str, id_prefix: str = "textcall",
                    tool_specs: list[dict] | None = None) -> tuple[list[dict], str]:
    """Tool calls left in the reply text -> (OpenAI-shaped calls, the text without them).

    Qwen XML and Hermes JSON inside <tool_call> tags; with tool_specs also a reply that is only
    a JSON call. Cheap when there is nothing to find, so callers run it on every reply without calls.
    """
    content = content or ""
    found, pattern = _xml_calls(content), _TEXT_CALL
    if not found and tool_specs:
        found, pattern = _garbled_calls(content, tool_specs), _GARBLED_CALL
    if not found:
        found = [c for c in (_json_call(repair_json(m.group(1))) for m in _TEXT_JSON_CALL.finditer(content)) if c]
        pattern = _TEXT_JSON_CALL
    rest = pattern.sub("", content).strip() if found else content
    if not found:
        found = _bare_calls(content, tool_specs)
        rest = "" if found else content
    calls = [{"id": f"{id_prefix}_{i}", "type": "function",
              "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
             for i, (name, args) in enumerate(found)]
    return calls, rest


def _fits_arguments(args: Any, tool_specs: list[dict] | None) -> str | None:
    """The one advertised tool whose schema fits these argument names: every required field is given and
    every given field is its own. Two fitting tools (grep and glob both take pattern + path) -> None."""
    if not isinstance(args, dict) or not args:
        return None
    keys, fit = set(args), []
    for spec in tool_specs or []:
        function = spec.get("function", {})
        params = function.get("parameters", {})
        required = set(params.get("required", []))
        if required and required <= keys <= set(params.get("properties", {})):
            fit.append(function.get("name", ""))
    return fit[0] if len(fit) == 1 else None


def canonical_tool_name(name: str, tool_specs: list[dict] | None = None, args: Any = None) -> str:
    """Map near-miss names ("power_shell", "PowerShell", "Read_File") to the advertised tool name.

    Only case and separators are ignored, so the match is never a guess between two tools. With `args`,
    a name that is still unknown ("run", "match" with {"command": ...}; live runs repeated it 3 times
    after "unknown tool") goes to the one tool whose schema fits exactly those arguments.
    """
    names = [s.get("function", {}).get("name", "") for s in (tool_specs or [])]
    if not name or name in names or name.startswith("mcp__"):
        return name
    mapped = _canonical_by_name(name, names)
    if mapped != name:
        return mapped
    return _fits_arguments(args, tool_specs) or name


def _canonical_by_name(name: str, names: list[str]) -> str:
    # Names other agents' tools taught the model (Claude Code, Cline, Codex): one wasted round trip each.
    alias = _NAME_ALIASES.get(_name_key(name))
    if alias in names:
        return alias
    key = _name_key(name)
    hits = [n for n in names if _name_key(n) == key]
    if len(hits) == 1:
        return hits[0]
    # One-letter typos ("powersell", "ppowershell"): an "unknown tool" error cost a whole step. Only a
    # single advertised name one edit away, and not for short names where one letter is another tool.
    near = [n for n in names if len(key) >= 6 and _one_edit(key, _name_key(n))]
    if len(near) == 1:
        return near[0]
    # A name cut short ("powers" for powershell): the start of exactly one advertised name.
    starts = [n for n in names if len(key) >= 5 and _name_key(n).startswith(key)]
    return starts[0] if len(starts) == 1 else name


def _one_edit(a: str, b: str) -> bool:
    """a and b differ by one inserted, deleted or replaced character, or two swapped neighbours."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def name_did_you_mean(name: str, tool_specs: list[dict] | None = None) -> str | None:
    """Nearest advertised tool name for a name that canonical_tool_name could not map.

    Quantized models mix up edit_file/patch/apply_diff more often than they forget a field,
    so a near-miss name deserves a hint instead of a bare "unknown tool".
    """
    names = [s.get("function", {}).get("name", "") for s in (tool_specs or [])]
    if not name or not names:
        return None
    key = _name_key(name)
    scored = sorted(((difflib.SequenceMatcher(None, key, _name_key(n)).ratio(), n) for n in names),
                    key=lambda x: (-x[0], x[1]))
    best, hit = scored[0]
    return hit if best >= 0.6 and hit != name else None


def required_arguments(tool_name: str, tool_specs: list[dict] | None = None) -> list[str]:
    """Return required fields from the currently advertised tool schema."""
    for spec in tool_specs or []:
        function = spec.get("function", {})
        if function.get("name") == tool_name:
            return list(function.get("parameters", {}).get("required", []))
    return []


def validate_required_arguments(
    tool_name: str,
    args: dict[str, Any],
    tool_specs: list[dict] | None = None,
) -> str | None:
    """Report missing required args before any side effect or direct dict indexing."""
    required = required_arguments(tool_name, tool_specs)
    if not required:
        return None
    missing = [
        key for key in required
        if key not in args or args[key] is None or (isinstance(args[key], str) and not args[key].strip() and key != "content")
    ]
    if not missing:
        return None
    quoted = ", ".join(repr(key) for key in missing)
    return (
        f"Error: invalid tool call for {tool_name}: missing required argument(s) {quoted}. "
        f"No changes were made. Retry {tool_name} with every required field from its schema."
    )


def semantic_argument_problems(tool_name: str, args: dict[str, Any]) -> list[dict]:
    """Semantic problems that a required-field check cannot see.

    Returns [{field, why, fix}]. Only rules that are facts of the tool implementation:
    `end_line` cannot be before `start_line` (read_file silently returned an empty range
    before this check existed), `pattern` cannot be empty, `glob` cannot be whitespace.
    Values that the tool clamps on purpose (limit > 2000, glob cap of 100 paths) are not
    errors and must not be rejected here.
    """
    problems = []

    def add(field, why, fix):
        problems.append({"field": field, "why": why, "fix": fix})

    if tool_name == "read_file":
        start = args.get("start_line")
        end = args.get("end_line")
        if isinstance(start, int) and isinstance(end, int) and end < start:
            add("end_line", f"end_line {end} is before start_line {start}",
                "swap them, or drop end_line and use limit to page")
    if tool_name == "grep":
        pattern = args.get("pattern")
        if isinstance(pattern, str) and not pattern.strip():
            add("pattern", "pattern is empty", "give a regex or plain text to search for")
        glob = args.get("glob")
        if isinstance(glob, str) and glob and not glob.strip():
            add("glob", "glob contains only whitespace", "drop glob or give a file-name mask such as *.py")
    return problems


def format_problems(tool_name: str, problems: list[dict]) -> str:
    """Render the problem list as one short message for the model."""
    if not problems:
        return ""
    parts = [f"{p['field']}: {p['why']} ({p['fix']})" for p in problems]
    return (f"Error: invalid tool call for {tool_name}: " + "; ".join(parts) +
            ". No changes were made. Retry with arguments that match the tool schema.")


def unknown_tool_error(tool_name: str, tool_specs: list[dict] | None = None) -> str | None:
    """Return an actionable error for an unadvertised tool without guessing execution."""
    if not tool_specs:
        return None
    names = {s.get("function", {}).get("name", "") for s in tool_specs}
    if tool_name in names or tool_name.startswith("mcp__"):
        return None
    nearest = name_did_you_mean(tool_name, tool_specs)
    suffix = f" (did you mean {nearest}?)" if nearest else ""
    return f"Error: unknown tool {tool_name}{suffix}. No tool was run."


def validate_tool_arguments(tool_name: str, args: dict[str, Any],
                            tool_specs: list[dict] | None = None) -> str | None:
    """Run all side-effect-free validation in one place before permission/dispatch."""
    unknown = unknown_tool_error(tool_name, tool_specs)
    if unknown:
        return unknown
    # Prefer actionable semantic errors when a field is present but invalid (for example,
    # whitespace-only grep.pattern) over the generic missing-field message.
    semantic = semantic_argument_problems(tool_name, args)
    if semantic:
        return format_problems(tool_name, semantic)
    required = validate_required_arguments(tool_name, args, tool_specs)
    if required:
        return required
    return None


def normalization_notice(before: Any, after: Any) -> str:
    """Describe top-level argument changes so the model knows what was actually executed."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ""
    changed = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key, object()), after.get(key, object())
        if left == right and type(left) is type(right):
            continue
        if key not in before:
            changed.append(f"{key} added")
        elif key not in after:
            changed.append(f"{key} removed")
        else:
            old = repr(left)
            new = repr(right)
            if len(old) > 100:
                old = old[:97] + "..."
            if len(new) > 100:
                new = new[:97] + "..."
            changed.append(f"{key}: {old} -> {new}")
    return "[Arguments normalized: " + "; ".join(changed[:6]) + "]" if changed else ""


def bad_args_hint(raw: str | None, limit: int = 600) -> str:
    """Short preview of unparsable arguments so the model can see its own mistake."""
    t = (raw or "").strip()
    if not t:
        return "(empty arguments)"
    if len(t) > limit:
        t = t[: limit // 2] + " … " + t[-limit // 2:]
    return " ".join(t.splitlines())



_ALIASES = {
    "path": ("file_path",), "file_path": ("path",),
    "command": ("cmd",), "cmd": ("command",),
    "pattern": ("regex",), "regex": ("pattern",),
}


def _schema_for(tool_name: str, tool_specs: list[dict] | None) -> dict:
    for spec in tool_specs or []:
        function = spec.get("function", {})
        if function.get("name") == tool_name:
            return function.get("parameters", {}) or {}
    return {}


_MAX_SCHEMA_REPAIR_DEPTH = 6
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _json_type(value: Any) -> str:
    """Use JSON Schema's value categories (Python bool is not a JSON number)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "unknown"


def _resolve_local_ref(schema: dict, root: dict) -> dict | None:
    """Resolve only local, one-name $defs/definitions refs; never load remote schemas."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return None
    match = re.fullmatch(r"#/(\$defs|definitions)/([^/]+)", ref)
    if not match:
        return None
    table = root.get(match.group(1))
    if not isinstance(table, dict):
        return None
    name = match.group(2).replace("~1", "/").replace("~0", "~")
    target = table.get(name)
    return target if isinstance(target, dict) else None


def _decode_json_if_possible(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _repair_schema_value(value: Any, schema: dict, root: dict, depth: int = 0) -> Any:
    """Schema-guided repair modeled on OpenCode's tool-input-repair plugin.

    Repair is intentionally conservative: union/composition schemas are not guessed at,
    local references are depth-limited, and only clearly typed values are coerced.
    Maxedcode's local tool schemas treat undeclared keys as closed by default unless a
    schema explicitly permits additional keys, matching the CLI task's stricter contract.
    """
    if depth > _MAX_SCHEMA_REPAIR_DEPTH or not isinstance(schema, dict):
        return value

    ref_target = _resolve_local_ref(schema, root)
    if ref_target is not None:
        return _repair_schema_value(value, ref_target, root, depth + 1)

    wanted = schema.get("type")
    if isinstance(wanted, list):
        if value is None and "null" in wanted:
            return value
        if _json_type(value) in wanted:
            return value
        candidates = [item for item in wanted if item != "null"]
        return (_repair_schema_value(value, {**schema, "type": candidates[0]}, root, depth + 1)
                if len(candidates) == 1 else value)

    if wanted is None:
        # When both unions are declared, or a branch already accepts the current type,
        # the input is ambiguous and should be left alone rather than forced into a branch.
        if isinstance(schema.get("anyOf"), list) and isinstance(schema.get("oneOf"), list):
            return value
        branches = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else schema.get("oneOf")
        if not isinstance(branches, list) or value is None:
            return value
        value_type = _json_type(value)
        if any(not isinstance(branch, dict) or branch.get("type") == value_type for branch in branches):
            return value
        candidates = [branch for branch in branches if isinstance(branch, dict) and branch.get("type") != "null"]
        return _repair_schema_value(value, candidates[0], root, depth + 1) if len(candidates) == 1 else value

    if wanted in ("number", "integer"):
        if not isinstance(value, str) or not value.strip():
            return value
        try:
            number = float(value.strip())
        except (ValueError, OverflowError):
            return value
        if not (number == number and abs(number) != float("inf")):
            return value
        if wanted == "integer":
            if not number.is_integer() or abs(number) > _MAX_SAFE_INTEGER:
                return value
            return int(number)
        return int(number) if number.is_integer() and abs(number) <= _MAX_SAFE_INTEGER else number

    if wanted == "boolean":
        # Match OpenCode: don't guess variants such as "yes", "off" or 0/1.
        if value == "true":
            return True
        if value == "false":
            return False
        return value

    if wanted == "string":
        # Maxedcode extension: numeric scalar values are often emitted in the wrong JSON type
        # by small local models; preserve useful information by canonicalizing them as text.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value

    if wanted == "object":
        parsed = _decode_json_if_possible(value)
        if not isinstance(parsed, dict):
            return value
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        patterned = isinstance(schema.get("patternProperties"), dict)
        composed = any(isinstance(schema.get(key), list) for key in ("allOf", "anyOf", "oneOf"))
        additional = schema.get("additionalProperties", False)
        repaired = dict(parsed)
        for key, current in list(parsed.items()):
            declared = key in properties
            property_schema = properties.get(key) if declared else (None if patterned else additional)
            if (not declared and additional is False and not patterned and not composed):
                repaired.pop(key, None)
                continue
            if not isinstance(property_schema, dict):
                continue
            property_is_composed = any(keyword in property_schema for keyword in
                                       ("anyOf", "oneOf", "allOf", "enum", "const", "$ref", "nullable"))
            plain = (isinstance(property_schema.get("type"), str)
                     and property_schema.get("type") != "null"
                     and not composed and not property_is_composed)
            placeholder = isinstance(current, dict) and not current and property_schema.get("type") != "object"
            if declared and key not in required and plain and (current is None or placeholder):
                repaired.pop(key, None)
                continue
            new_value = _repair_schema_value(current, property_schema, root, depth + 1)
            if new_value != current or type(new_value) is not type(current):
                repaired[key] = new_value
        return repaired

    if wanted == "array":
        parsed = _decode_json_if_possible(value)
        prefix = schema.get("prefixItems")
        items = schema.get("items")
        legacy_tuple = isinstance(items, list)
        tuple_schemas = prefix if isinstance(prefix, list) else items if legacy_tuple else None
        if isinstance(parsed, list):
            fixed = []
            changed = False
            for index, item in enumerate(parsed):
                if tuple_schemas is not None:
                    member_schema = tuple_schemas[index] if index < len(tuple_schemas) else (
                        schema.get("items") if isinstance(prefix, list) else schema.get("additionalItems"))
                else:
                    member_schema = items
                new_item = _repair_schema_value(item, member_schema, root, depth + 1) if isinstance(member_schema, dict) else item
                fixed.append(new_item)
                changed = changed or new_item != item or type(new_item) is not type(item)
            return fixed if changed else parsed

        # Only promote a scalar to a singleton array when the item schema clearly accepts it.
        if tuple_schemas is not None or not isinstance(items, dict):
            return value
        fixed_item = _repair_schema_value(parsed, items, root, depth + 1)
        item_type = items.get("type")
        actual = _json_type(fixed_item)
        compatible = ((item_type == "object" and actual == "object")
                      or (item_type == "array" and actual == "array")
                      or (item_type == "integer" and actual == "number" and isinstance(fixed_item, int) and not isinstance(fixed_item, bool))
                      or (item_type == "number" and actual == "number" and isinstance(fixed_item, (int, float)) and not isinstance(fixed_item, bool))
                      or (item_type == actual and item_type in ("string", "boolean")))
        return [fixed_item] if compatible else value

    return value


def _coerce(value: Any, schema: dict) -> Any:
    """Backward-compatible wrapper around the recursive schema-aware repair."""
    return _repair_schema_value(value, schema, schema, 0)


def _object_from_list(value: list[Any]) -> dict[str, Any] | None:
    if len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    if value and all(isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str) for pair in value):
        try:
            return dict(value)
        except (ValueError, TypeError):
            return None
    return None


def _normalize_path(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    # A leaked tool-call markup tail ("...\\sandbox\n</", "</parameter>", "x.py<\\parameter><\\<\\function>")
    # is not part of the path: < and > cannot occur in a Windows file name.
    path = re.sub(r"\s*<[\\/<>A-Za-z_\s]*$", "", value)
    path = path.replace("\r", "").replace("\n", "").strip().strip('`').strip().strip('"').strip("'")
    if os.name == "nt":
        path = path.replace("/", "\\")
        # A single-rooted path has no drive; anchor it to the current drive without expanding ~.
        if path.startswith("\\") and not path.startswith("\\\\") and not re.match(r"^[A-Za-z]:", path):
            drive = ntpath.splitdrive(os.getcwd())[0]
            if drive:
                path = drive + path
        path = ntpath.normpath(path)
        if not ntpath.isabs(path):
            path = ntpath.abspath(path)
    elif re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
        # Keep explicit Windows paths usable when repairing a session on a non-Windows host.
        path = ntpath.normpath(path.replace("/", "\\"))
    else:
        path = os.path.abspath(os.path.normpath(path))
    return path


def repair_tool_arguments(tool_name: str, args: dict[str, Any], tool_specs: list[dict] | None = None) -> dict[str, Any]:
    """Repair aliases, types, unknown keys, and common path artifacts against the advertised schema."""
    schema = _schema_for(tool_name, tool_specs)
    if not schema:
        return dict(args)
    properties = schema.get("properties", {})
    value = dict(args)
    # Correct common model-generated aliases only when the schema has the destination field.
    for target, aliases in _ALIASES.items():
        if target in properties and target not in value:
            source = next((alias for alias in aliases if alias in value), None)
            if source is not None:
                value[target] = value[source]
        # Alias keys are only retained if they are real schema properties.
        for alias in aliases:
            if alias not in properties:
                value.pop(alias, None)
    # Tool arguments are top-level JSON objects. Pass the entire root schema so $defs and
    # definitions refs resolve just as they do in OpenCode's plugin.
    effective_schema = dict(schema)
    effective_schema.setdefault("additionalProperties", False)
    fixed = _repair_schema_value(value, effective_schema, effective_schema, 0)
    if not isinstance(fixed, dict):
        fixed = value
    for key in ("path", "file_path"):
        if key in fixed:
            fixed[key] = _normalize_path(fixed[key])
    for key, item in fixed.items():
        if isinstance(item, str) and key not in ("path", "file_path"):
            fixed[key] = _strip_call_tail(item)
    if tool_name == "ask_user":
        _clean_question(fixed)
    return fixed


# The closing tags of the call itself leaked into the end of a value (LCB abc396_d: a correct solution.py
# ended with "</parameter>\n</\nparameter>\n</invoke>\n</function>\n</tool_call>", SyntaxError, one more
# step to rewrite it). Only a run of two or more such closing tags at the very end is cut, so a file that
# merely ends with one XML tag is left alone.
_CALL_TAIL = re.compile(r"(?:\s*</\s*(?:parameter|invoke|function|tool_call|function_calls)\s*>){2,}\s*$", re.I)


def _strip_call_tail(value: str) -> str:
    # "</\nparameter>" (a broken tag) also matches: \s* after "</" spans the newline.
    if "</" not in value[-400:]:
        return value
    m = _CALL_TAIL.search(value, max(0, len(value) - 400))
    if not m:
        return value
    head = value[:m.start()]
    return head.rstrip() + ("\n" if value[m.start():].startswith(("\n", "\r\n")) or head.endswith("\n") else "")


# Tool-call markup the model leaked into the end of a text value: "Что сделать?\n</</option>".
_MARKUP_TAIL = re.compile(r"(?:\s*<[\\/]+(?:[A-Za-z_]+>)?|\s*<[\\/]*(?:parameter|function|options?|tool_call|arg_value|arg_key)\b[^<>\n]*>)+\s*$",
                          re.I)


def _clean_question(args: dict[str, Any]) -> None:
    """ask_user shown to the person must be readable: drop leaked markup and unpack an options list the model
    sent as one string ('["["A", "B", "C"]' — real session: the user got one garbage option)."""
    if isinstance(args.get("question"), str):
        args["question"] = _MARKUP_TAIL.sub("", args["question"])
    options = args.get("options")
    if not isinstance(options, list):
        return
    out = []
    for option in options:
        text = _MARKUP_TAIL.sub("", str(option)).strip()
        if text.startswith("[") and '"' in text:
            inner = re.sub(r'[\s\]"]+$', "", re.sub(r'^[\s\["]+', "", text))
            parts = [p.replace('\\"', '"').strip() for p in re.split(r'"\s*,\s*"', inner)]
            parts = [p for p in parts if p]
            if len(parts) > 1:
                out += parts
                continue
        out.append(text)
    args["options"] = [o for o in out if o]

# Tools whose half-sent text would act: repair_json closes an unterminated string, so a reply cut by the
# output limit wrote half a file (real session: a 10.7k-char write_file ended mid-row of a CSV) or would run
# half a command.
CUT_TOOLS = frozenset({"write_file", "edit_file", "patch", "apply_diff", SHELL_TOOL, "py_run"})


def _ends_inside_string(s: str) -> bool:
    in_str = esc = False
    for ch in s:
        if esc:
            esc = False
        elif ch == "\\" and in_str:
            esc = True
        elif ch == '"':
            in_str = not in_str
    return in_str


def cut_off_error(tool_name: str, raw: str | None, finish_reason: str | None = None) -> str | None:
    """An error when the arguments stop inside a string value or the server reported the reply cut by the
    output limit (it may close the JSON itself): nothing is run. Missing closing braces alone are still
    repaired; only an unterminated string means lost text."""
    if tool_name not in CUT_TOOLS:
        return None
    if finish_reason == "length":
        return _cut_message(tool_name, raw)
    if not raw:
        return None
    try:
        json.loads(raw)
        return None
    except (ValueError, TypeError):
        pass
    if not _ends_inside_string(raw):
        return None
    return _cut_message(tool_name, raw)


CUT_COUNT: dict[str, int] = {}   # cut calls per target file in this process: the hint gets firmer each time


def _cut_target(raw):
    m = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', str(raw or ""))
    return m.group(1) if m else ""


def _cut_message(tool_name: str, raw: str | None = None) -> str:
    target = _cut_target(raw)
    key = f"{tool_name}:{target.lower()}"
    CUT_COUNT[key] = n = CUT_COUNT.get(key, 0) + 1
    base = (f"Error: the arguments of {tool_name} were cut off inside a text value (the reply hit the output "
            f"limit); nothing was run, so no half-written file or half command.")
    if n == 1:
        return base + " Send the call again; split a large file into a first write_file and further edit_file/patch calls."
    where = f" for {target}" if target else ""
    if n == 2:
        # The same whole file again is the same thousands of generated tokens cut at the same place.
        return (base + f" This is the 2nd cut call{where}. Do not send the same content again: write a short "
                "skeleton first (at most ~150 lines), then add the rest in further edit_file/patch calls.")
    return (base + f" This is cut call #{n}{where}: do NOT send it whole again. Write at most ~100 lines per call, "
            "or say in the answer what is left to write.")


def normalize_tool_arguments(
    tool_name: str,
    raw_arguments: str | None,
    tool_specs: list[dict] | None = None,
    finish_reason: str | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Repair JSON and serialize a canonical safe value for the conversation history.

    Returns (parsed_arguments, canonical_json, error). On invalid JSON the canonical
    representation is `{}` rather than the raw malformed string, so the next request
    cannot fail just because an earlier tool call had broken JSON.
    """
    raw = raw_arguments or "{}"
    cut = cut_off_error(tool_name, raw, finish_reason)
    if cut:
        # The model's own bytes stay in the history when they parse (prefix cache); "{}" only for broken JSON.
        try:
            json.loads(raw)
            return {}, raw, cut
        except (ValueError, TypeError):
            return {}, "{}", cut
    parsed = repair_json(raw)
    if not isinstance(parsed, dict) and isinstance(parsed, list):
        candidate = _object_from_list(parsed)
        if isinstance(candidate, dict):
            parsed = candidate
    if not isinstance(parsed, dict):
        return {}, "{}", (
            f"Error: invalid JSON arguments for {tool_name}; they could not be repaired. "
            f"No tool was run. The arguments the model actually sent were: {bad_args_hint(raw)!r}. "
            "Retry with one JSON object matching the tool schema: every field must be a key in "
            "double quotes, and long file text must be a single JSON string with escaped quotes "
            "and \\n newlines (or split a large file into smaller write_file/edit_file calls)."
        )

    parsed = repair_tool_arguments(tool_name, parsed, tool_specs)
    canonical = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    error = validate_tool_arguments(tool_name, parsed, tool_specs)
    if _verbatim_ok(raw, parsed):
        canonical = raw
    return parsed, canonical, error


def _verbatim_ok(raw: str, parsed: Any) -> bool:
    """The model's own argument string can stay as it is: it already is the repaired value.

    Prefix-cache safety: the server re-renders the history and reuses its decoded reply only when that
    rendering continues it token for token (Strata: otherwise it falls back to the last <|im_start|> and
    reads the whole reply again). A re-dump of an unchanged value ('"a": 1' -> '"a":1') is such a change.
    A real repair still replaces the string: the same value is what runs.
    """
    try:
        return json.loads(raw) == parsed
    except (ValueError, TypeError):
        return False


def normalize_paste_text(raw: str) -> str:
    """Preserve long/multiline clipboard text while normalizing newline style."""
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


# Canned assistant lines the CLI used to write for rejected tool calls. They must not be sent
# back: a local model copies such a short assistant turn verbatim and answers with no tool call.
REJECTED_TOOLCALL_MARKERS = {
    "A malformed tool call was rejected before execution.",
    "[A malformed historical tool call was omitted before sending this request.]",
}


def sanitize_tool_history(messages: list[dict], tool_specs: list[dict] | None = None) -> list[dict]:
    """Copy history, canonicalize JSON, and remove broken tool-call/result pairs.

    Older sessions may contain an assistant tool call with missing required fields or
    syntactically invalid JSON. Some compatible APIs validate these historical calls
    and reject the *next* request with HTTP 400. We omit the invalid call and its result
    as a pair so the request remains a valid conversation.
    """
    cleaned: list[dict] = []
    invalid_call_ids: set[str] = set()
    for message in messages:
        item = dict(message)
        calls = item.get("tool_calls")
        if item.get("role") == "assistant" and not calls:
            text = (item.get("content") or "").strip()
            if not text or text in REJECTED_TOOLCALL_MARKERS:
                continue
        if item.get("role") == "assistant" and isinstance(calls, list):
            normalized_calls = []
            for call in calls:
                call_copy = dict(call)
                call_id = str(call_copy.get("id") or "")
                fn = dict(call_copy.get("function") or {})
                raw = fn.get("arguments") or "{}"
                parsed = repair_json(raw)
                name = str(fn.get("name") or "unknown")
                if isinstance(parsed, list):
                    parsed = _object_from_list(parsed)
                if isinstance(parsed, dict):
                    parsed = repair_tool_arguments(name, parsed, tool_specs)
                if not isinstance(parsed, dict) or validate_required_arguments(name, parsed, tool_specs):
                    if call_id:
                        invalid_call_ids.add(call_id)
                    continue
                # Prefix-cache safety: an argument string the server already saw must be sent back
                # byte-identical (see _verbatim_ok). Only a string that is not a JSON object is replaced.
                canonical = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                fn["arguments"] = raw if _verbatim_ok(raw, parsed) else canonical
                call_copy["function"] = fn
                normalized_calls.append(call_copy)
            if normalized_calls:
                item["tool_calls"] = normalized_calls
            elif (item.get("content") or "").strip():
                item.pop("tool_calls", None)
            else:
                # Keep history structurally valid while making the omitted legacy call explicit.
                # Its tool result is removed below, so the API never receives an orphan result.
                item.pop("tool_calls", None)
                item["content"] = "[A malformed historical tool call was omitted before sending this request.]"
        cleaned.append(item)

    # Do not send orphan tool results for calls omitted above.
    if invalid_call_ids:
        cleaned = [
            item for item in cleaned
            if not (item.get("role") == "tool" and item.get("tool_call_id") in invalid_call_ids)
        ]
    return cleaned
