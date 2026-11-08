"""Model-authored task checklist shared by the tool and the input-bar widget."""
import re
import unicodedata

STATUS = ("pending", "in_progress", "done")
MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
# Spellings other agents use (Claude Code / DeepSeek Harness: "completed"): accepted instead of a failed call.
STATUS_ALIASES = {"completed": "done", "complete": "done", "finished": "done", "in-progress": "in_progress",
                  "inprogress": "in_progress", "active": "in_progress", "todo": "pending", "not_started": "pending"}


def normalize(todos):
    """The same list with known status spellings mapped onto STATUS; anything else is left for validate()."""
    if not isinstance(todos, list):
        return todos
    out = []
    for t in todos:
        if isinstance(t, dict) and isinstance(t.get("status"), str):
            s = t["status"].strip().lower()
            t = {**t, "status": STATUS_ALIASES.get(s, s)}
        out.append(t)
    return out
# No fixed cap: the model chooses how many steps its checklist needs.
MAX_TASKS = None


# Japanese mixes kanji and kana, Korean may mix hangul and hanja: one writing system each.
_SCRIPT_GROUP = {"HIRAGANA": "CJK", "KATAKANA": "CJK", "HANGUL": "CJK"}


def _lang_of(text):
    """Writing system of a task description (CYRILLIC, GREEK, ARABIC, CJK, ... or LATIN), any language.

    `code` spans stay untranslated and are ignored. Latin letters also appear as names inside non-Latin
    text (pytest, README), so a non-Latin script wins whenever it is present; no letters at all -> None."""
    counts = {}
    for ch in re.sub(r"`[^`]*`", "", text):
        if ch.isalpha():
            script = unicodedata.name(ch, "LATIN").split()[0]
            script = _SCRIPT_GROUP.get(script, script)
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None
    other = {k: v for k, v in counts.items() if k != "LATIN"}
    return max(other, key=other.get) if other else "LATIN"


def validate(todos, lang=None):
    todos = normalize(todos)
    if not isinstance(todos, list) or not todos:
        return "Error: todos must be a non-empty list of {content, status}"
    active = 0
    for i, t in enumerate(todos):
        if not isinstance(t, dict) or not isinstance(t.get("content"), str) or not t["content"].strip() or t.get("status") not in STATUS:
            return f"Error: item {i} needs non-empty content and status in {'/'.join(STATUS)}"
        if not lang:
            lang = _lang_of(t["content"])
        elif _lang_of(t["content"]) not in (None, lang):
            return f"Error: item {i} is in another language. Write the whole checklist in the language of the user's request."
        if t["status"] == "in_progress":
            active += 1
    if active > 1:
        return "Error: at most one task may have status in_progress"
    return None


def copy_tasks(todos, lang=None):
    """Return an independent, display-safe task list or raise ValueError."""
    todos = normalize(todos)
    err = validate(todos, lang)
    if err:
        raise ValueError(err)
    return [{"content": item["content"].strip(), "status": item["status"]} for item in todos]


def tool_todo(todos, lang=None):
    todos = normalize(todos)
    err = validate(todos, lang)
    if err:
        return err
    done = sum(1 for t in todos if t["status"] == "done")
    return f"{done}/{len(todos)} done\n" + "\n".join(f"{MARK[t['status']]} {t['content']}" for t in todos)


# Progress checkpoints are placed at stage boundaries, not after every tool call.
# This nudges the model to report real transitions without adding a todo call
# after every individual file read or edit.
_RESEARCH_PS = re.compile(
    r"(?i)^(?:Get-ChildItem|ls|dir|cat|type|Get-Content|Select-String|grep|rg|head|tail|pwd|where|whoami|echo|Get-Command|git\s+(?:status|log|diff|show|blame|remote\s+geturl))\b"
)
_TEST_COMMAND = re.compile(
    r"(?i)\b(?:pytest|unittest|nose2?|npm\s+(?:run\s+)?test|pnpm\s+test|yarn\s+test|jest|vitest|cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test|py_compile|compileall|ruff|mypy|flake8|tsc|typecheck)\b"
)


def is_verification_tool(name, args):
    """Whether a tool call runs a likely test/lint/type/syntax verification."""
    if name == "powershell":
        return bool(_TEST_COMMAND.search(str(args.get("command") or "")))
    if name == "py_run":
        code = str(args.get("code") or "")
        return bool(_TEST_COMMAND.search(code) or re.search(r"(?i)\b(?:unittest|pytest|assertRaises|assertEqual|assertTrue|compileall|py_compile)\b", code))
    return False


class TaskProgressGuard:
    """Check stage transitions only when the model has chosen to maintain a checklist.

    Research/edit/verification transitions are checkpointed without requiring an update after
    every individual file. Checklists are optional: short tasks and tasks without a visible plan
    must not incur an extra model turn just to satisfy the UI.
    """

    RESEARCH_TOOLS = {"read_file", "grep", "py_source", "web_search", "fetch_url", "subagent"}
    EDIT_TOOLS = {"edit_file", "write_file", "patch", "apply_diff"}
    RUN_TOOLS = {"powershell", "py_run"}
    # Never blocked and never counted: they are not work, they are interaction or state.
    EXEMPT = {"todo_write", "ask_user", "finish", "exit_plan_mode", "skill", "remember",
              "tool_search", "bg_output", "bg_kill"}
    def __init__(self, strict=True):
        # strict=False: the checklist is cosmetic and never blocks tools or forces extra model turns.
        self.strict = strict
        self.research_since_update = False
        self.edits_since_update = False
        self.closed_by_tool = False

    def reset(self):
        self.research_since_update = False
        self.edits_since_update = False
        self.closed_by_tool = False

    def finish_by_tool(self):
        """Record that the turn ended explicitly rather than due to a silent empty response."""
        self.closed_by_tool = True

    def plan_updated(self, old_tasks, new_tasks):
        """Clear a checkpoint only if the model actually changed the plan/statuses."""
        if old_tasks != new_tasks:
            self.research_since_update = False
            self.edits_since_update = False
            return True
        return False

    def substantive(self, name, args):
        """Whether a call is real work (research, a file change or a command), not just interaction."""
        if name in self.RESEARCH_TOOLS or name in self.EDIT_TOOLS or name in self.RUN_TOOLS:
            return True
        return name == "powershell" and bool(_RESEARCH_PS.search(str(args.get("command") or "").strip()))

    def blocked_reason(self, name, args, tasks):
        if name == "todo_write" or not self.strict:
            return None
        # Never force an extra model round-trip solely to create a checklist.
        if not tasks:
            return None
        if name in self.EXEMPT:
            return None
        if name in self.EDIT_TOOLS and self.research_since_update:
            return (
                "Task checklist checkpoint required: research has produced results, but the live plan "
                "has not been updated. Call todo_write with the completed research/inspection step marked "
                "done and the implementation step marked in_progress, then retry this file change. "
                "Keep the same task content if appropriate, but the statuses must reflect real progress."
            )
        if self.edits_since_update and is_verification_tool(name, args):
            return (
                "Task checklist checkpoint required: code changes have been made, but the live plan "
                "has not been updated for verification. Call todo_write to mark completed implementation "
                "steps done and the relevant test/check step in_progress, then retry this verification."
            )
        return None

    def observe_tool(self, name, args, ok):
        if not ok:
            return
        # Only treat research as a stage before implementation has started. Reads
        # during an edit/debug cycle must not force an update between every file edit.
        if not self.edits_since_update:
            if name in self.RESEARCH_TOOLS:
                self.research_since_update = True
            elif name == "powershell" and _RESEARCH_PS.search(str(args.get("command") or "").strip()):
                self.research_since_update = True
        if name in self.EDIT_TOOLS:
            self.edits_since_update = True
