"""Runtime tool routing and safety helpers. Tool schemas live in schemas.py.

Tool implementations live in fs.py, runners.py, mcp.py, and the related modules.
This module exports the schema registry for existing callers while keeping routing logic executable.
"""
import re

from .schemas import BUILTIN, READONLY, PARALLEL_READS, EDIT_TOOLS
from ..tool import ToolRegistry

# One immutable metadata view, built in stable name order. Runtime dispatch remains explicit.
_REGISTRY = ToolRegistry(BUILTIN, parallel_names=PARALLEL_READS, read_names=READONLY,
                         writer_names=EDIT_TOOLS)


# plan mode: powershell/py_run are allowed only if they do not touch the disk
WRITE_PS = re.compile(r"(?i)(?:(?<![=\-])>>?(?!&)(?!\s*(?:\$null|nul\b))|\bOut-File\b|\bSet-Content\b|\bAdd-Content\b|\bRemove-Item\b|\bNew-Item\b|\bMove-Item\b|\bCopy-Item\b|\bClear-Content\b|\brm\b|\bdel\b|\bdelitem\b|\binstall\b|\bgit\b\s+(commit|push|pull|checkout|reset|clean|merge|stash)|\btaskkill\b|\breboot\b)")
WRITE_PY = re.compile(r"(?i)(?:write_text|write_bytes|\.unlink\(|\.rename\(|\bshutil\b|\bos\.(remove|replace|rename|mkdir|unlink|system)|\.touch\(|open\([^)]*\b[2wa]\+?['\"]|\bsubprocess\b|\bpip install\b|>>)")
# read-only shell commands: no confirmation needed, the agent runs them right away
PS_RO = r"(?:Get-ChildItem|ls|dir|cat|type|Get-Content|Select-String|grep|rg|head|tail|test|pwd|which|where|whoami|echo|Get-Command|git\s+(?:status|log|diff|show|blame|remote\s+geturl))"
def ps_readonly(cmd):
    """Conservative check: only a single plain read command is safe to run concurrently."""
    source = (cmd or "").strip()
    if not source:
        return False
    # A read-only-looking first command can pipe into a mutator (e.g. Get-Content | iex),
    # redirect output, or evaluate a subexpression. Treat shell syntax as not parallel-safe.
    if any(ch in source for ch in ("|", ";", "&", ">", "<", "$", "`", "\n", "\r", "(", ")", "{" , "}")):
        return False
    return bool(re.match(PS_RO, source) and not WRITE_PS.search(source))

def parallel_safe(name, args):
    """Whether a tool call is safe to run concurrently with another model-issued call."""
    args = args if isinstance(args, dict) else {}
    metadata = _REGISTRY.get(name)
    if metadata is not None and metadata.parallel:
        return True
    # A narrowly recognized read-only PowerShell command is okay; arbitrary shell/Python is not.
    return name == "powershell" and ps_readonly(args.get("command") or "")


# A file named in the request ("calc.py", ".env", "C:\work"): a structural hint, the same in every language.
_NAMED_FILE_HINT = re.compile(r"(?i)\b[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|ya?ml|toml|ini|cfg|md|txt|csv|tsv|log|env|bat|ps1|sh|html|css)\b")
_DOTFILE_HINT = re.compile(r"(?i)(?:^|[\s`\"'])\.[\w-]+")
_DRIVE_PATH_HINT = re.compile(r"[A-Za-z]:[\\/]")


def _request_text(prompt):
    text = str(prompt or "").strip()
    return re.sub(r"</?(?:user_input|mode_notice)[^>]*>", " ", text).strip()


def prompt_needs_tools(prompt):
    """Whether the request may use tools: any non-empty request may.

    The user can write in any language, so the intent is not guessed from words here (word lists only
    ever covered English and Russian). The model decides; the system prompt says plain conversation
    needs no tools."""
    return bool(_request_text(prompt))


def last_assistant_has_tool_calls(messages):
    """Whether the latest assistant message is an unfinished tool-call step."""
    last = next((m for m in reversed(messages or []) if m.get("role") == "assistant"), None)
    return bool(last and last.get("tool_calls"))


def tool_request_policy(prompt, messages, hook_context="", schemas_enabled=False, mode=""):
    """Return (keep_schema_prefix, allow_tool_calls) for this request.

    The tool catalog is part of the cached prompt prefix and must be sent in every request of a
    session, starting with the first one. Sending tools=[] for a conversational answer and then
    the catalog on the next request (or on a switch to plan mode) changes the leading bytes of the
    prompt, so llama-server falls back to re-processing the whole conversation instead of reusing
    the slot's KV cache. tool_choice is not prompt content, so only that is allowed to change.

    schemas_enabled is kept for call compatibility; the prefix decision no longer depends on it.
    """
    allow_calls = bool(prompt_needs_tools(prompt) or last_assistant_has_tool_calls(messages)
                       or str(hook_context or "").strip() or str(mode or "").lower() == "plan")
    return True, allow_calls


def prompt_needs_repo_map(prompt):
    """Build the repo-wide AST map unless the request names its target file: a named file is cheaper
    and more accurate to inspect directly. No word lists, so it works the same in every language."""
    text = _request_text(prompt)
    if not text:
        return False
    return not (_NAMED_FILE_HINT.search(text) or _DOTFILE_HINT.search(text) or _DRIVE_PATH_HINT.search(text))
