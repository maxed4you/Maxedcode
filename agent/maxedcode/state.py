"""Session state, commands, modes and texts (compaction prompt, tool labels)."""
from contextlib import suppress
from collections import deque
import json
import os
from .config import MODEL, RUNTIME, STATE_FILE, norm_effort

def load_state():
    """Settings kept between runs: the last effort."""
    with suppress(Exception):
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}
def save_state():
    with suppress(Exception):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"effort": S["effort"], "sampling": RUNTIME}), encoding="utf-8")
S = {"model": MODEL, "mode": "act", "auto": False,
     "ctx": 0, "ptok": 0, "cached": 0, "usage": True, "effort": norm_effort(os.environ.get("AGENT_EFFORT") or load_state().get("effort", "medium")),
     "last_mode": None, "pre_plan": "manual"}
EFFORTS = ("off", "low", "medium", "high", "ultra")
CMDS = {"/new": "New session", "/compact": "Compress history into a summary: /compact [N] (the last N messages stay as is)",
        "/context": "What goes into the context (token estimate and estimated cost)",
        "/search": "Search saved sessions: /search query (Enter opens the matching session)", "/copy": "Copy the whole chat (/copy last — only the last reply)", "/effort": "Reasoning depth: /effort off|low|medium|high|ultra (ultra: big multi-part tasks, staged run with review)", "/sampling": "Sampling live: /sampling | /sampling temp=0.3 topk=20 top_p=0.95 | /sampling reset (saved to state.json)",
        "/btw": "Side question (not added to history)", "/history": "Session history", "/model": "Switch model: /model name",
        "/server": "Model server of the agent: /server (found on this PC, a cloud API, or type an address) or /server <ip|url>",
        "/cloud": "Cloud code drafts over any number of keys/providers: /cloud setup | add | list | default <id> [model] | models <id> | test [id] | remove <id> | note <model> <text> | status | disable | tokens <n> | subagents on|off | autosend on|off (on = send without asking; same as Settings -> Cloud in the window)",
        "/mcp": "MCP: /mcp add name command… | remove name | reload", "/plan": "Plan mode: reading/searching allowed, editing forbidden", "/act": "Manual mode: asks before edits and commands",
        "/run": "Continue right away: switch to Act and do what you planned (/run what exactly)",
        "/auto": "Auto mode (autoconfirm tools)", "/help": "Help",
    "/undo": "Undo every file change of the last step (checkpoints from edit_file/write_file/patch; shell edits are not tracked)",
    "/redo": "Redo the last step reverted with /undo",
    "/sessions": "Session history (alias of /history)",
    "/models": "List configured models or switch: /models <name>",
    "/editor": "Write a long prompt in a big editor (also Ctrl+G or Alt+G in the input); Ctrl+S puts it back into the input",
    "/allow": "Permission rule: /allow tool or tool(pattern)", "/deny": "Permission rule: /deny tool or tool(pattern)",
    "/permissions": "Show allow/deny/ask rules (permissions.json)", "/quit": "Quit",
    "/memory": "Memory files and notes: /memory shows what is loaded · /memory <text> saves a project note",
    "/init": "Draft the project instruction file (CLAUDE.md/AGENTS.md) from the project map, then write it after confirmation",
    "/cache": "Prefix cache: cached tokens of the last request + hit rate from /metrics",
    "/skill": "Load a skill: /skill name args (no name: list skills; own skills live in .maxedcode/skills)",
    "/commands": "Custom slash commands from .maxedcode/commands", "/agents": "Custom subagent definitions from .maxedcode/agents",
    "/rewind": "Back to an earlier point of the dialog (state + files from checkpoints)",
    "/resume": "Resume a session of this cwd: /resume | /resume <id>", "/save": "Save the session, optionally with a name",
    "/hooks": "Show hooks from settings.json",
    "/tools": "Show the tool catalog that is in the prompt prefix"}
# Modes, cycle: manual → accept-edits → auto → plan → manual (Tab forward, Shift+Tab back)
MODES = (("auto", "auto mode"), ("manual", "manual mode"), ("plan", "plan mode"),
         ("accept-edits", "accept-edits mode"))
MODE_COLORS = {"auto": "#e5c07b", "plan": "#c678dd", "manual": "grey62", "accept-edits": "#98c379"}
CYCLE = ("manual", "accept-edits", "auto", "plan")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
VERBS = ("Thinking", "Analyzing", "Matching", "Decoding", "Predicting", "Counting",
         "Planning", "Synthesizing", "Cross-checking", "Untangling", "Running vectors", "Looking for traces")
TITLES = {"powershell": "PowerShell", "read_file": "Reading file", "write_file": "Writing file",
          "edit_file": "Editing file", "patch": "Patching file", "apply_diff": "Applying diff", "finish": "Finish turn",
          "tool_search": "MCP tool schema", "grep": "Searching files",
          "py_run": "Python", "py_source": "Library source", "web_search": "Web search", "fetch_url": "Opening page", "browser": "Browser", "ask_user": "Question",
          "todo_write": "Plan", "remember": "Memory note", "skill": "Skill", "exit_plan_mode": "Plan",
          "subagent": "Subagent", "cloud_code": "Cloud code draft", "bg_output": "Background task", "bg_kill": "Background task"}
COMPACT_PROMPT = ("Summarize the conversation for continuation by the same coding agent. "
                  "Return only the summary; do not answer the user or perform new work.\n\n"
                  "# Objective\nState the concrete user goal and acceptance criteria. Quote the user's own words where the exact wording matters, and every correction or preference the user gave.\n"
                  "# Important Details\nPreserve constraints, decisions with their reason, exact identifiers, paths, commands, versions, and key code snippets.\n"
                  "# Work State\n"
                  "## Completed\nList verified changes and checks.\n"
                  "## Inspected Evidence\nRecord each already inspected file/range and the exact facts obtained, plus successful searches/tests and failed tool attempts that must not be repeated.\n"
                  "## Errors and Fixes\nEach error met (exact message) and how it was resolved, or that it is still open.\n"
                  "## Active\nDescribe the unfinished implementation or investigation.\n"
                  "## Pending\nWork the user asked for that is not started yet.\n"
                  "## Blocked\nRecord blockers and what evidence is missing; do not invent progress.\n"
                  "# Next Move\nGive the next concrete step in execution order.\n"
                  "# Relevant Files\nList exact paths and why they matter.\n\n"
                  "Keep every section in this order; write (none) for an empty one. Use terse bullets, not prose. "
                  "If the conversation already holds an earlier summary, merge it: keep facts that are still true, drop stale ones, do not copy it verbatim. "
                  "After compaction, treat Completed and Inspected Evidence as durable: do not repeat those inspections or checks unless code changed or new evidence contradicts them. Continue from Next Move. "
                  "Be concise but lossless for actionable details. Mark uncertain claims as uncertain. "
                  "Keep code and paths verbatim where changing them could break continuation. Do not call any tool.")

# Shared message queues: steer interrupts the next model call; user queue waits for the current turn.
USER_QUEUE = []
STEER_QUEUE = []

# Completed background subagents are delivered as ordinary user messages, after system + memory.
SUBAGENT_RESULTS = deque()

# In-process open-session tabs and delegated child sessions. Child metadata is also persisted
# in each session JSON so it can be reconstructed after an app restart.
OPEN_SESSIONS = []
CHILD_SESSIONS = {}
# A delegated task list is advanced one child at a time by the parent model.
SUBAGENT_BATCHES = {}

def track_open_session(sid):
    sid = str(sid or '').strip()
    # Stable order: switching to a tab must not move it to the end (tabs jumped around and the
    # oldest one fell off the strip).
    if not sid or sid in OPEN_SESSIONS:
        return
    OPEN_SESSIONS.append(sid)
    del OPEN_SESSIONS[:-12]

def upsert_child_session(sid, **fields):
    sid = str(sid or '').strip()
    if not sid:
        return {}
    row = CHILD_SESSIONS.setdefault(sid, {"id": sid})
    row.update(fields)
    return row
