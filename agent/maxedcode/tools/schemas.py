"""Model-facing JSON schemas for the built-in tools.

Descriptions in these schemas are intentional natural-language metadata sent to the model;
the executable tool handlers are implemented in sibling modules.
"""
from ..util import T, s
from ..config import FINISH_TOOL_ENABLED
from ..shelltool import POSIX
from .todo import STATUS


def _shell_params():
    return {"command": s("Command"),
            "timeout": {"type": "integer", "description": "seconds, default 120 (600 for git clone/pip/npm install/downloads), max 600; a command still running then continues as a background task"},
            "run_in_background": {"type": "boolean", "description": "start without waiting, return the task id right away (long-running process)"}}


POWERSHELL_SPEC = T(
    "powershell", "Run a Windows PowerShell 5.1 command or multi-line script (not bash). It must finish in seconds: "
    "do not use -Recurse over the whole profile or disk, search in specific folders. Non-interactive only "
    "(no Read-Host/pause/editors). Unknown cmdlets/parameters and syntax errors are rejected before anything runs",
    _shell_params(), ["command"])
# Linux/macOS: the same tool and arguments under the shell's own name (see shelltool.py).
BASH_SPEC = T(
    "bash", "Run a bash command or multi-line script on this Linux/macOS machine (not PowerShell). It must finish in "
    "seconds: do not search the whole home folder or disk (find / or grep -r ~), search in specific folders. "
    "Non-interactive only (no read prompts, sudo passwords, editors or pagers; stdin is empty unless you pipe into "
    "a command). Syntax errors are rejected before anything runs",
    _shell_params(), ["command"])

BUILTIN = [
    BASH_SPEC if POSIX else POWERSHELL_SPEC,
    T("read_file", "Read a text file by line range (default first 2000 lines; use offset/limit or start_line/end_line for paging). Large outputs are saved to temporary storage and only a preview is returned. Large .log/.csv/.tsv/.json/.jsonl/.ndjson files over 24 KB are not read into context unless a line range is explicit; use py_run for counts/summaries. For a large source file, locate the needed place with grep. "
      "A folder path returns its file tree plus small source files in one call. An image file (png/jpg/webp) is shown to you as a picture. Project instruction files "
      "(CLAUDE.md/AGENTS.md) and folder docs are already in the context — do not re-read them",
      {"path": s("Absolute path of a file or folder"), "start_line": {"type": "integer", "description": "From which line, default 1"},
       "end_line": {"type": "integer", "description": "Up to which line, inclusive"},
       "offset": {"type": "integer", "description": "Zero-based line offset, used when start_line is omitted"},
       "limit": {"type": "integer", "description": "Maximum lines to return, capped at 2000"}}, ["path"]),
    T("edit_file", "Replace a single exact fragment in an existing file. Exact old_text must match once. Do not change unrelated blocks; after two failures switch to write_file (whole file) or apply_diff. For several nearby changes use one patch call.",
      {"path": s("Absolute path"), "old_text": s("What to replace"), "new_text": s("Replace it with")}),
    T("patch", "Apply several exact search/replace blocks to ONE existing file in a single call. Each old_text must occur exactly once; all blocks are validated before writing and one /undo restores the whole patch. Prefer this over repeated edit_file calls for multiple nearby changes. Change only the blocks requested; preserve unrelated code.",
      {"path": s("Absolute path"), "blocks": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
          "old_text": s("Exact existing fragment; must occur exactly once"), "new_text": s("Replacement text")}, "required": ["old_text", "new_text"]}}}, ["path", "blocks"]),
    T("apply_diff", "Apply a standard unified diff to ONE existing file. Use ---/+++ file headers and @@ hunk headers; the patch is applied only when context matches unambiguously. Fallback for repeated edit_file exact-match failures. Do not change unrelated blocks.",
      {"path": s("Absolute path of the existing file"), "diff": s("Standard unified diff text with @@ hunks")}, ["path", "diff"]),
    T("grep", "Search a narrow project path; for code use glob=*.py. Returns at most 100 matches with a short preview line, not full matching files. Set context=3-5 when inspecting a function. Avoid searching logs/data unless explicitly requested.",
      {"pattern": s("Regex or plain text"), "path": s("File or folder, absolute path"),
       "glob": s("File name mask, e.g. *.py (optional)"),
       "context": {"type": "integer", "description": "lines before/after each match. Defaults to 3 for source-code searches and 0 otherwise"},
       "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                       "description": "content (default) = file:line: text. files_with_matches = only paths (use it to find where something is defined, then read that file). count = file: N matches, for a big search instead of hundreds of lines"}},
      ["pattern", "path"]),
    T("glob", "List file paths matching a glob pattern under a directory. Returns at most 100 paths; use read_file or grep to inspect content.",
      {"pattern": s("Glob pattern, e.g. **/*.py or src/*.ts"), "path": s("Root directory, default current working directory")}, ["pattern"]),
    T("write_file", "Create a new text file, or fully rewrite a small one, with its complete content in this one call (no placeholders). Change an existing file with patch/edit_file instead.",{"path": s("Absolute path"), "content": s("Content")}),
    T("py_run", "Run Python code and return stdout+stderr. After 60 s a still running snippet continues as a background task (bg_output / bg_kill) if it printed something; a silent one is stopped. For tests and quick checks instead of write_file + " + ("bash" if POSIX else "powershell"),
      {"code": s("Python code")}),
    T("tool_search", "Load the full schema for one deferred MCP tool by its exact name. Use this when the desired MCP tool name appears in the available names listed here; after the schema is returned, call that MCP tool in the next model turn.",
      {"name": s("Exact full MCP tool name, e.g. mcp__server__tool")}, ["name"]),
    T("py_source", "Show the real source of a Python object with file:line, e.g. textual.screen.Screen.get_selected_text",
      {"symbol": s("Dotted path: module.Class.method"), "max_lines": {"type": "integer", "description": "default 80"}}, ["symbol"]),
    T("web_search", "Search the internet: titles, links, descriptions. Use it first for anything outside this PC "
      "(new models/programs/versions, downloads, docs, news). For AI models also fetch_url "
      "https://huggingface.co/api/models?search=NAME&sort=downloads&limit=10", {"query": s("Query")}),
    T("fetch_url", "Download a web page and return its text", {"url": s("Link")}),
    T("browser", "Work in the user's own browser (Chrome/Edge with their logins and open tabs). One step: `action` "
      "(read/screenshot/click/type/key/scroll/open/tabs/switch/back). A long job across several pages or sites (search "
      "and compare, go through many results): `task` instead, done by a browser helper in its own context that "
      "reports back and keeps this chat small. Use it when the job needs the user's browser or a logged-in site; for "
      "public pages fetch_url is faster",
      {"task": s("What to do or find in the browser, with every detail needed (sites, values to enter, what to report)"),
       "action": {"type": "string", "enum": ["read", "screenshot", "click", "type", "key", "scroll", "open", "tabs", "switch", "back"],
                  "description": "Helper only: one step"},
       "url": s("open: address"), "ref": {"type": "integer", "description": "click/type/key/scroll: element number [N] from the last read; type/key without it go where the cursor is"},
       "text": s("type: text that replaces the field's value"), "submit": {"type": "boolean", "description": "type: press Enter after"},
       "keys": s("key: e.g. Enter, Escape, Control+a"), "tab": {"type": "integer", "description": "switch: tab id from tabs"},
       "new_tab": {"type": "boolean", "description": "open: in another new tab"},
       "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "description": "scroll"},
       "start": {"type": "integer", "description": "read: continue a long page from this character"},
       "find": s("read: only the lines that contain this text (a long page)")}, []),
    T("ask_user", "Ask the user a question with answer options when a choice between paths is needed or data is missing",
      {"question": s("Question"), "options": {"type": "array", "items": {"type": "string"}, "description": "2-5 options"}},
      ["question"]),
    T("cloud_code", "Ask a cloud model to write one specific piece of code: a function, a class, or one whole new file, from a written specification. Privacy comes first: the cloud never sees the project. Describe what to build in words (purpose, inputs and outputs, names and signatures it must use, the language and libraries, edge cases); never paste local file contents or code from the repository, paths, logs, environment, chat history, secrets or credentials. Ask for the smallest part that needs the stronger model and integrate it yourself. The exact outgoing task is shown to the user for approval; the cloud gets no tools and no previous messages and returns a draft you must review and test. Several cloud models can be configured: action=list shows them with what each is good for; pass model to choose one (omit it for the default). A model without a note: web_search what it is good for, then save one line with action=note. Subagents may use it only when the configuration allows it; every draft still needs approval.",
      {"action": {"type": "string", "enum": ["draft", "list", "note"],
                  "description": "draft (default): send task; list: the configured models and their notes (nothing is sent); note: save what model is good for"},
       "task": {"type": "string", "maxLength": 5000,
                "description": "draft: a standalone specification of the code to write. No source files, logs, local paths, private project data or credentials."},
       "model": s("draft: model id from action=list (omit for the default); note: the model the note is about"),
       "note": s("note: one line on what the model is good for (speed, coding, reasoning, context, price)")}, []),
    T("subagent", "Delegate work to persistent child sessions. Use prompt for one read-only investigation, or tasks with 2–6 ordered, self-contained implementation tasks. A tasks call creates a batch but runs ONLY the first child; it returns a batch_id and short report. The parent must read that report and decide whether to call subagent(batch_id=...) to run exactly the next queued task. That next call runs one child only and returns its report before any later child starts, so the parent can adapt or stop. Each child has its own visible/saved session and isolated working history, reuses this session's model, prefix and exact tool schemas, and never runs concurrently with parent/child inference. Task-list agents may edit files only through permission-checked tools; scope changes, no destructive commands, no nested/background agents. Reports must name changed paths and targeted checks. Parent verifies those paths/checks without rereading whole files or logs.",
      {"prompt": s("One self-contained, read-only task; use this OR tasks/batch_id"),
       "tasks": {"type": "array", "minItems": 2, "maxItems": 6, "items": {"type": "string", "minLength": 1},
                 "description": "Start a sequential batch of 2–6 ordered implementation tasks. Only task 1 runs in this call; after reading the report, continue with the returned batch_id if still needed."},
       "batch_id": s("Continue an existing batch; runs only its next queued task. Use the batch_id returned by the previous subagent report."),
       "report_chars": {"type": "integer", "minimum": 200, "maximum": 10000, "description": "Maximum child report length in characters; default 1500"},
       "agent": s("Optional additional task instructions from .maxedcode/agents; shared-prefix mode still uses parent model and schemas"),
       "background": {"type": "boolean", "description": "Deprecated. Must be false: delegated inference is always sequential to protect local GPU/RAM capacity."}}, []),
    T("code_mode", "Run a short Python orchestration script in a restricted namespace. Call tools.<name>(...) and await results; run independent calls via asyncio.gather. Imports, def, while, comprehensions, direct filesystem access, subprocesses, and timers are unavailable: it is not a Python runner, run code and tests with py_run. Returns one compact aggregate result.",
      {"code": s("Python orchestration snippet; tool calls are async and should be awaited")}, ["code"]),
    T("bg_output", "Output of a background task started with run_in_background. To wait for it, pass wait (seconds): "
      "it returns the moment the task ends. Never wait with " + ("sleep" if POSIX else "Start-Sleep"),
      {"id": s("Task id"), "wait": {"type": "integer", "description": "seconds to wait for the task to finish, max 600"}}, ["id"]),
    T("bg_kill", "Stop a background task by id", {"id": s("Task id")}, ["id"]),
    T("todo_write", "Create or update a concise checklist for the CURRENT user request when work is substantial (3+ distinct kinds of work). Write every step in the USER'S LANGUAGE (the language of their request), with as many steps as the work needs. Do not use for explanations, conversational Q&A, or read-only tasks. Mark done only after completion; update only when the stage changes.",
      {"todos": {"type": "array", "items": {"type": "object",
       "properties": {"content": {"type": "string"},
                      "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
       "required": ["content", "status"]}}}, ["todos"]),
    T("remember", "Save a memory note (types: user, feedback, project, reference): what to remember, why, how to apply it. Writes to the memory index",
      {"note": s("Text of the note"), "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
       "why": s("Why this is worth remembering"), "apply": s("How to apply it later"),
       "scope": {"type": "string", "enum": ["project", "global"], "description": "default project"}}, ["note"]),
    T("skill", "Load the full text of a skill by name. In the context there are only skill names and descriptions (progressive disclosure)",
      {"name": s("Skill name"), "args": s("Arguments substituted into $ARGUMENTS")}, ["name"]),
    T("exit_plan_mode", "Finish planning: show the plan and ask the user to confirm it before implementing",
      {"plan": s("The plan")}, ["plan"]),
]
if FINISH_TOOL_ENABLED:
    BUILTIN.append(T("finish", "Close this turn immediately and provide its final response in text. Call this alone, after all work and verification are complete; it prevents an extra model generation.",
                     {"text": s("Final answer to show the user")}, ["text"]))
READONLY = ("read_file", "grep", "glob", "web_search", "fetch_url", "ask_user", BUILTIN[0]["function"]["name"], "py_source", "py_run", "todo_write",
            "skill", "exit_plan_mode", "finish")   # plan-mode allowlist; powershell/py_run are checked below
# Only calls that are intrinsically read-only may run together. Shell/Python snippets can mutate
# state, and ask_user/todo_write are interaction/state operations, so they must not be batched.
PARALLEL_READS = {"read_file", "grep", "glob", "web_search", "fetch_url", "py_source", "skill"}
EDIT_TOOLS = ("edit_file", "write_file", "patch", "apply_diff")  # forbidden in plan mode
