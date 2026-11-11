"""Agent: dialogue loop, commands, tool calls."""
import asyncio
import ctypes
import json
import os
import re
import shlex
import sys
import threading
import time
import traceback
from contextlib import suppress
from pathlib import Path

import openai
from openai import AsyncOpenAI
from rich.markdown import Markdown
from rich.text import Text
from rich.table import Table
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Button, Collapsible, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from .. import hooks, memory, permissions, skills, subagent, state, cloud
from .. import compact, config, ultra
from .. import instruction_state
from .. import llmretry
from ..config import LOOP_RETRY_THINK, CACHE_DEBUG, PROMISE_CHECK, TOOL_CHOICE_NONE,API_KEY, BASE_URL, BUDGET_RE, COMPACT_AT, COMPACT_KEEP, COMPACT_KEEP_TOKENS, COMPACT_MAX_CHARS, CTX_SIZE_RE, CTX_SRC, CTX_WINDOW, ENV_BLOCK, FINISH_TOOL_ENABLED, HIST_FILE, RUNTIME, HOME, MCP_FILE, MIN_THINK, PERMS_FILE, SESS, SETTINGS, SYSTEM, ANSWER_ROOM, THINK_BUDGET, THINK_CAP, answer_room, cost_text, effort_body, summary_effort, edit_format_for, sampling_for, think_budget, quick_step, planning_step, THINK_CUT_NUDGE, LENGTH_CONTINUES, length_continue, top_k_for, min_p_for, usage_cost
from ..state import CMDS, COMPACT_PROMPT, CYCLE, EFFORTS, MODES, MODE_COLORS, S, SPIN, TITLES, VERBS, save_state
from ..tools import BUILTIN, EDIT_TOOLS, READONLY, WRITE_PS, WRITE_PY, parallel_safe, prompt_needs_repo_map, prompt_needs_tools, ps_readonly, tool_request_policy
from ..tools.catalog import session_catalog
from ..tools.code_mode import run_code
from ..tools import browser as browser_tool, runners, undo, web
from ..tools.fs import attach_files, disk_text, tool_apply_diff, tool_edit, tool_glob, tool_grep, tool_read, tool_write
from ..tool_validation import (text_tool_calls, canonical_tool_name, normalize_tool_arguments, sanitize_tool_history,
                                validate_required_arguments, validate_tool_arguments, normalization_notice,
                                broken_text_call, BROKEN_CALL_FEEDBACK)
from ..tools.mcp import MCP
from ..tools.pysrc import tool_pysrc
from ..shelltool import NAME as SHELL_TOOL
from ..ui.screens import BtwScreen, CloudApprovalScreen, CloudSetupScreen, CommandPalette, EditorScreen, EffortScreen, HistoryScreen, LinkScreen, Pick
from ..ui.widgets import Card, ChangeCard, Chat, Chrome, Prompt, SelStatic, TaskProgress, TodoPanel
from ..ui.links import chat_md, open_url
from ..filewatch import KEEP_TEXT, FileWatch, saved_changes, scratch_note, watched_files
from ..loopguard import LoopGuard, PROMISE_NUDGE, collapse as collapse_loop, judge_stalled, needs_judge, stalled_promise
from ..events import EventEmitter, EventMetrics
from ..repo_map import build_repo_map
from ..tools.todo import MARK, TaskProgressGuard, is_verification_tool, normalize as normalize_todos, tool_todo, validate as validate_todos
from ..util import attach_late, build_content, tool_content, cache_stats, cached_tokens, clean_title, clip, dur, hash_cwd, housekeeping, index_session, last_session, list_sessions, normalize_tool_output, repair_json, search_sessions, server_ctx, session_file, session_path, session_title, split_think, squash_loops, strip_ansi, think_loop, tool_call_status, track_tool_call, visible_user_text, close_open_calls, to_clip, tok, unwrap, wire, write_json
from .. import cachekey
from ..cachekey import extra_body as cache_extra_body
from ..request_compat import unsupported_optional_fields
from .. import cachedebug
from ..streaming import OneChunkStream

def key_of(a):
    """Main argument for permission rules: command / code / path."""
    return a.get("command") or a.get("code") or a.get("path") or a.get("task") or a.get("action") or ""

def compact_anchor(msgs):
    """Id of the existing compact anchor: recompaction updates that one message instead of stacking
    summaries, so everything after it keeps its position in the cached prefix."""
    for m in msgs:
        if m.get("role") == "user":
            match = re.search(r'<compact id="([^"]+)"', str(m.get("content") or ""))
            if match:
                return match.group(1)
    return None

def new_session_id():
    """Generate a timestamp-like session ID without colliding with another tab/file."""
    base = time.strftime("%Y%m%d-%H%M%S")
    candidate, suffix = base, 1
    while candidate in state.OPEN_SESSIONS or session_file(candidate, os.getcwd()).exists():
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    return candidate

# VS Code's integrated terminal keeps Ctrl+P (Quick Open), Ctrl+G (Go to Line), Ctrl+J (panel) and
# Ctrl+Q for itself (terminal.integrated.commandsToSkipShell; F1/F5/F6/F10/F11 too). Alt+P / Alt+G reach
# the app there and work in every terminal, so hints show them inside VS Code (and Cursor, Windsurf…).
IN_VSCODE = os.environ.get("TERM_PROGRAM", "").lower() == "vscode"
PALETTE_KEY, EDITOR_KEY = ("alt+p", "alt+g") if IN_VSCODE else ("ctrl+p", "ctrl+g")


def key_label(key):
    """Key name for help and notes: alt+p -> Alt+P."""
    return "+".join(part.capitalize() if len(part) > 1 else part.upper() for part in key.split("+"))


# Key cheat sheet: on the home screen under the input, in the chat — on the right of the mode line
HOTKEYS = (("shift+tab", "modes"), (PALETTE_KEY, "commands"), (EDITOR_KEY, "editor"))

HOME_PLACEHOLDER = 'Ask anything… "Fix a TODO in the codebase"'

# Block logo in the OpenCode style: row 0 — ascenders, ░ — fill inside a letter
LOGO_GLYPHS = {
    "m": ("     ", "█▀█▀█", "█░█░█", "▀ ▀ ▀"),
    "a": ("    ", "▀▀▀█", "█▀▀█", "▀▀▀▀"),
    "x": ("    ", "▀▄▄▀", "▄▀▀▄", "▀  ▀"),
    "e": ("    ", "█▀▀█", "█▀▀▀", "▀▀▀▀"),
    "d": ("   ▄", "█▀▀█", "█░░█", "▀▀▀▀"),
    "c": ("    ", "█▀▀▀", "█░░░", "▀▀▀▀"),
    "o": ("    ", "█▀▀█", "█░░█", "▀▀▀▀"),
}

# Logo: a large "maxed" with an orange top-to-bottom gradient and a small "code" on its baseline
LOGO_ROWS = ("#f6c3a6", "#ec9a74", "#d97757", "#a9573b")
LOGO_FILL = "#3b2219"

def logo():
    """Large "maxed" (per-row gradient) + small "code" at the bottom right, centered."""
    t = Text(justify="center")
    tail = ("     ", "     ", "     ", " code")   # rows of equal width, otherwise centering misaligns them
    for row in range(4):
        if row:
            t.append("\n")
        for li, ch in enumerate("maxed"):
            if li:
                t.append(" ")
            for px in LOGO_GLYPHS[ch][row]:
                t.append("█" if px == "░" else px, style=LOGO_FILL if px == "░" else LOGO_ROWS[row])
        t.append(tail[row], style="bold #e8e8e8")
    t.append("\nlocal-first coding agent", style="#6b7280")
    return t

def git_branch(cwd=None):
    """Current git branch from .git/HEAD (without running git); '' outside a repository."""
    with suppress(Exception):
        p = Path(cwd or os.getcwd()).resolve()
        for d in (p, *p.parents):
            head = d / ".git" / "HEAD"
            if head.is_file():
                ref = head.read_text(encoding="utf-8").strip()
                return ref.removeprefix("ref: refs/heads/") if ref.startswith("ref:") else ref[:7]
    return ""

# Palette for service output: old style names → soft colors (pure ANSI cyan/yellow/red hurt the eyes)
NOTE_STYLES = {"cyan": "#c9d1d9", "green": "#98c379", "yellow": "#e5c07b", "red": "#e06c75",
               "grey70": "#b0b5bd", "grey62": "#9aa0a8", "white": "#d4d4d4"}
MODE_HINTS = {"manual": "asks before edits and commands", "accept-edits": "edits without asking, commands ask",
              "auto": "tools run without asking", "plan": "read and search only, no edits"}


def help_text(app=None):
    """/help: commands in the same groups as in Ctrl+P; under each command — its current value
    (the same as shown in Ctrl+P), and a short key cheat sheet."""
    from .screens import PALETTE_GROUPS, command_badge
    t, listed = Text(), set()
    width = max(len(k) for k in CMDS) + 2
    groups = [*PALETTE_GROUPS, ("other", tuple(k for k in CMDS if all(k not in g for _, g in PALETTE_GROUPS)))]
    for title, names in groups:
        names = [n for n in names if n in CMDS and n not in listed]
        if not names:
            continue
        t.append(("\n" if t else "") + title + "\n", style="bold #d97757")
        for n in names:
            listed.add(n)
            t.append(f"  {n:<{width}}", style="bold #e0e0e0")
            t.append(CMDS[n] + "\n", style="#9aa0a8")
            value, style = command_badge(app, n) if app else ("", "")
            if value:
                t.append(" " * (width + 2) + ("" if value.startswith("●") else "now "), style="grey42")
                t.append(value + "\n", style=style)
    t.append("\nkeys\n", style="bold #d97757")
    newline = "Alt+Enter" if IN_VSCODE else "Ctrl+J, Alt+Enter"   # VS Code takes Ctrl+J
    for key, what in ((key_label(PALETTE_KEY), "command deck: search, ↑↓, Enter run, Tab insert"),
                      (key_label(EDITOR_KEY), "prompt editor: a big field for a long prompt, Ctrl+S puts it back"),
                      ("Shift+Enter", f"new line (also {newline}, or \\ at the end of the line)"),
                      ("Shift+Tab", "cycle mode: manual → accept-edits → auto → plan"),
                      ("Ctrl+N", "new session · Ctrl+Left/Right switch tabs"),
                      ("Esc", "take back an unread message / interrupt the model"),
                      ("Alt+V", "paste an image · drag files into the window to attach them"),
                      ("@path", "attach a file (@path#10-40 for lines)"),
                      ("/copy", "copy the chat · /copy last — the last reply · drag to select text")):
        t.append(f"  {key:<{width}}", style="bold #e0e0e0")
        t.append(what + "\n", style="#9aa0a8")
    t.rstrip()
    return t


def split_row(left, right, below=None):
    """A "left … right" line sized to the widget's real width. Rich splits the space itself, so the line
    can't get wider than the screen. The width used to be padded with spaces from App.size: the line came out
    a couple of characters wider than its slot, wrapped, the bottom panel grew, and the whole screen started
    scrolling ("ghosts" of the status line)."""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True, overflow="ellipsis")
    grid.add_row(left, right)
    if below is not None:
        grid.add_row(below, "")
    return grid


def ktok(n):
    """9700 → "9.7k", 32768 → "33k": short form for the status line."""
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if 1000 <= n < 10000 else f"{n // 1000}k" if n >= 10000 else str(n)


def sampling_text(model):
    """/sampling: every value that actually goes into the request, and where it comes from
    (/sampling override → model_settings in settings.json → environment variable → default)."""
    from ..config import model_profile
    profile = model_profile(model)
    cur = sampling_for(model)
    rows = [("temperature", cur.get("temperature"), "AGENT_TEMP", ("temperature", "temp")),
            ("top_p", cur.get("top_p"), "AGENT_TOP_P", ("top_p",)),
            ("top_k", top_k_for(model), "AGENT_TOP_K", ("top_k",)),
            ("min_p", 0.05, None, ()),
            ("presence_penalty", cur.get("presence_penalty"), "AGENT_PRESENCE", ("presence_penalty",))]
    if "frequency_penalty" in cur:
        rows.append(("frequency_penalty", cur["frequency_penalty"], None, ("frequency_penalty",)))
    t = Text()
    t.append("sent with every request to ", style="#9aa0a8")
    t.append(str(model), style="bold #e0e0e0")
    t.append("\n")
    for key, value, env, profile_keys in rows:
        if key in RUNTIME:
            src, style = "/sampling override (state.json)", "#e5c07b"
        elif any(k in profile for k in profile_keys):
            src, style = "model_settings in settings.json", "#98c379"
        elif env and os.environ.get(env):
            src, style = f"env {env}", "#c9d1d9"
        elif key == "min_p":
            src, style = "fixed in the request", "grey50"
        else:
            src, style = "default", "grey50"
        t.append(f"  {key:<19}", style="#c9d1d9")
        t.append(f"{value:<8g}" if isinstance(value, (int, float)) else f"{value!s:<8}", style="bold #e0e0e0")
        t.append(src + "\n", style=style)
    t.append("\nchange: /sampling temp=0.3 topk=20 top_p=0.95 · back to the settings: /sampling reset", style="grey50")
    return t


def permissions_text(rules, path):
    """/permissions: as one block, rules without "\\ " escaping, one per line."""
    colors = {"allow": "#98c379", "deny": "#e06c75", "ask": "#e5c07b"}
    t = Text()
    for i, (kind_, rule) in enumerate(rules, 1):
        shown_rule = str(rule).replace("(^", "(", 1).replace("$)", ")")       # "always" rules are ^re.escape$
        plain = re.sub(r"\\(.)", r"\1", shown_rule, flags=re.S)   # rules are stored in the file as re.escape
        lines = plain.splitlines() or [""]
        shown = lines[0] if len(lines[0]) <= 110 else lines[0][:109] + "…"
        t.append(f"{i:>3}  ", style="grey42")
        t.append(f"{kind_:<6}", style=f"bold {colors.get(kind_, '#c9d1d9')}")
        t.append(shown, style="#c9d1d9")
        if len(lines) > 1:
            t.append(f"  (+{len(lines) - 1} lines)", style="grey50")
        t.append("\n")
    t.append(f"\nfile: {path} · /allow, /deny, /ask add rules", style="grey50")
    return t


def drain_pending(msgs, queue):
    """Steer items arrive as new messages after the prefix; the prompt message is never edited."""
    while queue:
        q = queue.pop(0)
        msgs.append({"role": "user", "content": build_content(q, q)})

class TabStrip(HorizontalScroll):
    """Session tab strip: scrolls to the active tab when the window narrows."""

    def on_resize(self, event):
        self.call_after_refresh(self.app._scroll_active_tab)

    def watch_scroll_x(self, old, new):
        super().watch_scroll_x(old, new)
        self.app._update_tab_count()


class _Detached:
    """Stands in for the input when a queued message is sent: clearing it must not wipe what is typed now."""
    text = ""


class TabChoice(str):
    """A line in the tab list that remembers which session it opens."""
    sid = ""


def delegated_task(content):
    """The task a subagent received, without the delegation rules the parent wrapped around it."""
    if isinstance(content, str) and content.startswith("<delegation>") and "\nTask:\n" in content:
        return content.split("\nTask:\n", 1)[1].strip()   # a service tag: visible_user_text would hide it all
    return (visible_user_text(content) or "").strip()


class AgentApp(App):
    TITLE = "maxedcode"
    CSS = """
    /* overflow: hidden — the main screen never scrolls as a whole: if something at the bottom grows,
       let the chat (1fr) shrink instead of the whole screen moving with "ghost" lines */
    Screen { background: #000000; color: #d4d4d4; overflow: hidden hidden; }
    Screen > .screen--selection { background: #2a2f38; color: #d4d4d4; }
    #welcome { height: 1fr; padding: 0 2; content-align: center bottom; text-align: center; }
    #home-hint, #home-pad { display: none; }
    /* Home page (OpenCode-style): logo, centered input box with an accent bar, hints under it */
    Screen.-home #copied, Screen.-home #busy, Screen.-home #status, Screen.-home #arrow { display: none; }
    Screen.-home #inrow { width: 78; max-width: 100%; padding: 1 2 0 1; border: none;
                          border-left: thick #d97757; background: #161616; }
    Screen.-home #modes { width: 78; max-width: 100%; height: 2; padding: 0 2 1 1; border-left: thick #d97757; background: #161616; }
    Screen.-home #slash { width: 78; max-width: 100%; margin: 0; }
    Screen.-home #home-hint { display: block; width: 78; max-width: 100%; height: 1; }
    Screen.-home #home-pad { display: block; height: 1fr; }
    #chat { height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; }
    #chat > Static, #chat > Collapsible { margin-bottom: 1; }
    .user { background: #121519; padding: 0 1; border-left: thick #d97757; }
    .note { padding: 0 1; border-left: outer #2c3138; }
    Collapsible { border: none; padding: 0; background: transparent; }
    Collapsible > Static { color: #d97757; }
    Collapsible > Contents { padding: 0 0 0 2; color: #8a8f98; }
    #session-bar { height: 1; max-height: 1; background: #0a0a0a; }
    #session-tabs { width: 1fr; height: 1; max-height: 1; background: #0a0a0a; padding: 0 1; scrollbar-size-horizontal: 0; }
    #session-tabs Button { height: 1; min-width: 0; max-width: 42; padding: 0; margin: 0; border: none;
                           background: #0a0a0a; color: #6f6f6f; text-style: none; }
    #session-tabs Button:hover { background: #1a1a1a; color: #d4d4d4; }
    #session-tabs Button.active { background: #1c1c1c; color: #c8c8c8; }
    #session-tabs Button.running { color: #d97757; }
    #session-tabs Button.close-tab { color: #4a4a4a; margin: 0 1 0 0; }
    #session-tabs Button.close-tab:hover { background: #3a1d1d; color: #e06c75; }
    /* a button, not a tab: accent background so "New session" isn't confused with the tabs */
    #new-session-tab { width: auto; min-width: 0; height: 1; padding: 0 1; margin: 0 1 0 0; border: none;
                       background: #d97757; color: #0a0a0a; text-style: bold; }
    #new-session-tab:hover { background: #e8936f; color: #0a0a0a; }
    #new-session-tab:disabled { background: #2a2a2a; color: #5a5a5a; text-style: bold; }
    /* always visible: how many tabs there are and how many the narrow strip hides; click — list of all */
    #tab-count { width: auto; min-width: 0; height: 1; padding: 0 1; margin: 0 1 0 0; border: none;
                 background: #1c1c1c; color: #9a9a9a; text-style: none; }
    #tab-count:hover { background: #2a2a2a; color: #e0e0e0; }
    #tab-count.hidden-tabs { background: #3a2a20; color: #e8936f; text-style: bold; }
    /* another tab looked at while the live session keeps running (or a subagent transcript) */
    #peek-wrap { display: none; height: 1fr; }
    #peek-head { height: auto; padding: 0 2; background: #0e0b12; border-bottom: solid #2a2433; }
    #peek-head.root { background: #0b0d10; border-bottom: solid #2c3138; }
    #peek { height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; }
    #peek > Static, #peek > Collapsible { margin-bottom: 1; }
    /* subagent messages: a violet bar instead of the parent's orange one */
    .sub { border-left: outer #8a6bb0; padding-left: 1; }
    .user.sub { background: #15121a; border-left: thick #8a6bb0; }
    #peek > .sub-tag { height: 1; margin-bottom: 0; }
    #busy { height: 1; padding: 0 2; color: #d97757; }
    /* Keep this row in the layout even when empty: copying must not move the chat
       under an active mouse selection. Keep it to one line so it cannot reflow. */
    #copied { height: 1; max-height: 1; margin: 0 2; background: #121519; color: #7ec69a; }
    #slash { display: none; height: auto; max-height: 9; margin: 0 2; border: round #33383f; background: #0b0d10; }
    #inrow { height: auto; margin: 0 2; border-top: solid #33383f; border-bottom: solid #33383f; }
    #arrow { width: 2; color: #d97757; text-style: bold; }
    #inp { border: none; background: transparent; height: auto; min-height: 2; max-height: 10; padding: 0; }
    #inp:focus { border: none; }
    #task-progress { height: auto; max-height: 16; padding: 0 2; }
    #status { height: 2; padding: 0 2; }
    #modes { height: 1; padding: 0 2; }
    /* Modal windows: one frame for all (like Ctrl+P), window type at the top left, keys at the bottom right */
    ModalScreen { align: center middle; background: #000000 65%; }
    .dialog { width: 92; max-width: 95%; height: auto; max-height: 92%; padding: 1 2; background: #0e1013;
              border: round #3a3f47; border-title-align: left; border-subtitle-align: right; }
    .dialog.wide { width: 120; }
    .dialog-title { margin-bottom: 1; }
    .dialog-body { margin-bottom: 1; }
    .dialog-label { margin-top: 1; }
    .dialog-scroll { height: auto; max-height: 24; margin-bottom: 1; scrollbar-size-vertical: 1;
                     scrollbar-color: #3a3f47; scrollbar-background: #0e1013; }
    #monitor-scroll { max-height: 40; }
    .dialog Input { height: 3; border: round #2c3138; background: #0e1013; padding: 0 1; }
    .dialog Input:focus { border: round #d97757; }
    .dialog TextArea { border: round #2c3138; background: #0b0d10; }
    .dialog TextArea:focus { border: round #d97757; }
    #editor-area { height: 22; max-height: 70vh; }
    #cloud-task-review { height: 14; max-height: 50vh; }
    .dialog OptionList, .dialog TextArea { scrollbar-size-vertical: 1; scrollbar-color: #3a3f47;
                                           scrollbar-color-hover: #555b64; scrollbar-background: #0e1013; }
    .dialog OptionList { height: auto; max-height: 18; border: none; background: transparent; padding: 0; }
    .dialog OptionList:focus { border: none; }
    .dialog OptionList > .option-list--option { padding: 0 1; }
    .dialog OptionList > .option-list--option-highlighted { background: #1f2a33; text-style: none; }
    .dialog OptionList > .option-list--option-hover { background: #171b20; }
    .dialog-actions { height: 1; margin-top: 1; align: right middle; }
    .dialog Button { height: 1; min-width: 0; padding: 0 1; margin: 0 0 0 2; border: none; background: #1f2329;
                     color: #c9d1d9; text-style: none; }
    .dialog Button:hover { background: #2c3138; }
    .dialog Button:focus { background: #343a43; color: #ffffff; text-style: bold; }
    .dialog Button.-primary { background: #d97757; color: #0e1013; text-style: bold; }
    .dialog Button.-primary:hover, .dialog Button.-primary:focus { background: #ec9472; color: #0e1013; }
    .dialog Checkbox { border: none; background: transparent; padding: 0; margin-top: 1; }
    .dialog Checkbox:focus { border: none; }
    .dialog Checkbox > .toggle--button { background: #2c3138; color: #2c3138; }
    .dialog Checkbox.-on > .toggle--button { background: #2c3138; color: #98c379; }
    .dialog Checkbox:focus > .toggle--label { background: transparent; text-style: bold; }
    #cloud-error { height: auto; margin-top: 1; }
    #editor-count { height: 1; text-align: right; }
    CommandPalette { align: center top; background: #000000 65%; }
    SettingsDeck #deck { width: 96; }
    #deck { width: 80; max-width: 95%; height: auto; max-height: 90%; margin-top: 3; padding: 0 1; background: #0e1013;
            border: round #3a3f47; border-title-align: left; border-subtitle-align: right; }
    #deck-search { height: 4; padding: 1 0 1 1; border-bottom: dashed #2a2e35; }
    #deck-caret { width: 2; }
    #palette-input { width: 1fr; border: none; background: transparent; padding: 0; height: 1; }
    #palette-input:focus { border: none; }
    #palette-list { height: auto; max-height: 18; border: none; background: transparent; padding: 0; margin: 0;
                    scrollbar-size-vertical: 1; scrollbar-color: #3a3f47; scrollbar-color-hover: #555b64; scrollbar-background: #0e1013; }
    #palette-list:focus { border: none; }
    #palette-list > .option-list--option { padding: 0 1; }
    #palette-list > .option-list--option-disabled { text-opacity: 100%; text-style: none; }
    #palette-list > .option-list--option-highlighted { background: #1f2a33; text-style: none; }
    #palette-list > .option-list--option-hover { background: #171b20; }
    #deck-detail { height: auto; min-height: 4; padding: 1 1 0 1; border-top: dashed #2a2e35; }
    """
    BINDINGS = [Binding("ctrl+c", "cancel_or_quit", priority=True), Binding("ctrl+q", "quit", priority=True),
                Binding("ctrl+n", "new_session", "New session", show=True),
                Binding("ctrl+left", "previous_session", "Previous session", show=False),
                Binding("ctrl+right", "next_session", "Next session", show=False),
                Binding("ctrl+shift+c", "copy_selection", "Copy selection / chat", show=False),
                Binding("ctrl+p", "command_palette", "Command palette", show=False),
                Binding("alt+p", "command_palette", show=False),   # Ctrl+P never reaches us in VS Code
                # not priority: modal screens (Pick, /btw, /history) keep their own Escape
                Binding("escape", "back", "Back / interrupt", show=False)]

    def __init__(self):
        super().__init__()
        self.events = EventEmitter()
        self.event_metrics = EventMetrics()
        self._turn_event_seq = 0
        self._agent_event_ended = False
        start_mode = os.environ.get("MAXEDCODE_START_MODE", "").strip().lower()
        if start_mode in ("auto", "manual", "plan", "accept-edits"):
            S["mode"] = "plan" if start_mode == "plan" else "accept-edits" if start_mode == "accept-edits" else "act"
            S["auto"] = start_mode == "auto"
        self.client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY, **llmretry.client_options())
        cloud.set_approval_handler(self._approve_cloud_request)
        self.mcp = MCP()
        self.sid, self.title_, self.msgs = new_session_id(), None, [{"role": "system", "content": SYSTEM}]
        state.track_open_session(self.sid)
        self._session_tabs_signature = None
        self._home_shown = None   # home screen (logo + centered input) or chat
        self._session_tabs_rendering = False
        self._hydrated_parent_ids = set()
        self._session_headers = {}
        # The live session (self.sid) is the one that runs turns. view_sid — another tab shown in its
        # place (a subagent transcript, or a session looked at while the live one is busy).
        self.view_sid, self._main_draft = None, ""
        self._peek_n, self._peek_cards, self._peek_key, self._peek_new = 0, {}, None, False
        self._peek_live = None   # the reply a running subagent is writing, shown below its saved messages
        self._session_queue = []   # (sid, text): first messages of other tabs, run once the live session is idle
        self._starting_queued = False   # drain_session_queue is opening a session to send its message
        self._peek_busy = False
        self.sess_path, self.sess_cwd = None, ""   # session file: its own cwd, or any project via /history
        self.mem, self.head, self.marks, self.hctx, self.name_ = [], 1, [], "", ""   # memory message: the cached prefix ends there
        self.rules_seen, self.docs_seen = set(), set()   # path-scoped rules and folder docs already injected
        self._late_ctx = []   # rules / folder docs / background results: delivered inside a tool result, never as a mid-turn message
        self.instate = instruction_state.InstructionState()   # fold baseline: a rewind must not drop it whole
        self.busy, self.worker, self.frame, self.last_c, self.verb = None, None, 0, 0.0, VERBS[0]
        self.compact_gate = compact.Gate()   # when auto-compact may run in this turn (no fixed count)
        self.compact_nudges = 0       # one template nudge per compaction, not a retry loop
        self._invalid_tool_retries = 0
        self._think_retries = 0      # replies cut off while still thinking (no answer)
        self._promise_nudged = False  # one "you announced a step but made no call" nudge per turn
        self._length_continues, self._last_finish, self._empty_retried = 0, None, False
        self._think_looped = False   # the last reply was stopped: its reasoning kept repeating itself
        self._last_budget = 0        # the thinking room the last request actually used (for the red note)
        self._budget_off = False     # sticky: a model that exhausts the cap gets no cap for the session
        self.t0, self.clipped, self._seen, self._seen_t, self._bar_timer = 0.0, "", "", 0.0, None
        self.saved_tok = 0     # tokens freed since the last real usage from the server (nothing frees them now)
        self._disabled_extra_fields_by_model = {}  # retain compatibility fallbacks across turns without dropping cache keys
        self._last_kw = None   # the last model request as sent: a /btw during a turn forks from exactly this prefix
        self._btw_jobs, self._btw_task, self._btw_scr = [], None, None   # side questions: queued, running, its screen
        self._btw_ready = True  # the server is free (between model steps or turns): a /btw may start now
        self.session_cost = 0.0
        self.todos, self.todo_w = [], None
        self.task_progress_guard = TaskProgressGuard(strict=bool(SETTINGS.get("todo_strict", False)))
        self.loopguard = LoopGuard()
        self.fwatch = FileWatch()   # files changed by tools that are not edit tools -> diff cards
        self._turn_request = ""
        self._todo_final_nudges = 0
        self._turn_metrics = {"model_ms": 0, "model_calls": 0, "tool_ms": 0, "tool_calls": 0, "tools": {}, "tool_ms_by_name": {}, "cost": 0.0, "finished_by_tool": False}
        self.progress_w = None
        self._repo_map_dirty = True
        self._repo_map_revision = 0
        self._repo_map_task = None
        self._repo_map_task_revision = None
        # These policy flags are read by tools()/est_tokens() during the first
        # on_mount -> refresh_status() call, before turn() has run.
        # Initialize the full state here so startup does not depend on a user turn.
        # The catalog is in the prefix from the very first request: the prefix must never change
        # mid-session, otherwise the server re-reads the whole conversation.
        self._use_tool_schemas = True
        self._allow_tool_calls = False
        self._tool_schemas_enabled = True
        self.hist, self.hpos, self.draft, self.queue = [], 0, "", []
        state.USER_QUEUE.clear()
        state.STEER_QUEUE.clear()
        self.steer_queue, self.queue_input, self._draining_queue = state.STEER_QUEUE, False, False
        self.queue = state.USER_QUEUE
        self._pending_input, self._retract = [], None   # what Esc can return to the input
        self.archive, self._summary_w = [], None   # messages replaced by compaction; live summary widget
        self.redo_stack = []
        undo.set_sid(self.sid)
        with suppress(Exception):
            self.hist = [json.loads(l)["p"] for l in HIST_FILE.read_text(encoding="utf-8").splitlines()][-200:]
        self.hpos = len(self.hist)
        self.build_mem()

    def build_mem(self):
        """Memory + skills as a separate message after system, so the cached prefix stays stable.
        Notes written later are echoed, this message is not rebuilt during the session.
        The environment block goes right after memory, as the first message the engine sees after the
        system turn: SYSTEM itself is byte-identical for every folder and every day, so the shared
        root checkpoint at the end of the system turn still hits for a brand new session."""
        t = memory.text() + skills.listing()
        self.mem = [{"role": "system", "content": "# Memory\n" + t}] if t else []
        self.mem.append({"role": "user", "content": ENV_BLOCK})
        self.mem_files = {str(f).lower() for _, f in memory.startup_files()}   # already in the memory message
        self.docs_seen = set()
        self._late_ctx = []
        self._resume_note = ""   # what changed in memory/env since a reopened session started (next message)
        self.head = 1 + len(self.mem)
        self.msgs = [self.msgs[0]] + self.mem

    def compose(self) -> ComposeResult:
        with Horizontal(id="session-bar"):
            yield TabStrip(id="session-tabs")
            # outside the scroll area: with any number of tabs the buttons stay visible on the right
            count = Button("▤ 1 tab", id="tab-count", compact=True)
            count.tooltip = "All open tabs"
            count.can_focus = False
            yield count
            new_button = Button("+ New session", id="new-session-tab", compact=True)
            new_button.tooltip = "Start a new session (Ctrl+N)"
            new_button.can_focus = False
            yield new_button
        yield Static("", id="welcome")
        yield Chat(id="chat")
        with Vertical(id="peek-wrap"):
            yield Chrome("", id="peek-head")
            yield Chat(id="peek")
        yield Chrome("", id="copied")
        yield OptionList(id="slash")
        yield Chrome("", id="busy")
        yield Chrome("", id="status")
        yield TaskProgress(id="task-progress")
        with Horizontal(id="inrow"):
            yield Chrome("❯", id="arrow")
            yield Prompt()
        yield Chrome("", id="modes")
        yield Chrome("", id="home-hint")
        yield Static("", id="home-pad")

    @property
    def chat(self):
        return self.query_one("#chat", Chat)

    def emit_event(self, name, **fields):
        """Record one normalized lifecycle event for the shared UI/headless event model."""
        event = self.events.emit(name, **fields)
        self.event_metrics.consume(event)
        return event

    async def on_mount(self):
        global CTX_WINDOW, CTX_SRC
        self.show_home(True)
        self.emit_event("agent_start", cwd=os.getcwd())
        self.progress_w = self.query_one("#task-progress", TaskProgress)
        with suppress(Exception):  # window/tab title: without this WT shows "python"
            if sys.platform == "win32":
                ctypes.windll.kernel32.SetConsoleTitleW(self.title)
            # VS Code does not see SetConsoleTitleW: it needs an OSC sequence (0 = title)
            self._driver.write(f"\x1b]0;{self.title}\x07")
            self._driver.flush()
        SESS.mkdir(parents=True, exist_ok=True)
        self._save_on_console_close()
        threading.Thread(target=housekeeping, daemon=True).start()   # old outputs/logs/checkpoints, off the UI thread
        os.environ.setdefault("COLORTERM", "truecolor")
        os.environ.setdefault("FORCE_COLOR", "3")
        # one look at the server (any kind): its window, its loaded model id, reasoning support. No server
        # (or a test without one): nothing else to ask
        info = {}
        with suppress(Exception):
            info = await asyncio.to_thread(config.resolve_server)
            if info.get("online"):      # AGENT_CTX stays, unless the loaded model has less (config._adopt)
                CTX_WINDOW, CTX_SRC = config.CTX_WINDOW, config.CTX_SRC
            if config.MODEL != "local":
                # the server's id: model_settings profiles match it, "local" matched none; on LM Studio /
                # Ollama the loaded model, so a request never makes the server load a second one
                S["model"] = config.MODEL
        self._model_note = info.get("note") or ""
        self._hydrate_child_sessions(self.sid)
        await self.banner()
        if self._model_note:
            await self.note(self._model_note, "grey50")
        self.update_notes()
        await self.refresh_session_tabs(force=True)
        h = await hooks.run("SessionStart", {"session_id": self.sid, "cwd": os.getcwd(), "event": "SessionStart",
                                             "source": "startup"})
        if h["context"] or h["message"]:
            t = (h["context"] + h["message"]).strip()
            self.msgs.append({"role": "system", "content": "<hook>\n" + t + "</hook>"})
            await self.note(t, "cyan")
        self.query_one(Prompt).focus()
        # timers fire during shutdown too, when screens are already gone: NoMatches is not an error there
        self.set_interval(0.1, lambda: self._quiet(self.tick))
        self.set_interval(0.15, self.sync_clip)
        self.set_interval(1.0, lambda: self._quiet(self.refresh_status))
        self.set_interval(0.5, self.peek_tick)
        for n, c in self.mcp.cfg().items():
            self.mcp.start(n, c)
        self.refresh_status()

    def _save_on_console_close(self):
        """Closing the console window (its ✕), logoff or shutdown ends the process without exit()/on_unmount.
        Windows gives a console control handler a few seconds first: enough to write the session."""
        if sys.platform != "win32":
            return
        with suppress(Exception):
            def on_event(event):
                if event in (2, 5, 6):   # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                    self.save_session(index=False)
                return 0   # the default handler still ends the process
            # kept on self: the callback must live as long as the process
            self._console_handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(on_event)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._console_handler, True)

    def exit(self, *args, **kwargs):
        """Quit (Ctrl+C twice, /quit): the session is saved as it is at this moment, with the unsent input."""
        self.save_session()
        return super().exit(*args, **kwargs)

    def _draft(self):
        """The live session's unsent input (while another tab is shown, it waits aside)."""
        if self.view_sid:
            return self._main_draft
        with suppress(Exception):
            return self.query_one(Prompt).text
        return ""

    def _restore_draft(self, text):
        if self.view_sid:
            self._main_draft = text or ""
            return
        with suppress(Exception):
            p = self.query_one(Prompt)
            p.text = text or ""
            p.move_cursor(p.document.end)

    def on_unmount(self):
        """Always terminate child processes and close the shared event stream."""
        self.save_session()
        cloud.set_approval_handler(None)
        if not self._agent_event_ended:
            self._agent_event_ended = True
            with suppress(Exception):
                self.emit_event("agent_end", session_id=self.sid, ok=True)
        with suppress(Exception):
            runners.cleanup()

    async def banner(self):
        """Render a clean OpenCode-style welcome page without adding splash text to the conversation."""
        cfg = cloud.status()
        t = logo()
        t.append("\n\n")
        if cfg.get("configured"):
            t.append("cloud ", style="grey50")
            t.append(str(cfg.get("model")), style="#7ec69a")
            t.append(" ready · approval before every call", style="grey50")
        else:
            t.append("/cloud", style="#d97757")
            t.append(" to connect a cloud model", style="grey50")
        self.query_one("#welcome", Static).update(t)
        self.show_home(True)

    def show_home(self, enabled=False):
        """Toggle welcome page versus conversation without remounting either widget."""
        enabled = bool(enabled)
        if self._home_shown == enabled:
            return
        self._home_shown = enabled
        peek = bool(self.view_sid)   # another tab is shown in place of the live session: keep it there
        with suppress(Exception):
            self.query_one("#welcome", Static).display = enabled and not peek
            self.query_one("#chat", Chat).display = not enabled and not peek
            self.query_one("#peek-wrap").display = peek
            prompt = self.query_one(Prompt)
            prompt.screen.set_class(enabled and not peek, "-home")
            if not peek:
                prompt.placeholder = HOME_PLACEHOLDER if enabled else Prompt.PLACEHOLDER
            self._home_margin = -1   # make center_home recompute the padding
            self.center_home()
            self.refresh_status()

    def center_home(self):
        """Centers the input on the home screen. Screen align centers the block of all widgets as a whole,
        and the tabs there span the full width, so we set the left padding of narrow rows ourselves."""
        ids = ("#inrow", "#modes", "#home-hint", "#slash")
        # width of the screen with the input, not App.size: the terminal doesn't report its size right away at start
        # (Windows Terminal maximizes the window after launch) — hence also once a second from refresh_status
        width = 0
        with suppress(Exception):
            width = self.query_one(Prompt).screen.size.width
        m = max(0, ((width or self.size.width or 80) - 78) // 2) if self._home_shown and not self.view_sid else None
        if m == getattr(self, "_home_margin", None):
            return
        self._home_margin = m
        with suppress(Exception):
            for wid in ids:
                st = self.query_one(wid).styles
                if m is None:
                    st.clear_rule("margin")   # in the chat — padding from CSS again
                else:
                    st.margin = (1 if wid == "#inrow" else 0, 0, 0, m)

    def on_resize(self, event):
        self.center_home()

    def on_paste(self, event):
        """Pasting/dropping a file while focus is not in the input (clicked on the chat, etc.):
        it still lands in the input line, as in Claude Code. Modal windows handle paste themselves."""
        with suppress(Exception):
            prompt = self.query_one(Prompt)
            if self.screen is not prompt.screen or isinstance(self.focused, (Input, TextArea)):
                return
            event.stop()
            prompt.focus()
            prompt.take_paste(event.text)

    async def _approve_cloud_request(self, task, source="parent"):
        """Show the exact task payload before any network request; nothing else is submitted.
        "always" turns the question off (/cloud autosend on), so later tasks go without it."""
        answer = await self.push_screen(CloudApprovalScreen(task, source), wait_for_dismiss=True)
        if answer == "always":
            cloud.set_option("autosend", "on")
            self.notify("Cloud tasks now go without asking. Turn it back on: /cloud autosend off", timeout=6)
        return bool(answer)

    def _log_ui_exception(self, where, exc):
        """Persist a safe traceback so a failed session switch can be diagnosed without closing the CLI."""
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            path = HOME / "ui_errors.log"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {type(exc).__name__}: {exc}\n")
                stream.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            return path
        except Exception:
            return None

    async def open_session_safely(self, sid):
        """Persist and open a session with rollback on invalid/stale history instead of crashing Textual."""
        old_sid = self.sid
        self.save_session()  # crucial: fresh sessions must get a backing JSON before leaving them
        try:
            target_path = session_path(sid)
            if not target_path.exists():
                raise FileNotFoundError(f"Session {sid} has no saved transcript at {target_path}")
            await self.load(sid)
            return True
        except Exception as exc:
            log_path = self._log_ui_exception(f"open session {sid}", exc)
            self.notify(f"Could not open session ({type(exc).__name__}); CLI kept running. Log: {log_path or 'unavailable'}",
                        severity="error", timeout=8)
            if self.sid != old_sid:
                with suppress(Exception):
                    await self.load(old_sid)
            return False

    # ── session tabs / persistent child sessions ──
    def _hydrate_child_sessions(self, parent_sid):
        parent_sid = str(parent_sid or "")
        if not parent_sid or parent_sid in self._hydrated_parent_ids:
            return
        self._hydrated_parent_ids.add(parent_sid)
        # Hydrate only when entering a parent session, not on every 100ms UI tick.
        # Resumed sessions can belong to a different project cwd than this process started in.
        for item in list_sessions(cwd=self.sess_cwd or os.getcwd()):
            path = item.get("path")
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                continue
            if not payload.get("is_subagent") or str(payload.get("parent_sid") or "") != parent_sid:
                continue
            sid = str(payload.get("id") or item.get("id") or "")
            if not sid:
                continue
            state.upsert_child_session(
                sid, id=sid, title=payload.get("title") or item.get("title") or sid,
                parent_sid=parent_sid, task=payload.get("agent_task") or "",
                status=payload.get("agent_status") or "completed", step=payload.get("agent_step") or 0,
                activity=payload.get("agent_activity") or "", report=payload.get("agent_report") or "",
                cwd=payload.get("cwd") or item.get("cwd") or "", path=str(path),
                ts=payload.get("ts") or item.get("ts") or 0, index=payload.get("agent_index"))

    def _live_busy(self):
        """The live session hasn't fully finished: a turn (with the subagents it runs), its own
        queue, or the start of a queued message from another tab."""
        worker = self.worker
        return bool(self.busy or self.queue or self._draining_queue or self._starting_queued
                    or (worker is not None and not worker.is_finished))

    def _tab_label(self, sid):
        """Root tab title: the live session's own title, else the saved one (cached)."""
        label = self.title_ if sid == self.sid else None
        if not label:
            label = self._session_headers.get(sid)
        if not label:
            try:
                payload = json.loads(session_path(sid).read_text(encoding="utf-8"))
                label = (clean_title(payload.get("title")) or payload.get("name")
                         or session_title(payload.get("msgs")) or "New session")
            except Exception:
                label = "New session"
            self._session_headers[sid] = str(label)
        if label == "New session":   # an empty tab with a queued first message: name it by that message
            label = next((text for q, text in self._session_queue if q == sid), label)
        return re.sub(r"\s+", " ", str(label)).strip() or "New session"

    def _session_tab_model(self):
        active_meta = state.CHILD_SESSIONS.get(self.sid) or {}
        parent_sid = str(active_meta.get("parent_sid") or self.sid)
        self._hydrate_child_sessions(parent_sid)
        shown = self.view_sid or self.sid
        busy, queued = self._live_busy(), {q for q, _ in self._session_queue}
        # labels shrink with the window, so a narrow terminal still fits several tabs
        width = self.size.width or 120
        room, child_room = (23, 16) if width >= 120 else (16, 10) if width >= 80 else (10, 6)
        root_ids = []
        for sid in state.OPEN_SESSIONS:
            meta = state.CHILD_SESSIONS.get(sid) or {}
            if not meta.get("parent_sid") and sid not in root_ids:
                root_ids.append(sid)
        if parent_sid and parent_sid not in root_ids:
            root_ids.insert(0, parent_sid)
        if self.sid not in root_ids and not active_meta.get("parent_sid"):
            root_ids.append(self.sid)
        tabs = []
        for sid in root_ids:
            status = "running" if sid == self.sid and busy else "queued" if sid in queued else "open"
            mark = {"running": "⟳ ", "queued": "◷ "}.get(status, "")
            title = mark + self._tab_label(sid)
            tabs.append({"id": sid, "label": title[:room + len(mark)], "full": title[:70], "active": sid == shown,
                         "kind": "root", "status": status, "live": sid == self.sid})
        children = [dict(meta) for meta in state.CHILD_SESSIONS.values() if str(meta.get("parent_sid") or "") == parent_sid]
        # by number, not by last update: a running agent must not jump to the end on every step
        children.sort(key=lambda x: (int(x.get("index") or 0), str(x.get("id") or "")))
        icons = {"queued": "○", "running": "⟳", "completed": "✓", "failed": "✗", "cancelled": "■"}
        for child in children:
            sid = str(child.get("id") or "")
            if not sid:
                continue
            status = str(child.get("status") or "queued")
            agent_no = child.get("index") or 0
            short = re.sub(r"^Agent \d+:?\s*", "", str(child.get("title") or f"Agent {agent_no}"))
            short = re.sub(r"\s+", " ", short).strip()
            head = f"{icons.get(status, '○')} A{agent_no} "
            tabs.append({"id": sid, "label": head + short[:child_room], "full": head + short[:40],
                         "active": sid == shown, "kind": "child", "status": status, "live": False})
        if self.sid not in {x["id"] for x in tabs}:
            tabs.append({"id": self.sid, "label": (self.title_ or "New session")[:room], "full": self.title_ or "New session",
                         "active": self.sid == shown, "kind": "child" if active_meta.get("parent_sid") else "root",
                         "status": active_meta.get("status", "open"), "live": True})
        return tabs

    async def refresh_session_tabs(self, force=False):
        # a forced redraw waits for the one in flight: dropped, it left stale tabs that were then
        # rebuilt by the timer right under the next click, swallowing it
        while force and self._session_tabs_rendering:
            await asyncio.sleep(0.01)
        if self._session_tabs_rendering:
            return
        tabs = self._session_tab_model()
        signature = self._session_tabs_key(tabs)
        if not force and signature == self._session_tabs_signature:
            return
        self._session_tabs_rendering = True
        try:
            try:
                strip = self.query_one("#session-tabs", HorizontalScroll)
                new = self.query_one("#new-session-tab", Button)
            except NoMatches:   # the worker ran until app shutdown: the screen is already gone
                return
            new.label = "+ New session" if (self.size.width or 120) >= 80 else "+"
            busy = self._live_busy()
            await strip.remove_children()
            buttons = []
            for tab in tabs:
                # the shown tab has a colored dot, as in OpenCode
                label = Content.assemble(("• ", "#d97757"), tab["label"]) if tab["active"] else tab["label"]
                button = Button(label, id="session-tab-" + tab["id"], compact=True)
                button.can_focus = False   # clicking a tab doesn't steal focus from the input
                if tab["kind"] == "child":
                    button.tooltip = "Subagent transcript (read-only)"
                elif tab["status"] == "queued":
                    button.tooltip = "Starts when the running session finishes"
                if tab["active"]:
                    button.add_class("active")
                if tab["status"] == "running":
                    button.add_class("running")
                buttons.append(button)
                if tab["kind"] == "root":
                    running = tab["live"] and busy   # the running session can't be closed under the agent
                    close = Button("✕", id="close-session-" + tab["id"], disabled=running, compact=True)
                    close.can_focus = False
                    close.add_class("close-tab")
                    close.tooltip = ("Running — wait for it to finish" if running else
                                     "Close tab (the session stays in /history)")
                    buttons.append(close)
            await strip.mount(*buttons)
            # after layout, otherwise there are no sizes yet and scrolling won't work
            self.call_after_refresh(self._scroll_active_tab)
            self._session_tabs_signature = signature
        finally:
            self._session_tabs_rendering = False

    def _maybe_refresh_session_tabs(self):
        if self._session_tabs_rendering:
            return
        tabs = self._session_tab_model()
        if self._session_tabs_key(tabs) != self._session_tabs_signature:
            self.run_worker(self.refresh_session_tabs(), group="session-tabs", exclusive=False)

    def _scroll_active_tab(self):
        """The shown tab is always visible in the strip (after a redraw and after the window narrows)."""
        with suppress(NoMatches):
            strip = self.query_one("#session-tabs", HorizontalScroll)
            # the shown tab's ✕ first, then the tab itself: in a narrow window the start of its title wins
            for selector in (f"#close-session-{self.view_sid or self.sid}", "Button.active"):
                target = next(iter(strip.query(selector)), None)
                if target:
                    strip.scroll_to_widget(target, animate=False)
        self._update_tab_count()

    def _update_tab_count(self):
        """The counter next to "+ New session": every tab, and how many the strip can't fit right now
        (a narrowed window used to hide new tabs without a trace)."""
        with suppress(NoMatches):
            strip = self.query_one("#session-tabs", HorizontalScroll)
            count = self.query_one("#tab-count", Button)
            tabs = [b for b in strip.query(Button) if str(b.id or "").startswith("session-tab-")]
            left = strip.scroll_offset.x
            right = left + strip.scrollable_content_region.width
            hidden = sum(1 for b in tabs if b.virtual_region.x < left or b.virtual_region.right > right)
            total = len(tabs)
            if (self.size.width or 120) >= 80:
                text = f"▤ {total} tab{'s' if total != 1 else ''}" + (f" · {hidden} hidden" if hidden else "")
            else:
                text = f"▤{total}" + (f" +{hidden}" if hidden else "")
            if str(count.label) != text:
                count.label = text
                count.refresh(layout=True)   # width: auto must follow the new label
            count.set_class(bool(hidden), "hidden-tabs")
            count.tooltip = "All open tabs" + (f" — {hidden} don't fit the window" if hidden else "")

    def _session_tabs_key(self, tabs):
        return (tuple((x["id"], x["label"], x["active"], x["status"]) for x in tabs)
                + (("busy", self._live_busy()), ("narrow", (self.size.width or 120) < 80)))

    async def close_session_tab(self, sid):
        """✕ on a tab: close the tab. The session itself stays on disk — /history opens it again
        (deleting is only there, Del twice). A message still queued for the tab doesn't start, but it
        stays in the session file and returns to the input when the session is opened.
        Any tab can be closed while the agent works, except the session it's running in."""
        if sid == self.sid and self._live_busy():
            self.notify("The agent is working in this session; wait for it to finish before closing it.", severity="warning")
            return
        self._session_queue = [x for x in self._session_queue if x[0] != sid]
        if self.view_sid == sid:
            await self.leave_peek()
        if sid == self.sid:
            others = [x["id"] for x in self._session_tab_model() if x["kind"] == "root" and x["id"] != sid]
            if others:
                await self.open_session_safely(others[-1])
            else:
                await self.command("/new")
            if self.sid == sid:   # switching failed — the tab stays
                await self.refresh_session_tabs(force=True)
                return
        with suppress(ValueError):
            state.OPEN_SESSIONS.remove(sid)
        self._session_headers.pop(sid, None)
        await self.refresh_session_tabs(force=True)

    async def on_button_pressed(self, event):
        button_id = str(event.button.id or "")
        if button_id.startswith("close-session-"):
            await self.close_session_tab(button_id[len("close-session-"):])
        elif button_id == "new-session-tab":
            await self.action_new_session()
        elif button_id == "tab-count":
            self.pick_tab()
        elif button_id.startswith("session-tab-"):
            await self.show_tab(button_id[len("session-tab-"):])

    def pick_tab(self):
        """Every tab in a list: the way to reach the ones a narrow window can't fit."""
        options = []
        for tab in self._session_tab_model():
            label = ("    ◆ subagent " if tab["kind"] == "child" else "") + tab["full"]
            if tab["live"]:
                label += "   (agent working)" if tab["status"] == "running" else "   (current)"
            option = TabChoice(label + ("   ← shown" if tab["active"] else ""))
            option.sid = tab["id"]
            options.append(option)

        async def chosen(option):
            if option is not None and getattr(option, "sid", None):
                await self.show_tab(option.sid)
        # a button handler is not a worker: only the callback form of push_screen
        self.push_screen(Pick(f"Open tabs: {len(options)}", options, custom=False), chosen)

    async def show_tab(self, sid):
        """A tab was chosen. A subagent tab is always a read-only transcript; another session opens
        for real when the live one is idle, otherwise it's shown in its place and what is typed
        there waits in the queue."""
        sid = str(sid)
        if sid == self.sid:
            await self.leave_peek()
            return
        if (state.CHILD_SESSIONS.get(sid) or {}).get("parent_sid") or self._live_busy():
            await self.peek(sid)
            return
        await self.leave_peek()
        await self.open_session_safely(sid)

    async def action_new_session(self):
        """Ctrl+N / "+ New session". While the agent works, the new tab is shown next to it and its
        first message starts once the running session finishes."""
        if not self._live_busy():
            await self.leave_peek()
            await self.command("/new")
            await self.refresh_session_tabs(force=True)
            return
        sid = new_session_id()
        with suppress(Exception):
            write_json(session_file(sid, self.sess_cwd),
                       {"id": sid, "title": "(untitled)", "name": "", "ts": time.time(), "cwd": str(self.sess_cwd or Path.cwd()),
                        "msgs": [], "archive": [], "draft": "", "queued": [], "estimated_cost": 0.0})
        state.track_open_session(sid)
        await self.peek(sid)

    async def _switch_session_by(self, direction):
        ids = [x["id"] for x in self._session_tab_model()]
        if len(ids) < 2:
            return
        try:
            index = ids.index(self.view_sid or self.sid)
        except ValueError:
            index = 0
        await self.show_tab(ids[(index + direction) % len(ids)])

    async def action_previous_session(self):
        await self._switch_session_by(-1)

    async def action_next_session(self):
        await self._switch_session_by(1)

    # ── another tab shown in place of the live session ──
    @property
    def shown_chat(self):
        """The chat on screen: the live session's, or the tab shown instead."""
        return self.query_one("#peek", Chat) if self.view_sid else self.chat

    def _apply_view(self):
        home, self._home_shown = self._home_shown, None
        self.show_home(bool(home))

    async def peek(self, sid):
        """Show another tab without touching the live session: its turn keeps running underneath."""
        if not self.view_sid:
            self._main_draft = self._draft()   # read before view_sid is set: it is still in the input
        child = bool((state.CHILD_SESSIONS.get(sid) or {}).get("parent_sid"))
        self.view_sid = sid
        self._peek_n, self._peek_cards, self._peek_key, self._peek_new = 0, {}, None, True
        self._peek_live = None
        with suppress(NoMatches):
            await self.query_one("#peek", Chat).remove_children()
            prompt = self.query_one(Prompt)
            prompt.text = ""
            prompt.read_only = False
            prompt.placeholder = ("Subagent transcript · /btw question — ask this subagent on the side · Esc — back" if child else
                                  "Message for this session — it starts when the running session finishes · Esc — back")
            self.query_one("#slash").display = False
            self._apply_view()
            await self.refresh_peek(force=True)
            prompt.focus()
        await self.refresh_session_tabs(force=True)

    async def leave_peek(self):
        """Back to the live session (Esc, its tab, or the shown tab became the live one)."""
        if not self.view_sid:
            return
        self.view_sid = None
        self._peek_live = None
        with suppress(NoMatches):
            prompt = self.query_one(Prompt)
            prompt.read_only = False
            prompt.text = self._main_draft
            prompt.move_cursor(prompt.document.end)
            await self.query_one("#peek", Chat).remove_children()
            self._apply_view()
            self.chat.scroll_end(animate=False)
            prompt.focus()
        self._main_draft = ""
        await self.refresh_session_tabs(force=True)

    def action_back(self):
        """Esc: leave a shown tab first; only then does Esc interrupt the agent."""
        if self.view_sid and len(self.screen_stack) == 1:
            self.run_worker(self.leave_peek(), group="peek")
            return
        self.action_cancel()

    def _peek_head(self, sid, payload, meta, child):
        title = clean_title(payload.get("title") or meta.get("title")) or "New session"
        live = self.title_ or "New session"
        t = Text()
        if child:
            status = str(meta.get("status") or payload.get("agent_status") or "queued")
            color = {"running": "#d97757", "completed": "#98c379", "failed": "#e06c75"}.get(status, "grey62")
            icon = {"queued": "○", "running": "⟳", "completed": "✓", "failed": "✗", "cancelled": "■"}.get(status, "○")
            t.append(f"◆ Subagent A{meta.get('index') or payload.get('agent_index') or '?'}", style="bold #b48ead")
            t.append(f"  {icon} {status}", style=f"bold {color}")
            step = meta.get("step") or payload.get("agent_step") or 0
            if step:
                t.append(f" · step {step}", style="grey62")
            activity = str(meta.get("activity") or payload.get("agent_activity") or "")
            if activity:
                t.append(f" · {activity[:80]}", style="grey62")
            if meta.get("cache"):   # the last request: how much of its prompt the server's cache served
                t.append(f" · {meta['cache']}", style="grey50")
            t.append(f"\nlaunched by «{live[:40]}» · /btw question — side question to this subagent · Esc — back",
                     style="grey50")
        else:
            waiting = sum(1 for q, _ in self._session_queue if q == sid)
            t.append(f"◻ {title[:60]}", style="bold #c9d1d9")
            if self._live_busy():
                t.append(f"\n⟳ agent busy in «{live[:40]}» — messages here wait{f' ({waiting})' if waiting else ''}"
                         " and start when it fully finishes · Esc — back", style="grey50")
            else:
                t.append("\nstarting…", style="grey50")
        with suppress(NoMatches):
            head = self.query_one("#peek-head", Chrome)
            head.set_class(not child, "root")
            head.update(t)

    async def refresh_peek(self, force=False):
        """Bring the shown tab up to date: new messages are appended, tool cards finish in place."""
        sid = self.view_sid
        if not sid:
            return
        meta = dict(state.CHILD_SESSIONS.get(sid) or {})
        path = Path(meta.get("path") or session_path(sid))
        key = None
        with suppress(OSError):
            key = path.stat().st_mtime_ns
        if not force and key == self._peek_key:
            await self._peek_live_view(meta)
            return
        self._peek_key = key
        payload = {}
        with suppress(Exception):
            payload = json.loads(path.read_text(encoding="utf-8"))
        child = bool(payload.get("is_subagent") or meta.get("parent_sid"))
        self._peek_head(sid, payload, meta, child)
        archive = [m for m in payload.get("archive") or [] if isinstance(m, dict)]
        shown = archive + [m for m in payload.get("msgs") or [] if isinstance(m, dict)]
        peek = self.query_one("#peek", Chat)
        if len(shown) < self._peek_n:   # the child compacted its history: draw it again
            await peek.remove_children()
            self._peek_n, self._peek_cards, self._peek_new = 0, {}, True
        results = {m.get("tool_call_id"): m.get("content") for m in shown if m.get("role") == "tool"}
        status = str(meta.get("status") or payload.get("agent_status") or "")
        running = child and status in ("running", "queued")
        for cid, card in list(self._peek_cards.items()):
            if cid in results or not running:
                card.finish(cid in results, results.get(cid, "(no result: the subagent stopped)"))
                del self._peek_cards[cid]
        agent = (meta.get("index") or payload.get("agent_index") or "?") if child else None
        ws = self._transcript(shown[self._peek_n:], archived=len(archive) if not self._peek_n else 0,
                              agent=agent, results=results, cards=self._peek_cards if running else None)
        if self._peek_new:   # once per drawing: the messages already waiting for this tab
            self._peek_new = False
            for q, text in self._session_queue:
                if q == sid:
                    ws += self._queued_echo(text)
            if not ws and not child:
                ws.append(SelStatic(Text("Empty session — type the first message below.", style="grey50"), classes="note"))
        self._peek_n = len(shown)
        if ws:
            if self._peek_live is not None:   # the reply being written stays below the saved ones
                live_w, self._peek_live = self._peek_live, None
                await live_w.remove()
            follow = peek.follow
            await peek.mount_all(ws)
            if follow:
                peek.scroll_end(animate=False)
        await self._peek_live_view(meta if running else {})

    async def _peek_live_view(self, meta):
        """The reply a running subagent is writing now (it is saved to its session only when the step ends)."""
        think, text = str(meta.get("live_think") or ""), split_think(str(meta.get("live_text") or ""))[1]
        w = self._peek_live
        if not (think.strip() or text.strip()):
            if w is not None:
                self._peek_live = None
                await w.remove()
            return
        body = Text(think.strip(), style="italic grey62")
        if text.strip():
            body.append("\n\n" + text.strip(), style="#c9d1d9")
        peek = self.query_one("#peek", Chat)
        if w is None:
            inner = SelStatic(body)
            w = self._peek_live = Collapsible(inner, title="✻ Reasoning · live", collapsed=False, classes="sub")
            w.inner = inner
            follow = peek.follow
            await peek.mount_all([w])
            if follow:
                peek.scroll_end(animate=False)
        else:
            w.inner.update(body)
            if peek.follow:
                peek.scroll_end(animate=False)

    def _queued_echo(self, text):
        return [SelStatic(Text("❯ " + text), classes="user"),
                SelStatic(Text("◷ queued — starts when the running session fully finishes", style="grey50"), classes="note")]

    async def queue_for_tab(self, text):
        """Enter in a shown tab: the message waits for the live session to finish, then this tab
        becomes the live one and the message is sent as if typed there."""
        sid = self.view_sid
        if (state.CHILD_SESSIONS.get(sid) or {}).get("parent_sid"):
            self.notify("A subagent transcript is read-only. Esc — back to the parent session.", severity="warning")
            return
        self.query_one(Prompt).text = ""
        self._session_queue.append((sid, text))
        self._file_queue(sid, text, add=True)
        peek = self.query_one("#peek", Chat)
        await peek.mount_all(self._queued_echo(text))
        peek.scroll_end(animate=False)
        self._peek_key = None   # redraw the header: its queued count changed
        await self.refresh_peek()
        self._kick_session_queue()
        await self.refresh_session_tabs(force=True)

    @staticmethod
    def _file_queue(sid, text, add):
        """Mirror a message queued for another tab in that session's file ("queued"): if the CLI or the tab
        is closed before it starts, opening the session later puts it back into the input."""
        with suppress(Exception):
            f = session_path(sid)
            d = json.loads(f.read_text(encoding="utf-8"))
            queued = [str(x) for x in d.get("queued") or []]
            if add:
                queued.append(text)
            elif text in queued:
                queued.remove(text)
            d["queued"] = queued
            write_json(f, d)

    def _kick_session_queue(self):
        if self._session_queue and not self._live_busy():
            self.run_worker(self.drain_session_queue(), group="session-queue", exclusive=True)
        elif not self._session_queue and self.view_sid and not self._live_busy() \
                and not (state.CHILD_SESSIONS.get(self.view_sid) or {}).get("parent_sid"):
            # the agent finished while another session was shown: that session is simply opened now
            self.run_worker(self.show_tab(self.view_sid), group="session-queue", exclusive=True)

    async def drain_session_queue(self):
        """Start the first message queued in another tab: open that session, send the message."""
        if self._live_busy() or not self._session_queue:
            return
        sid, text = self._session_queue.pop(0)
        # Busy while the session opens: peek_tick (every 0.5 s) saw an empty queue and an idle agent here and
        # started show_tab in this exclusive worker group, which cancelled this worker - on a slow PC the
        # message was lost (gone from the queue, the session file and the input).
        self._starting_queued, started = True, False
        try:
            self._file_queue(sid, text, add=False)   # it is sent now: opening the session must not restore it too
            if sid != self.sid and not await self.open_session_safely(sid):
                started = True                       # handled: it goes back into the input
                self.restore_input([text])
                self.notify("The queued message could not start: its session didn't open. It is back in the input.",
                            severity="error")
                return
            if self.view_sid == sid:
                await self.leave_peek()
            await self.on_prompt_submitted(None, queued=text)
            started = True
        finally:
            self._starting_queued = False
            if not started:                          # cancelled before it was sent: keep it queued
                self._session_queue.insert(0, (sid, text))
                self._file_queue(sid, text, add=True)
        await self.refresh_session_tabs(force=True)

    async def peek_tick(self):
        self._kick_session_queue()
        if self.view_sid and not self._peek_busy:
            self._peek_busy = True
            try:
                await self.refresh_peek()
            finally:
                self._peek_busy = False

    def _transcript(self, shown, archived=0, agent=None, results=None, cards=None, live=False):
        """Widgets for saved messages: a restored session (live=True also restores its task list)
        or a shown tab. agent — the subagent's number: its messages carry a violet mark so they
        are never taken for the parent's. cards — tool cards still waiting for a result go there."""
        if results is None:
            results = {m.get("tool_call_id"): m.get("content") for m in shown if m.get("role") == "tool"}
        sub = " sub" if agent is not None else ""
        out = []
        for i, m in enumerate(shown):
            role = m.get("role")
            if i == archived and archived:
                out.append(SelStatic(Text(f"— above: {archived} messages compacted earlier; "
                                          "the model now works from the summary —", style="grey50"), classes="note"))
            if compact.summary_text(m):
                out.append(self.summary_view(compact.summary_text(m), collapsed=True))
            elif role == "user" and agent is not None:
                task = delegated_task(m.get("content"))
                if task:
                    head = "▸ task from the parent agent\n" if i == 0 else "▸ parent → subagent\n"
                    out.append(SelStatic(Text.assemble((head, "bold #b48ead"), (task, "#c9d1d9")), classes="user sub"))
            elif role == "user" and (typed := visible_user_text(m.get("content"))):
                out.append(SelStatic(Text("❯ " + typed), classes="user"))
            elif role == "assistant":
                tt, vis = split_think(m.get("content") or "")
                think = (str(m.get("reasoning_content") or "") + tt).strip()
                if think and agent is not None:   # the subagent's reasoning, folded like the main chat's
                    out.append(Collapsible(SelStatic(Text(think, style="italic")), title="✻ Reasoning",
                                           collapsed=True, classes="sub"))
                if vis.strip():
                    if agent is not None:
                        out.append(Chrome(Text(f"◆ subagent A{agent}", style="bold #b48ead"), classes="sub-tag"))
                    out.append(SelStatic(Markdown(chat_md(vis.strip())), classes="ans" + sub))
                for c in m.get("tool_calls") or []:
                    with suppress(Exception):
                        name, args = c["function"]["name"], json.loads(c["function"]["arguments"] or "{}")
                        if name == "todo_write" and live:
                            self.todos = normalize_todos(args.get("todos") or [])
                            if self.progress_w:
                                self.progress_w.set_tasks(self.todos)
                            w = self.todo_w = TodoPanel()
                            w.update_list(self.todos)
                            w.finish(True, results.get(c["id"]))
                            out.append(w)
                            continue
                        card = Card(name, args)
                        edit = (m.get("edit_changes") or {}).get(c.get("id"))
                        if edit:   # the diff the edit really made; the card alone reads the disk as it is now
                            card.disk_change(*edit)
                        if agent is not None:
                            card.add_class("sub")
                        if cards is not None and c.get("id") not in results:
                            cards[c["id"]] = card   # still running: finished when its result arrives
                        else:
                            card.finish(True, results.get(c.get("id")))
                        out.append(card)
                        for kind, path, old, new in (m.get("file_changes") or {}).get(c.get("id")) or []:
                            out.append(ChangeCard(name, kind, path, old, new))
                if m.get("turn_time"):
                    out.append(SelStatic(Text(m["turn_time"], style="grey50"), classes="note"))
        return out

    # ── state / status ──
    def cur_mode(self):
        if S["mode"] == "plan":
            return "plan"
        if S["mode"] == "accept-edits":
            return "accept-edits"
        return "auto" if S["auto"] else "manual"

    def refresh_status(self):
        self.center_home()
        n_mcp = sum(1 for x in self.mcp.srv.values() if x["client"])
        # one number instead of two inconsistent ones: it used to be "(0 · 29% ctx)" — 0 from the server, 29% from the estimate
        used = compact.used_tokens(self.est_tokens(), S["ptok"] or S["ctx"], self.saved_tok, self.est_sent())
        left = (f"{S['model']} · {ktok(used)} / {ktok(CTX_WINDOW)} ctx ({self.fill_pct()}%)"
                + (f" · MCP {n_mcp}/{len(self.mcp.srv)}" if self.mcp.srv else ""))
        right = f"● {S['effort'] or 'medium'} · /effort"
        self.query_one("#status", Static).update(
            split_row(Text(left, style="grey62"), Text(right, style="grey50"), Text(os.getcwd(), style="grey62")))
        cur = self.cur_mode()
        a = Text()
        c = MODE_COLORS[cur]
        if self._home_shown:
            # line inside the input on the home screen: mode · model · context · effort
            a.append(dict(MODES)[cur].removesuffix(" mode").capitalize(), style=f"bold {c}")
            a.append(" · ", style="grey42")
            a.append(str(S["model"]), style="bold #e0e0e0")
            a.append(f" {CTX_WINDOW // 1000}k ctx", style="grey50")
            a.append(" · ", style="grey42")
            a.append(S["effort"] or "medium", style="bold #b69cff" if S["effort"] == "ultra" else "bold #d97757")
            self.query_one("#modes", Static).update(a)
            branch = git_branch()
            place = os.getcwd() + (f":{branch}" if branch else "")
            keys = Text()
            for key, what in HOTKEYS[1:]:   # the mode is already visible in the input
                keys.append(("  " if keys else "") + key, style="bold #e0e0e0")
                keys.append(" " + what, style="grey50")
            self.query_one("#home-hint", Static).update(split_row(Text(place, style="grey50"), keys))
            return
        a.append("⏵⏵ ", style=f"bold {c}")
        a.append(dict(MODES)[cur], style=f"bold {c}")
        a.append(" (shift+tab to cycle)", style="grey50")
        # cheat sheet on the right: the most needed keys always visible (in a narrow window it gets cut, not the mode)
        keys = Text()
        for key, what in HOTKEYS[1:]:
            keys.append(("   " if keys else "") + key, style="bold #c9d1d9")
            keys.append(" " + what, style="grey50")
        self.query_one("#modes", Static).update(split_row(a, keys))

    def paste_image(self):
        try:
            from PIL import ImageGrab
            im = ImageGrab.grabclipboard()
        except Exception as ex:
            return self.notify(f"Pillow is required (pip install pillow): {ex}", severity="error")
        if isinstance(im, list):  # files copied in Explorer
            paths = [p for p in im if re.search(r"\.(png|jpe?g|webp|gif)$", p, re.I)]
            if not paths:
                return self.notify("No image in the clipboard", severity="warning")
            ins = " ".join(f'"{p}"' for p in paths)
        elif im is None:
            return self.notify("No image in the clipboard", severity="warning")
        else:  # screenshot or image copied from a browser
            d = HOME / "clips"; d.mkdir(parents=True, exist_ok=True)
            f = d / f"clip_{int(time.time())}.png"
            im.save(f)
            ins = f'"{f}"'
        self.query_one(Prompt).insert(ins + " ")

    def set_mode(self, m):
        if m == "plan" and S["mode"] != "plan":
            S["pre_plan"] = self.cur_mode()   # leaving plan returns here, not to manual
        S["mode"] = m if m in ("plan", "accept-edits") else "act"
        S["auto"] = m == "auto"
        self.refresh_status()

    def cycle_mode(self, step=1):
        self.set_mode(CYCLE[(CYCLE.index(self.cur_mode()) + step) % len(CYCLE)])

    def set_busy(self, label):
        if self.busy is None and label:
            self.t0 = time.monotonic()
            self.verb = VERBS[int(self.t0 * 7) % len(VERBS)]
        self.busy = label

    def _quiet(self, fn):
        with suppress(NoMatches):
            fn()

    def tick(self):
        self.frame += 1
        self._maybe_refresh_session_tabs()
        bar = self.query_one("#busy", Static)
        if not self.busy:
            bar.update("")
            return
        t = Text()
        t.append(SPIN[self.frame % 10], style="bold #d97757")
        t.append(" " + (self.verb + "…" if self.busy == "Thinking…" else self.busy), style="bold")
        t.append(f" · {dur(time.monotonic() - self.t0)}", style="grey62")
        r = self._retract
        hint = ("Esc — take back the queued message" if self._has_pending() else
                "Esc — edit the message" if r and not r["answered"] else "Esc — interrupt")
        t.append(f"  ({hint})", style="grey50")
        if self.queue:
            t.append(f" · queued {len(self.queue)}", style="grey62")
        bar.update(t)

    # ── selection → clipboard ──
    def sync_clip(self):
        """Copy a stable mouse selection without changing terminal layout mid-drag."""
        with suppress(Exception):
            if not self.screen.selections:
                self._seen = ""
                self._seen_t = 0.0
                return
            text = self.screen.get_selected_text() or ""
            if text != self._seen:
                self._seen, self._seen_t = text, time.monotonic()
            elif text and text != self.clipped and time.monotonic() - self._seen_t >= 0.65:
                # The copied indicator occupies a permanently reserved one-line row;
                # updating it no longer shifts the coordinates of an active selection.
                self.copy_now(text, preview=False)

    def copy_now(self, text, preview=True):
        if not text:
            return
        self.clipped = text
        self.copy_to_clipboard(text)
        # clip.exe is a process start: run it off the UI thread so a mouse-up copy does not freeze the screen.
        threading.Thread(target=to_clip, args=(text,), daemon=True).start()
        bar = self.query_one("#copied", Static)
        # Fixed single line; never include a multi-line preview that changes layout.
        bar.update(Text("✓ copied to clipboard · %d characters" % len(text), style="#9ece6a"))
        if self._bar_timer:
            self._bar_timer.stop()
        self._bar_timer = self.set_timer(3, self.hide_bar)

    def hide_bar(self):
        # Do not set display=False: adding/removing a row can reset a selection.
        for bar in self.query("#copied"):     # the timer may fire after the screen changed: no bar, no error
            bar.update("")

    def action_copy_selection(self):
        """Ctrl+Shift+C copies the active selection, or the visible chat if none exists."""
        with suppress(Exception):
            selected = self.screen.get_selected_text() or ""
        if selected.strip():
            self.copy_now(selected, preview=False)
            return
        text = self.chat_text()
        if text:
            self.copy_now(text, preview=False)
        else:
            self.notify("Nothing to copy", severity="warning")

    def chat_text(self, last=False):
        """The whole visible chat as plain text: messages, replies, commands and their output, without frames or reasoning."""
        ws = [w for w in self.chat.children if isinstance(w, SelStatic) and w._sel_text.strip()]
        if last:
            ws = [w for w in ws if w.has_class("ans")][-1:]
        return "\n\n".join(w._sel_text.strip("\n") for w in ws)

    async def on_text_selected(self, e):
        """Mouse button released: copy right away, as in the chat window. Otherwise a click in the input
        right after selecting cleared it before the sync_clip timer managed to copy."""
        with suppress(Exception):
            text = (self.screen.get_selected_text() or "") if self.screen.selections else ""
            if text.strip():
                self._seen, self._seen_t = text, time.monotonic()
                self.copy_now(text, preview=False)

    def on_sel_static_link_clicked(self, e):
        # Textual names the handler after the nested message (SelStatic.LinkClicked).
        """An address the agent wrote was clicked in the chat: opening it always asks first."""
        self.push_screen(LinkScreen(e.url), lambda ok: self.open_link(e.url, ok))

    def open_link(self, url, ok):
        """Only after the user confirmed does the desktop browser get the address."""
        if not ok:
            return
        if open_url(url):
            self.notify(f"Opened {url}", severity="information")
        else:
            self.notify("No browser could open that address", severity="warning")

    async def put(self, w):
        self.show_home(False)
        follow = self.chat.follow
        await self.chat.mount(w)
        if follow:
            self.chat.scroll_end(animate=False)
        return w

    def update_notes(self):
        """Auto-update (maxedcode/update.py): a version installed at this start, then the daily look at GitHub in
        the background; a newer one downloaded installs at the next start."""
        from .. import update
        done = update.take_notice()
        if done:
            self.run_worker(self.note(f"maxedcode was updated to {done}", "green"))

        def ready(st):
            self.call_from_thread(lambda: self.run_worker(self.note(
                f"maxedcode {st['ready']} is downloaded: it installs at the next start (maxed --update: now)", "cyan")))
        update.check_async(ready)

    async def note(self, text, style="grey62"):
        """Service output to the chat. The first output of a slash command gets a "❯ /command" header,
        so it's clear which command it belongs to; harsh ANSI colors are replaced with soft ones."""
        body = text.copy() if isinstance(text, Text) else Text(strip_ansi(str(text)), style=NOTE_STYLES.get(style, style))
        echo, self._echo = getattr(self, "_echo", None), None
        if echo:
            body = Text.assemble(("❯ ", "#d97757"), (echo, "bold #8a9099"), "\n", body)
        return await self.put(SelStatic(body, classes="note"))

    # ── slash menu ──
    def slash_open(self):
        return self.query_one("#slash").display

    def on_text_area_changed(self, e):
        t, sl = e.text_area.text, self.query_one("#slash", OptionList)
        if t.startswith("/") and " " not in t and "\n" not in t:
            m = [(k, v) for k, v in CMDS.items() if k.startswith(t)]
            sl.clear_options()
            sl.add_options([Option(f"{k:<10} {v}", id=k) for k, v in m])
            sl.display = bool(m)
            if m:
                sl.highlighted = 0
        else:
            sl.display = False

    async def slash_accept(self, submit):
        sl, p = self.query_one("#slash", OptionList), self.query_one(Prompt)
        if sl.highlighted is None:
            return
        p.text = sl.get_option_at_index(sl.highlighted).id + ("" if submit else " ")
        p.move_cursor(p.document.end)
        sl.display = False
        if submit:
            # on_prompt_submitted is async: calling it without await only created a coroutine
            # and nothing ran, so Enter on a slash command did nothing
            await self.on_prompt_submitted(None)

    def recall(self, d):
        p = self.query_one(Prompt)
        n = self.hpos + d
        if not self.hist or n < 0 or n > len(self.hist):
            return False
        if self.hpos == len(self.hist):
            self.draft = p.text
        self.hpos = n
        p.text = self.hist[n] if n < len(self.hist) else self.draft
        p.move_cursor(p.document.end)
        return True

    # ── input ──
    async def on_prompt_submitted(self, _, queued=None):
        """Enter in the input. queued — a message typed earlier in another tab, sent now that its
        session became the live one (the input itself is left alone)."""
        p = self.query_one(Prompt)
        if queued is None and self.view_sid and (state.CHILD_SESSIONS.get(self.view_sid) or {}).get("parent_sid"):
            text = p.text.strip()
            q = text[len("/btw"):].strip() if re.match(r"/btw(\s|$)", text) else None
            if q:
                p.text = ""
                self.run_worker(self.child_btw(self.view_sid, q), exclusive=False)
            elif text:
                self.notify("A subagent tab takes only /btw question — a side question to this subagent.",
                            severity="warning")
            return
        if queued is None and self.view_sid:
            text = p.text.strip()
            if text:
                await self.queue_for_tab(text)
            return
        if queued is not None:
            p = _Detached()
        text = (queued if queued is not None else p.text).strip()
        if not text:
            return
        self.chat.follow = True
        if text.startswith("/") and "\n" not in text:
            p.text = ""
            self.run_worker(self.command(text), exclusive=False)
            return
        if text.startswith("!") and not self.busy:
            command = text[1:].strip()
            p.text = ""
            if not command:
                await self.note(f"Shell mode: type ! followed by a {TITLES.get(SHELL_TOOL, SHELL_TOOL)} command. Esc clears it without running.", "grey50")
                return
            self.hist.append(text); self.hpos = len(self.hist)
            self.run_worker(self.run_shell_input(command, original=text), exclusive=True)
            return
        if text.startswith("#") and not self.busy:
            # quick note → memory (type project by default, /memory for the rest)
            p.text = ""
            f, line = memory.note(text[1:].strip(), "project")
            if f:
                self.hist.append(text)
                await self.note(f"{line}\n→ {f}", "cyan")
            return
        original_text = text
        text = await self.expand_attachments(text)
        h = await hooks.run("UserPromptSubmit", {"session_id": self.sid, "cwd": os.getcwd(),
                                                 "event": "UserPromptSubmit", "prompt": text})
        if h["decision"] == "deny":
            p.text = ""
            await self.note(h["message"].strip() or "blocked by hook", "red")
            return
        if h["message"]:
            await self.note(h["message"].strip(), "cyan")
        self.hctx = h["context"]
        p.text = ""
        self.hist.append(text); self.hpos = len(self.hist)
        with suppress(Exception), HIST_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"p": text}, ensure_ascii=False) + "\n")
        if self.busy:
            if self.queue_input:
                target = self.queue
                self.queue_input = False
                label = f"queued for the next turn ({len(self.queue) + 1} waiting) · Esc — take it back"
            else:
                target = self.steer_queue
                label = "steer added to the active turn; it will be read after the current tool batch · Esc — take it back"
            target.append(text)
            ws = [await self.put(SelStatic(Text("❯ " + original_text), classes="user")), await self.note(label, "grey50")]
            # until the model reads the message, Esc returns it to the input (take_back_pending)
            self._pending_input.append({"q": target, "text": text, "orig": original_text, "w": ws})
            self.save_session(index=False)   # a queued message survives a closed window
            return
        # turn() echoes the message itself: echoing here too would show it twice
        self.worker = self.run_worker(self.turn(text, shown=original_text if text != original_text else None,
                                                retract=original_text), exclusive=True)

    def open_editor(self):
        """Ctrl+G / Alt+G in the input: a large editor with the text typed so far; Ctrl+S returns it to the input."""
        prompt = self.query_one(Prompt)

        def done(draft):
            if draft is not None:
                prompt.text = draft
                prompt.move_cursor(prompt.document.end)
            prompt.focus()
        # a key handler is not a worker: only the callback form of push_screen
        self.push_screen(EditorScreen(prompt.text), done)

    def action_command_palette(self):
        # a Binding action is not a worker: wait_for_dismiss=True fails here with NoActiveWorker
        self.push_screen(CommandPalette(), self._insert_command)

    async def _insert_command(self, selected):
        """The palette returned (how, command): run — execute right away, insert — insert to add arguments."""
        if not selected:
            return
        how, cmd = selected if isinstance(selected, tuple) else ("insert", selected)
        prompt = self.query_one(Prompt)
        prompt.text = cmd + ("" if how == "run" else " ")
        prompt.move_cursor(prompt.document.end)
        prompt.focus()
        if how == "run":
            await self.on_prompt_submitted(None)

    def action_cancel(self):
        """Esc. First takes back messages the model hasn't read yet (the agent doesn't
        stop); if there are none and the model hasn't started answering — rolls back the turn and
        returns the message to the input; otherwise just interrupts."""
        if not self.busy:
            return
        texts = self.take_back_pending()
        if texts:
            self.restore_input(texts)
            self.notify("Message returned to the input · the agent keeps working", timeout=2)
            return
        r = self._retract
        if r and not r["answered"]:
            r["go"] = True
        # any active worker: a turn, a command worker (e.g. /run), a subagent
        for w in list(self.workers):
            w.cancel()

    @staticmethod
    def _pull(q, item):
        """Remove exactly this object from the queue (identical texts may be queued twice)."""
        for i, x in enumerate(q):
            if x is item:
                del q[i]
                return True
        return False

    def _drop_widgets(self, ws):
        for w in ws:
            if w is not None and w.is_attached:
                w.remove()

    def take_back_pending(self, drained=False):
        """Messages typed during work and not yet read by the model: take them out of the queues,
        remove from the chat, return the original texts. drained=True — also a steer already included in
        the turn being rolled back (its message is removed together with the turn)."""
        texts, keep = [], []
        for e in self._pending_input:
            if self._pull(e["q"], e["text"]) or (drained and e["q"] is self.steer_queue):
                texts.append(e["orig"])
                self._drop_widgets(e["w"])
            else:
                keep.append(e)
        self._pending_input = keep
        return texts

    def restore_input(self, texts):
        """Return the texts to the input, before what is already typed there."""
        p = self.query_one(Prompt)
        parts = [t for t in texts if t and t.strip()] + ([p.text] if p.text.strip() else [])
        p.text = "\n".join(parts)
        p.move_cursor(p.document.end)
        p.focus()

    def retract_turn(self):
        """Esc before the model answers: the whole turn is rolled back, the message returns to the input.
        False — can't roll back (the model already answered or the history was rebuilt by compaction)."""
        r = self._retract
        if not (r and r["go"] and not r["answered"]):   # the answer may have started between Esc and the cancel
            return False
        msg = r.get("msg")
        if msg is not None:
            idx = next((i for i in range(len(self.msgs) - 1, -1, -1) if self.msgs[i] is msg), None)
            if idx is None:
                return False
            del self.msgs[min(idx, r.get("n", idx)):]
        del self.marks[r["marks"]:]
        self.title_ = r["title"]
        texts = [r["text"]] + self.take_back_pending(drained=True)
        kids = list(self.chat.children)
        echo = r.get("echo")
        self._drop_widgets((kids[kids.index(echo):] if echo in kids else []) + r["w"])
        self.restore_input(texts)
        self.notify("Message returned to the input", timeout=2)
        return True

    def _has_pending(self):
        return any(any(x is e["text"] for x in e["q"]) for e in self._pending_input)

    async def run_shell_input(self, command, original=""):
        h = await hooks.run("UserPromptSubmit", {"session_id": self.sid, "cwd": os.getcwd(),
                                                 "event": "UserPromptSubmit", "prompt": "!" + command})
        if h["decision"] == "deny":
            await self.note(h["message"].strip() or "blocked by hook", "red")
            return
        try:
            result = await self.run_call({"id": f"shell-{int(time.time() * 1000)}", "name": SHELL_TOOL,
                                          "args": json.dumps({"command": command}, ensure_ascii=False)})
        finally:
            self.set_busy(None)   # outside a model turn nobody resets the status: "Running: PowerShell…" would hang forever
        # The output is already in the command card: show it there in full, not as a second copy below
        with suppress(Exception):
            next(w for w in reversed(self.chat.children) if isinstance(w, Card) and w.tool == SHELL_TOOL).expand()
        # Shell mode is a user-initiated command, not an assistant tool call; keep a valid transcript.
        self.msgs.append({"role": "user", "content": f"<direct_shell command=\"{command}\">\n<output>\n{result}\n</output>\n</direct_shell>"})
        self.save_session()

    async def expand_attachments(self, text):
        """Resolve @path and @path#start-end references into bounded read-only attachments (tools.fs.attach_files)."""
        return await asyncio.to_thread(attach_files, text)

    def action_cancel_or_quit(self):
        if self.busy:
            return self.action_cancel()
        if time.time() - self.last_c < 1.5:
            return self.exit()
        self.last_c = time.time()
        self.notify("Press Ctrl+C again to quit")

    # ── commands ──
    async def command(self, text):
        self._echo = text.strip()   # note() shows the command above its output
        try:
            await self._command(text)
        finally:
            self._echo = None

    async def _command(self, text):
        cmd, _, arg = text.partition(" ")
        if cmd in ("/quit", "/exit"):
            self.exit()
        elif cmd in ("/new", "/clear"):
            if self.busy:
                return self.notify("Stop generation first (Esc)", severity="warning")
            self.save_session()
            await self.reset()
        elif cmd in ("/history", "/sessions"):
            sid = await self.push_screen(HistoryScreen(self.sid), wait_for_dismiss=True)
            if sid and self.busy:
                self.notify("The agent is still working: stop it (Esc) first, then open the session", severity="warning")
            elif sid:
                await self.open_session_safely(sid)
            await self.refresh_session_tabs(force=True)  # deleted sessions leave the tabs
        elif cmd == "/search":
            if not arg.strip():
                await self.note("Usage: /search words to find in saved sessions", "yellow")
            else:
                hits = search_sessions(arg.strip())
                if not hits:
                    await self.note("No matching messages in saved sessions for this cwd.", "yellow")
                else:
                    labels = [f"{Path(x['file_path']).name}:msg{x['msg_no']} [{x['role']}] {x['snippet']}" for x in hits]
                    choice = await self.push_screen(Pick(f"Session search: {arg.strip()}", labels, False), wait_for_dismiss=True)
                    if choice:
                        hit = next((x for x, label in zip(hits, labels) if label == choice), None)
                        if hit:
                            await self.command("/resume " + hit["session_id"])
        elif cmd == "/cloud":
            subcmd, _, value = arg.strip().partition(" ")
            subcmd, value = subcmd.lower(), value.strip()
            if subcmd in cloud.OPTIONS:
                field = cloud.OPTIONS[subcmd][0]
                if not value:
                    await self.note(f"Cloud {subcmd}: {cloud.load_config().get(field)}  ·  change: /cloud {subcmd} <value>", "cyan")
                else:
                    try:
                        cfg = cloud.set_option(subcmd, value)
                        await self.note(f"Cloud {subcmd} set to {cfg[field]}.", "green")
                    except ValueError as exc:
                        await self.note(f"Cloud {subcmd}: {exc}", "red")
            elif subcmd == "add":
                saved = await self.push_screen(CloudSetupScreen(add=True), wait_for_dismiss=True)
                if saved and saved.get("account"):
                    a = saved["account"]
                    await self.note(f"Cloud key added: {a['label']} · default model {a['model']} · "
                                    f"{len(a['models'])} models. The agent picks among them (cloud_code action=list).", "green")
            elif subcmd in ("list", "accounts"):
                accs = cloud.accounts()
                lines = [f"{'*' if i == 0 else ' '} {a['label']}  ({a['id']})  {a['base_url']}  default {a['model']}  "
                         f"· {len(a['models'])} models" for i, a in enumerate(accs)]
                head = "Cloud accounts (* = default):\n" + "\n".join(lines) if lines else "No cloud accounts."
                await self.note(head + "\n\n" + cloud.catalog() +
                                "\n\n/cloud add · /cloud remove <id> · /cloud note <model> <text>", "cyan")
            elif subcmd == "remove":
                try:
                    gone = cloud.remove_account(value)
                    await self.note(f"Removed {gone['label']} and its key.", "yellow")
                except ValueError as exc:
                    await self.note(str(exc), "red")
            elif subcmd in ("default", "models", "test"):
                ref, _, extra = value.partition(" ")
                try:
                    if subcmd == "default":
                        a = cloud.make_default(ref, extra.strip())
                        await self.note(f"Default cloud model: {a['model']} via {a['label']}.", "green")
                    elif subcmd == "models":
                        a = await asyncio.to_thread(cloud.refresh_models, ref)
                        await self.note(f"{a['label']}: {len(a['models'])} models: " + ", ".join(a["models"][:30]), "cyan")
                    else:
                        r = await asyncio.to_thread(cloud.apply_change, {"do": "test", "id": ref or cloud.accounts()[0]["id"]})
                        await self.note(r["test"], "green" if r["test"].startswith("Connection successful") else "red")
                except (ValueError, IndexError) as exc:
                    await self.note(str(exc) or "No cloud accounts: /cloud add", "red")
            elif subcmd == "note":
                model_id, _, text = value.partition(" ")
                try:
                    cloud.set_note(model_id, text)
                    await self.note(f"Note for {model_id}: {text or '(removed)'}", "green")
                except ValueError as exc:
                    await self.note(str(exc), "red")
            elif subcmd == "disable":
                cloud.disable()
                await self.note("Cloud code assistance disabled. API key remains securely stored.", "yellow")
            elif subcmd == "status":
                cfg = cloud.status()
                state_text = "enabled" if cfg.get("configured") else "not configured"
                await self.note(f"Cloud: {state_text}\nEndpoint: {cfg.get('base_url')}\nModel: {cfg.get('model')}\n"
                                f"Max output tokens: {cfg.get('max_tokens')}\n"
                                f"API key: {'stored' if cfg.get('has_key') else 'missing'}\n"
                                f"Subagents: {'allowed' if cfg.get('allow_subagents') else 'disabled'}\n"
                                f"Approval: {'off, every task is sent without asking' if cfg.get('auto_approve') else 'each task is shown first'}"
                                "  ·  /cloud autosend on|off", "cyan")
            elif subcmd in ("", "setup", "configure"):
                was_home = self.query_one("#welcome", Static).display
                saved = await self.push_screen(CloudSetupScreen(), wait_for_dismiss=True)
                if saved and was_home:
                    await self.banner()
                elif saved:
                    await self.note("Cloud code assistance configured. The exact task is shown before every request; no history/files are attached.", "green")
            else:
                await self.note("Usage: /cloud setup | add | list | default <id> [model] | models <id> | test [id] | remove <id> | note <model> <text> | status | disable | tokens <n> | subagents on|off | autosend on|off", "yellow")
        elif cmd == "/server":
            await self.server_cmd(arg.strip())
        elif cmd == "/local":
            await self.local_cmd(arg)
        elif cmd in ("/settings", "/config"):
            await self.settings_cmd(arg)
        elif cmd in ("/model", "/models"):
            if cmd == "/models" and not arg.strip():
                profiles = SETTINGS.get("model_settings") or SETTINGS.get("models") or {}
                names = sorted(set([S["model"], *(str(k) for k in profiles)])) if isinstance(profiles, dict) else [S["model"]]
                await self.note("Configured models (current marked *):\n" + "\n".join(("* " if n == S["model"] else "  ") + n for n in names), "cyan")
                return
            requested = arg.strip()
            if requested and requested != S["model"] and len(self.msgs) > self.head + 4:
                await self.note("Compressing recent history before model switch to reduce cache churn…", "grey50")
                await self.compact(keep=None, auto=True)
            if requested:
                S["model"] = requested
            self.refresh_status()
            await self.note(f"model: {S['model']}")
        elif cmd in ("/plan", "/act", "/auto"):
            if cmd == "/auto":
                self.set_mode("auto" if not S["auto"] else "manual")
            else:
                self.set_mode("plan" if cmd == "/plan" else "manual")
            cur = self.cur_mode()
            await self.note(Text.assemble(("mode → ", "#9aa0a8"), (dict(MODES)[cur], f"bold {MODE_COLORS[cur]}"),
                                          (f"  {MODE_HINTS.get(cur, '')}", "#9aa0a8")))
        elif cmd == "/mcp":
            await self.mcp_cmd(arg)
        elif cmd == "/effort":
            v = config.norm_effort(arg) if arg.strip() else ""
            if v not in EFFORTS:
                v = await self.push_screen(EffortScreen(EFFORTS, S["effort"]), wait_for_dismiss=True)
            if v:
                S["effort"] = v
                save_state()
                self.refresh_status()
                await self.note(f"reasoning depth: {v}" + (" · staged run for big multi-part tasks" if v == "ultra" else ""),
                                "#b69cff" if v == "ultra" else "grey62")
        elif cmd in ("/sampling", "/temp", "/topk"):
            keys = {"temp": "temperature", "temperature": "temperature", "top_k": "top_k", "topk": "top_k",
                    "top_p": "top_p", "presence": "presence_penalty", "frequency": "frequency_penalty"}
            v = arg.strip().lower()
            changed = False
            if not v:
                await self.note(sampling_text(S["model"]))
                return
            elif v in ("reset", "default", "clear"):
                RUNTIME.clear()
                changed = True
            else:
                bad = []
                changed = True
                for p in v.replace(",", " ").split():
                    k, sep, val = p.partition("=")
                    if not sep:
                        k, val = cmd.strip("/"), k
                    key = keys.get(k)
                    if not key:
                        bad.append(k)
                        continue
                    try:
                        num = int(float(val)) if key == "top_k" else float(val)
                    except ValueError:
                        bad.append(val)
                        continue
                    if key == "temperature" and not 0 <= num <= 2:
                        bad.append(val)
                        continue
                    RUNTIME[key] = num
                    changed = True
                if bad:
                    await self.note(f"not set: {' '.join(bad)} · use temp, top_k, top_p, presence, frequency", "yellow")
            if changed:
                save_state()
            await self.note(Text.assemble(("applied now, saved to state.json\n", "#98c379"), sampling_text(S["model"])))
            self.refresh_status()
        elif cmd == "/context":
            report = Text(self.context_report(), style=NOTE_STYLES["cyan"])
            report.highlight_regex(r"^\S.*:$", "bold #e0e0e0")
            report.highlight_regex(r"▮+", "#d97757")
            report.highlight_regex(r"TOTAL.*", "bold #e0e0e0")
            await self.note(report)
        elif cmd == "/undo":
            # Remember the pre-undo bytes of every file in the step so /redo can put them back.
            state_now = {}
            for rec in undo.last_group():
                with suppress(OSError):
                    target = Path(rec["path"])
                    state_now.setdefault(rec["path"], target.read_bytes() if target.exists() else None)
            r = undo.undo_step()
            if r:
                self.redo_stack.append(state_now)
            await self.note(f"/undo restored {len(r)} file(s) of the last step:\n" + "\n".join(r) if r
                            else "Nothing to undo (no checkpoints for this session)", "cyan" if r else "yellow")
        elif cmd == "/redo":
            if not self.redo_stack:
                await self.note("Nothing to redo; run /undo first.", "yellow")
            else:
                turn = f"redo-{time.time_ns()}"   # one group: the next /undo reverts the whole redo
                files = self.redo_stack.pop()
                for path, content in files.items():
                    target = Path(path)
                    with suppress(OSError):
                        undo.snap(path, target.read_bytes() if target.exists() else "", new=not target.exists(), turn=turn)
                        if content is not None:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(content)
                        elif target.exists():
                            target.unlink()
                await self.note(f"/redo re-applied {len(files)} file(s):\n" + "\n".join(files), "cyan")
        elif cmd in ("/allow", "/deny"):
            if not arg.strip():
                await self.note(f"Usage: /allow {SHELL_TOOL}(git *) or /allow py_run", "yellow")
            else:
                permissions.add(cmd[1:], arg.strip())
                await self.note(f"{cmd[1:]} {arg.strip()} saved to {PERMS_FILE}", "cyan")
        elif cmd == "/permissions":
            r = permissions.load()
            rules = [("allow", x) for x in r["allow"]] + [("deny", x) for x in r.get("deny", [])] + [("ask", x) for x in r.get("ask", [])]
            if not rules:
                await self.note(f"No permission rules. Add: /allow {SHELL_TOOL}(git *) — then it will not ask again", "grey70")
            else:
                await self.note(permissions_text(rules, PERMS_FILE))
        elif cmd == "/compact":
            if self.busy:
                return self.notify("Generation is running. Esc — interrupt.", severity="warning")
            try:
                keep = int(arg) if arg.strip() else None
            except ValueError:
                keep = None
            await self.put(SelStatic(Text("❯ " + text), classes="user"))
            self.set_busy("Compressing…")
            try:
                await self.compact(keep)
            finally:
                self.set_busy(None)
        elif cmd == "/copy":
            with suppress(Exception):
                self.screen.clear_selection()
            txt = self.chat_text(last=arg.strip() == "last")
            if txt:
                self.copy_now(txt, preview=False)
            else:
                await self.note("Nothing to copy", "red")
        elif cmd == "/btw":
            if arg.strip():
                await self.btw(arg.strip())
            else:
                await self.note("Usage: /btw question — a quick side question with context", "red")
        elif cmd == "/run":
            if self.busy:
                return self.notify("Generation is running. Esc — interrupt.", severity="warning")
            if S["mode"] == "plan":
                self.set_mode(S["pre_plan"])
            what = arg.strip()
            await self.turn(what or "Continue: implement what you planned in the plan. No new plan.",
                            shown="/run" + (" " + what if what else ""))
        elif cmd == "/memory":
            t = arg.strip()
            if t:
                f, line = memory.note(t, "project")
                await self.note(f"{line}\n→ {f}" if f else line, "cyan")
            else:
                s = [f"{f}: {n} lines (limit {memory.MAX_LINES})" for f, n in memory.stats()]
                for f in memory.index_files():
                    tp = memory.topics(f)
                    if tp:
                        s.append(f"topic files in {f.parent}: {', '.join(tp)}")
                r = memory.rules()
                if r:
                    s.append("rules: " + ", ".join(f"{f} ({', '.join(p)})" for p, b, f in r))
                await self.note("\n".join(s) or "memory is empty", "cyan")
        elif cmd == "/init":
            cwd = Path(os.getcwd())
            f = memory.instruction_file(cwd) or cwd / "CLAUDE.md"
            old = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
            draft = await subagent.run(
                f"Look at the project in {cwd}: its folder tree, README, config and test files. Write the text of a "
                "project instruction file for an agent: how to run and test this project, what lives in which folder "
                "(the map an agent can navigate by), the conventions and what to avoid. Up to 120 lines of plain "
                "markdown, facts only, nothing you did not read. Return only the file text, no comments.",
                parent_session=self.sid)
            if not draft:
                await self.note("the subagent found nothing to write about", "red")
                return
            choice = await self.push_screen(
                Pick(f"{'Improve' if old.strip() else 'Create'} {f}?", ["Yes, write it", "No, keep as is"],
                     False, clip(draft)), wait_for_dismiss=True)
            if not choice or not choice.startswith("Yes"):
                return
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(draft, encoding="utf-8")
            self.build_mem()          # the new instruction file goes into the memory message
            await self.note(f"written {f} ({len(draft.splitlines())} lines), memory rebuilt", "cyan")
        elif cmd == "/editor":
            prompt = self.query_one(Prompt)
            draft = await self.push_screen(EditorScreen(arg), wait_for_dismiss=True)
            if draft is not None:
                prompt.text = draft
                prompt.move_cursor(prompt.document.end)
                prompt.focus()
                await self.note("Draft loaded into the input; press Enter to send it.", "grey50")
        elif cmd == "/cache":
            q, h = cache_stats()
            s = [f"last prompt: {S['ptok']:,} tok, of them cached: {S.get('cached', 0):,}"]
            s.append(f"engine prefix cache: {int(h):,} of {int(q):,} requests ({h * 100 // q}%)" if q else "/metrics not available")
            await self.note("\n".join(s), "cyan")
        elif cmd == "/tools":
            names = [i["function"]["name"] for i in self.tools()]
            s = [f"catalog in the prompt prefix: {len(names)} tool(s)"]
            s.append(", ".join(names) if names else "none")
            s.append("catalog is sent in every request of this session (the prefix must not change)")
            s.append("tool calls allowed" if self._allow_tool_calls else
                     "tool_choice=none: only this parameter changes between requests, the prefix stays identical")
            await self.note("\n".join(s), "cyan")
        elif cmd == "/skill":
            name, _, a = arg.strip().partition(" ")
            x, body = skills.skill(name, a) if name else (None, None)
            if not name:
                await self.note("\n".join(f"{s['name']} — {s['description']}" for s in skills.skills())
                                or "no skills", "cyan")
            elif not x:
                await self.note("no skill " + name + " (look in .maxedcode/skills)", "red")
            else:
                # Marked as loaded: as a bare user message the model called the skill tool for it again.
                await self.turn(f'<skill name="{x["name"]}" loaded_by="user">\n{body}\n</skill>', shown=text)
        elif cmd == "/commands":
            c = skills.commands()
            await self.note("\n".join(f"{k} — {v[0].get('description') or '(no description)'}" for k, v in c.items())
                            or "no custom commands (.maxedcode/commands/*.md)", "cyan")
        elif cmd == "/agents":
            a = skills.agents()
            await self.note("\n".join(f"{k} — {v['description']} (tools: {', '.join(v['tools']) or 'all'})" for k, v in a.items())
                            or "no custom agents (.maxedcode/agents/*.md)", "cyan")
        elif cmd == "/hooks":
            s = []
            for e, hs in (SETTINGS.get("hooks") or {}).items():
                for h in hs:
                    for k in h.get("hooks") or []:
                        s.append(f"{e} [{h.get('matcher') or '*'}] {k.get('command')}")
            await self.note("\n".join(s) or "no hooks in settings.json", "cyan")
        elif cmd == "/ask":
            if arg.strip():
                permissions.add("ask", arg.strip())
                await self.note(f"ask rule added: {arg.strip()}", "green")
            else:
                await self.note("Usage: /ask tool or tool(pattern)", "red")
        elif cmd == "/save":
            self.name_ = arg.strip() or self.title_ or ""
            self.save_session()
            await self.note("saved" + (f" as {self.name_}" if self.name_ else ""), "cyan")
        elif cmd == "/resume":
            sid = arg.strip() or last_session()
            if not sid:
                await self.note("no sessions for this cwd", "red")
            else:
                if await self.open_session_safely(sid):
                    await self.note(f"resumed: {self.title_ or 'new session'}", "cyan")
        elif cmd == "/rewind":
            if not self.marks:
                await self.note("Nothing to rewind to yet: rewind points appear after your messages.", "yellow")
            elif self.busy:
                await self.note("Stop generation first (Esc).", "yellow")
            else:
                # one point per rewind mark: listing every user-role message (repo map, nudges, checkpoints)
                # pointed at the wrong turn or past the end of marks (IndexError killed the app)
                marks = [x for x in self.marks if self.head <= x[0] < len(self.msgs)]
                labels = []
                for k, (i, _) in enumerate(marks, 1):
                    # the turn's own words: a <repo_map> message may come right after the mark
                    text = next((t for m in self.msgs[i:i + 4] if m.get("role") == "user"
                                 for t in [visible_user_text(m.get("content"))] if t), "")
                    labels.append(f"{k}. {' '.join(text.split())[:60]}")
                ch = await self.push_screen(Pick("Rewind to which point?", labels, False), wait_for_dismiss=True) \
                    if labels else None
                if ch:
                    n = int(ch.split(".")[0])
                    i, ts = marks[n - 1]
                    self.msgs, self.marks = self.msgs[:i], marks[:n - 1]
                    # The instruction baseline survives a rewind that removed nothing; only a real
                    # revert drops it (opencode instruction-state.ts rewind with a boundary).
                    dropped = self.instate.rewind(i)
                    self.rules_seen, self.docs_seen = self.instate.seen("rule"), self.instate.seen("doc")
                    r = undo.rewind(ts)
                    await self.note(f"rewound to the point {n}: {r} file(s) restored, history cut to {len(self.msgs)} messages"
                                    + (f", {self.instate.count()} instruction(s) dropped" if dropped else ", instruction baseline kept"), "cyan")
        elif cmd == "/help":
            await self.note(help_text(self))
        else:
            fm, body = skills.command_text(cmd[1:], arg.strip())
            if body:
                # custom slash command from .maxedcode/commands or ~/.maxedcode/commands
                if fm.get("model"):
                    S["model"] = fm["model"]
                await self.turn(body, shown=text)
            else:
                await self.note(f"Unknown command {cmd} — {key_label(PALETTE_KEY)} lists all commands, "
                                "/help explains them", "red")

    def extra(self):
        return {"top_k": top_k_for(S["model"]), "min_p": min_p_for(S["model"]),
                **cache_extra_body(os.getcwd(), self.sid), **effort_body(S["effort"])}

    def context_report(self):
        names = {c["id"]: c["function"]["name"] for m in self.msgs if m["role"] == "assistant" for c in m.get("tool_calls", [])}
        cat = {"system prompt": tok(SYSTEM), "tool descriptions": tok(json.dumps(self.tools(), ensure_ascii=False)),
               "memory": sum(tok(m["content"]) for m in self.mem),
               "your messages": 0, "model reasoning": 0, "replies and tool calls": 0, "tool results": 0}
        items, imgs = [], 0
        for m in self.msgs[self.head:]:
            c = m.get("content") or ""
            if isinstance(c, list):
                imgs += sum(1 for p in c if p["type"] == "image_url")
                c = " ".join(p.get("text", "") for p in c if p["type"] == "text")
            if m["role"] == "user":
                k, n, lab = "your messages", tok(c), "you: " + unwrap(c)[:40].replace("\n", " ")
            elif m["role"] == "tool":
                k, n, lab = "tool results", tok(c), "result " + names.get(m["tool_call_id"], "?")
            else:
                tc = m.get("tool_calls", [])
                cat["model reasoning"] += tok(m.get("reasoning_content") or "")
                k, n = "replies and tool calls", tok(c) + sum(tok(t["function"]["arguments"]) for t in tc)
                lab = "model: " + (", ".join(t["function"]["name"] for t in tc) or c[:30].replace("\n", " "))
            cat[k] += n
            items.append((n, lab))
        total = sum(cat.values()) or 1
        lines = ["What goes into the model at each step (token estimate: ASCII ≈ 4 characters per token, other alphabets ≈ 2, CJK ≈ 1):"]
        lines += [f"  {k:<30}{v:>7,} tok.  {v * 100 // total:>3}%  " + "▮" * round(v * 20 / total) for k, v in cat.items()]
        lines.append(f"  {'TOTAL':<30}{self.est_tokens():>7,} tok." + (f"  (+ {imgs} images, the server counts them)" if imgs else ""))
        if items:
            lines.append("Heaviest items in history:")
            lines += [f"  {n:>6,} tok.  {lab}" for n, lab in sorted(items, reverse=True)[:5]]
        lines.append(f"Context window size: {CTX_WINDOW:,} tok. ({CTX_SRC}) · auto-compact at {compact.threshold()}%")
        lines.append(f"Estimated model cost this session: {cost_text(self.session_cost, S['model'])} (rates configurable in config.py / AGENT_PRICE)")
        ld = memory.loaded()
        if ld:
            lines.append("Memory loaded at launch:")
            lines += [f"  {n:>6,} lines  {label}: {f}" for label, f, n in ld]
            if self.docs_seen:
                lines.append("  folder docs loaded during the session: " + ", ".join(sorted(self.docs_seen)))
        if S["ctx"]:
            lines.append(f"The server counted in the last request (input + output): {S['ctx']:,}")
            lines.append(f"Filled: {min(100, S['ctx'] * 100 // CTX_WINDOW) if CTX_WINDOW else 0}%")
        if S.get("cached"):
            lines.append(f"Prefix cache: {S['cached']:,} tok of the last prompt came from cache")
        q, h = cache_stats()
        if q:
            lines.append(f"Engine prefix cache: {int(h):,} of {int(q):,} requests hit ({h * 100 // q}%)")
        return "\n".join(lines)

    def safe_ctx(self):
        ctx = list(self.msgs)
        for i in range(len(ctx) - 1, -1, -1):
            m = ctx[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                have = {x.get("tool_call_id") for x in ctx[i + 1:] if x["role"] == "tool"}
                if not {c["id"] for c in m["tool_calls"]} <= have:
                    ctx = ctx[:i]
                break
        return ctx

    # ── context: token estimate + compression (/compact and auto-compact) ──
    def est_tokens(self, msgs=None):
        return compact.est_tokens(self.msgs if msgs is None else msgs, self.head,
                                  compact.prefix_cost(SYSTEM, self.tools(), self.mem))

    def fill_pct(self, msgs=None, freed=0):
        """Real usage from the server, or the estimate when that is larger.

        The server number describes only the request that produced it: tool results and steer
        messages added after it are not counted there. Without max() the bar shows a stale value and
        COMPACT_AT fires one request too late — the oversized request goes to the server first and
        the turn dies with a context-overflow error.
        """
        return compact.fill_pct(self.est_tokens(msgs), S["ptok"] or S["ctx"], self.saved_tok + freed, CTX_WINDOW,
                                self.est_sent())

    def est_sent(self):
        """Our estimate of the request S["ptok"] measured (0 when there is no measurement): what was
        appended after it is added to the server number (compact.used_tokens)."""
        return getattr(self, "_est_sent", 0) if S["ptok"] else 0

    def cut_index(self, keep=None):
        """Choose a compaction boundary, preserving system + memory and tool-call/result pairs."""
        return compact.auto_cut(self.msgs, self.head, keep, COMPACT_KEEP_TOKENS)

    def transcript(self, msgs):
        # The summarizer request carries the cached prefix too, so its room is smaller by that prefix.
        prefix = compact.prefix_cost(SYSTEM, self.tools(), self.mem)
        return compact.transcript(msgs, CTX_WINDOW, compact.reserve(CTX_WINDOW) + prefix)

    def summary_view(self, text, title="✻ Context summary", collapsed=False):
        """Collapsible block with a compaction summary: after a compact the model works only from it."""
        inner = SelStatic(Markdown(chat_md(text or "…")))
        w = Collapsible(inner, title=title, collapsed=collapsed)
        w.inner = inner
        return w

    def _summary_failed(self):
        if self._summary_w is not None:
            self._summary_w.title = "✻ Context summary — rejected, the context was NOT compacted"
            self._summary_w.collapsed = True
        self._summary_w = None

    async def _stream_summary(self, text, final=False):
        """Show what the summarizer is writing while it streams (throttled like the answer)."""
        now = time.monotonic()
        if not final and now - getattr(self, "_summary_t", 0.0) < 0.15:
            return
        self._summary_t = now
        body = split_think(text)[1].strip()
        if self._summary_w is None:
            if not body:
                return
            self._summary_w = await self.put(self.summary_view(body, "✻ Context summary — writing…"))
        else:
            self._summary_w.inner.update(Markdown(chat_md(body or "…")))
        if self.chat.follow:
            self.chat.scroll_end(animate=False)

    async def summarize(self, old):
        """Ask the model for the summary; a prose answer (no section headings) gets one nudge.
        Returns (summary, whole): whole is True when the summarizer saw the whole history."""
        summary = await self._inplace_summary()
        if summary:
            return summary, True
        return await self._transcript_summary(old), False

    async def _transcript_summary(self, old):
        tr = self.transcript(old)
        # After the conversation's own prefix first (the cache survives the compaction); a standalone
        # request only when that one fails, e.g. the model calls a tool instead of summarizing.
        for shared in (True, False):
            summary = await self._summary_call(tr, shared=shared)
            if summary and not compact.filled(summary) and self.compact_nudges < 1:
                self.compact_nudges += 1
                retry = await self._summary_call(tr, prev=summary, shared=shared)
                if compact.filled(retry):
                    return retry
            if summary:
                return summary
        return ""

    def summary_extra(self):
        eb = compact.summary_body(self.extra(), self.head)
        for field in self._disabled_extra_fields_by_model.get(S["model"], ()):
            eb.pop(field, None)
        return eb

    async def _inplace_summary(self):
        """Summary from the same messages the conversation already sent (KV cache hit): the
        summarizer sees the whole history. "" when it does not fit or the answer is unusable."""
        tools = self.tools()
        # The calibrated fill, not the bare estimate (it falls behind on a long turn, see compact.used_tokens).
        used = compact.used_tokens(self.est_tokens(), S["ptok"] or S["ctx"], self.saved_tok, self.est_sent())
        if not compact.inplace_fits(used, CTX_WINDOW):
            return ""
        msgs = wire(sanitize_tool_history(self.safe_ctx(), tools)) + [compact.inplace_request(COMPACT_PROMPT)]
        # Same tools and effort as the conversation: both are rendered into the system turn.
        st, out = None, ""
        try:
            st = await self.client.chat.completions.create(model=S["model"], stream=True, messages=msgs,
                                                            tools=tools, extra_body=self.summary_extra(),
                                                            **sampling_for(S["model"]))
            async for ch in st:
                d = ch.choices[0].delta if ch.choices else None
                if d and getattr(d, "tool_calls", None):
                    return ""        # the model went on with the task instead of summarizing
                if d and d.content:
                    out += d.content
                    await self._stream_summary(out)
        except asyncio.CancelledError:
            with suppress(Exception):
                await st.aclose()
            raise
        except Exception:
            return ""
        out = split_think(out)[1].strip()
        return out if compact.filled(out) else ""

    async def _summary_call(self, tr, prev="", shared=True):
        """Transcript summary. shared: asked after the conversation's prefix with its tools and body
        (see compact.transcript_request), "" when that fails; otherwise a standalone request."""
        if shared:
            tools = self.tools()
            msgs = wire(sanitize_tool_history(self.msgs[:self.head], tools)) + [compact.transcript_request(COMPACT_PROMPT, tr)]
            kw = dict(tools=tools, extra_body=self.summary_extra())
        else:
            msgs = [{"role": "system", "content": COMPACT_PROMPT}, {"role": "user", "content": tr}]
            kw = dict(extra_body={**self.extra(), **effort_body(summary_effort(S["effort"]))})
        if prev:
            msgs += [{"role": "assistant", "content": prev}, {"role": "user", "content": compact.NUDGE}]
        st = None
        try:
            st = await self.client.chat.completions.create(model=S["model"], stream=True, messages=msgs,
                                                            **kw, **sampling_for(S["model"]))
            out = ""
            async for ch in st:
                d = ch.choices[0].delta if ch.choices else None
                if shared and d and getattr(d, "tool_calls", None):
                    return ""        # the model went on with the task instead of summarizing
                if d and d.content:
                    out += d.content
                    await self._stream_summary(out)
            return split_think(out)[1].strip()
        except asyncio.CancelledError:
            with suppress(Exception):
                await st.aclose()
            raise
        except Exception:
            if not shared:
                raise
            return ""

    async def compact(self, keep=None, auto=False, fallback=False):
        """Compresses the history into one message; the last COMPACT_KEEP messages stay as they are.
        fallback: when the summarizer fails, use a summary built without the model (the context overflows,
        and the turn used to die here)."""
        cut = self.cut_index(keep)
        if cut is None or cut <= self.head:
            if not auto:
                await self.note("Nothing to compress: the history has not filled the context yet", "red")
            return False
        before, old = self.est_tokens(), self.msgs[self.head:cut]
        # opencode rule: while the whole history fits inside (window - reserve) it is summarized as it
        # is, so the summarizer request keeps the same prefix and its cache hit.
        room = CTX_WINDOW - compact.reserve(CTX_WINDOW)
        whole = compact.without_tail(self.msgs, self.head)
        if compact.est_tokens(whole, self.head, compact.prefix_cost(SYSTEM, self.tools(), self.mem)) <= room:
            # The whole history (minus the kept tail) fits inside the summarizer's room: send it as is.
            cut = len(self.msgs) - COMPACT_KEEP
            while cut > self.head and self.msgs[cut].get("role") == "tool":
                cut -= 1      # the kept tail must not start with a result whose call was summarized away
            old = self.msgs[self.head:cut]
        h = await hooks.run("PreCompact", {"session_id": self.sid, "cwd": os.getcwd(), "event": "PreCompact",
                                          "transcript": self.transcript(old)})
        if h["message"]:
            await self.note(h["message"].strip(), "cyan")
        self._summary_w = None
        self.set_busy("Compacting context…")
        try:
            summary, saw_all = await self.summarize(old)
        except Exception as e:
            if not fallback:
                self._summary_failed()
                if not auto:
                    await self.note(f"Compression failed: {type(e).__name__}: {e}", "red")
                return False
            summary, saw_all = "", False
        if not summary and fallback:
            summary = compact.basic_summary(old)
            await self.note("The summarizer failed; compressed with a summary built from the history itself", "yellow")
        if not summary:
            self._summary_failed()
            if not auto:
                await self.note("The summarizer returned nothing, context not compressed", "red")
            return False
        if not compact.filled(summary):
            # The template is what continuation needs; a prose summary is worth showing, not hiding.
            await self.note("summary has no section headings — the summarizer did not follow the template", "bold red")
        if saw_all and keep is None:
            # It saw the tail too: keep only the last step verbatim, the rest is re-read for nothing.
            cut = compact.step_cut(self.msgs, cut)
            old = self.msgs[self.head:cut]
        original_tail = [m for m in self.msgs[cut:]
                         if not (m.get("role") == "user" and str(m.get("content") or "").startswith("<pending_work>"))]
        # Recompaction updates the same anchor id rather than accumulating summary messages.
        compact_id = compact_anchor(self.msgs) or f"{self.sid}-summary"
        anchor = {"role": "user", "content": f"<compact id=\"{compact_id}\">\n{compact.anchor_body(summary, old)}\n({len(old)} messages compressed)</compact>"}
        rebuilt = [self.msgs[0]] + self.mem + [anchor]
        self.archive += old   # the model loses them, the saved session (and /history) keeps them
        self.msgs = rebuilt + original_tail
        self.loopguard.forget_reads()   # what it read before is gone from its context: reading again is allowed
        # Rules and folder docs folded into the summary are gone too: they come again on the next touch.
        self.instate.compacted(cut, len(rebuilt))
        self.rules_seen, self.docs_seen = self.instate.seen("rule"), self.instate.seen("doc")
        title = f"✻ Context summary — {len(old)} messages → what the model keeps (click to fold)"
        if self._summary_w is None:
            self._summary_w = await self.put(self.summary_view(compact.summary_text(anchor), title))
        else:
            self._summary_w.title = title
            self._summary_w.inner.update(Markdown(chat_md(compact.summary_text(anchor))))
        self._summary_w = None
        pending = [f"- [{t['status']}] {t['content']}" for t in self.todos if t.get("status") != "done"]
        if pending:
            self.msgs.append({"role": "user", "content": "<pending_work>\n" + "\n".join(pending) + "\n</pending_work>"})
        self.marks = []   # rewind points of the compressed part are gone
        # numbers on the bar must be right at once: real usage from the server is stale after a rebuild
        S["ptok"], S["ctx"] = 0, self.est_tokens()
        self.saved_tok = 0     # after a rebuild the usage is an estimate, nothing to correct
        self.save_session()
        await self.note(f"— context compressed: {before:,} → {self.est_tokens():,} tok. · {len(old)} messages compressed · "
                        f"{self.fill_pct()}% of the window —", "yellow" if auto else "cyan")
        self.refresh_status()
        return True

    async def btw(self, q):
        scr = BtwScreen(q)
        job = (q, scr)
        self._btw_jobs.append(job)
        self.btw_kick()
        if job in self._btw_jobs:
            scr.status = "Waiting for the model to finish its current step…"
        await self.push_screen(scr, wait_for_dismiss=True)
        if job in self._btw_jobs:
            self._btw_jobs.remove(job)
        elif self._btw_scr is scr and self._btw_task:
            self._btw_task.cancel()   # closed while answering: stop generation on the server too

    async def child_btw(self, sid, q):
        """/btw in a subagent tab: a side question to that subagent. It forks from the child's last request
        (the server holds it), waits for the child's step in progress, and the child's next step waits for it."""
        base = subagent.LAST_REQUEST.get(sid)
        if not base:
            self.notify("This subagent has not sent a request yet — ask again in a moment.", severity="warning")
            return
        base = {**base, "messages": [dict(m) for m in base["messages"]]}
        scr = BtwScreen(q)
        scr.status = "Waiting for the subagent to finish its current step…"
        task = asyncio.create_task(self.btw_answer(q, scr, base))
        await self.push_screen(scr, wait_for_dismiss=True)
        if not task.done():
            task.cancel()   # closed early: stop generation on the server too

    def btw_kick(self):
        """Start the next queued side question if the server is free: between model steps or turns.

        It does not wait for the turn to end: right after a model step the server is idle while the tools
        run, and the side answer streams in that gap. The next step waits for it (btw_settle), so the two
        never compete for the server.
        """
        if not self._btw_ready or not self._btw_jobs or (self._btw_task and not self._btw_task.done()):
            return
        q, scr = self._btw_jobs.pop(0)
        # During a turn: exactly the request the last step sent, which the server holds in its cache.
        # Shallow copies: compaction rewrites the history in place.
        base = self._last_kw if self.busy else None
        if base:
            base = {**base, "messages": [dict(m) for m in base["messages"]]}
        self._btw_scr = scr
        self._btw_task = asyncio.create_task(self.btw_answer(q, scr, base))
        self._btw_task.add_done_callback(lambda _t: self.btw_kick())

    async def btw_settle(self):
        """The next model step waits for a side answer in progress (and the ones queued behind it)."""
        while self._btw_task and not self._btw_task.done():
            await asyncio.wait({self._btw_task})   # not `await task`: its cancel must not cancel the turn
        self._btw_ready = False

    async def btw_answer(self, q, scr, base=None):
        """Stream one side answer into its screen; nothing goes into the history.

        The question goes after a prefix the server already holds, so it reads only its own tail, and the
        fork point is pinned like a subagent's: the btw tail must not push out the checkpoint the
        conversation resumes from. Mid-turn the prefix is the last step's request as sent; the step the
        model just made is not in it (its tool results are not ready yet). Between turns it is the
        history, built the way stream_once builds it.
        """
        if base is None:
            tool_specs = self.tools()
            eb = self.extra()
            for field in self._disabled_extra_fields_by_model.get(S["model"], ()):
                eb.pop(field, None)
            base = dict(model=S["model"], messages=wire(sanitize_tool_history(self.safe_ctx(), tool_specs)),
                        tools=tool_specs, extra_body=eb, **sampling_for(S["model"]))
            if TOOL_CHOICE_NONE and tool_specs and not self._allow_tool_calls:
                base["tool_choice"] = "none"
        prefix = base["messages"]
        ask = {"role": "user", "content": f"<btw>Side question from the user. Answer it briefly, without tools; "
                                          f"do not continue any task here: {q}</btw>"}
        kw = {k: v for k, v in base.items() if k != "stream_options"}
        kw.update(messages=[*prefix, ask], stream=True,
                  extra_body=cachekey.pin(dict(base["extra_body"]), len(prefix)))
        try:
            # The slot subagents take per request: a side question never runs at once with a child's step
            # (ultra parts, the subagent tool); it goes between two of them.
            async with subagent.inference_slot():
                scr.status = "Thinking…"
                scr.show()
                st = await self.client.chat.completions.create(**kw)
                try:
                    async for ch in st:
                        if ch.choices:
                            d = ch.choices[0].delta
                            scr.think += (d.model_extra or {}).get("reasoning_content") or ""
                            scr.text += d.content or ""
                            scr.show()
                finally:
                    with suppress(Exception):
                        await st.aclose()   # closed early: stop generation on the server, the turn waits for it
            if not scr.text.strip():
                # tools are in the request only to keep the prefix identical; a call is never run here
                scr.text = "(no answer: the model tried to call a tool — ask again, or ask in the main chat)"
                scr.show()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            scr.text = f"Error: {type(e).__name__}: {e}"
            scr.show()

    async def local_cmd(self, arg):
        """/local: run a model on this PC in a few picks - the program (Strata, LM Studio, Ollama, llama.cpp), the
        model, the context - then the agent uses it. /local stop stops the one that runs. maxedcode/launch.py does
        the work, the same as Settings -> Models -> Run a model in the window."""
        from .. import launch
        await self.note("looking for model servers and models on this computer…", "grey50")
        cat = await asyncio.to_thread(launch.catalog)
        progs = cat["servers"]
        if arg.strip().lower() in ("stop", "off"):
            on = [x for x in progs if x["running"]]
            if not on:
                return await self.note("no model server runs on this computer", "yellow")
            x = on[0]
            if len(on) > 1:
                labels = [f"{y['label']} · {(y['loaded'] or {}).get('model') or 'running'}" for y in on]
                choice = await self.push_screen(Pick("Stop which server?", labels, False), wait_for_dismiss=True)
                if not choice:
                    return
                x = on[labels.index(choice)]
            await self.note(f"stopping {x['label']}…", "grey50")
            r = await asyncio.to_thread(launch.stop, x["kind"])
            return await self.note(f"{x['label']}: {r['text']}", "green" if r["ok"] else "yellow")

        def ctx_k(n):
            return f"{n // 1024}K" if n < 1048576 else f"{n // 1048576}M"

        def state(x):
            if x["running"]:
                lo = x.get("loaded") or {}
                return "running" + (f": {lo['model']}" if lo.get("model") else ", no model loaded") + \
                    (f" · {ctx_k(lo['ctx'])}" if lo.get("ctx") else "")
            if not x["installed"]:
                return f"not installed - {x['about']}"
            return f"installed, not running · {len(x['models'])} models"
        labels = [f"{x['label']} · {state(x)}" for x in progs]
        choice = await self.push_screen(Pick("Run a model on this computer: which program?", labels, False,
                                             body="maxedcode starts the server, loads the model with the context you "
                                                  "pick and switches the agent to it. /local stop stops it."),
                                        wait_for_dismiss=True)
        if not choice:
            return
        x = progs[labels.index(choice)]
        if not x["installed"]:
            return await self.note(f"{x['label']} is not installed: download it from {x['download']}, then /local again",
                                   "yellow")
        if not x["can_start"]:
            return await self.note(f"{x['label']}: its start command is not found - {x.get('how') or 'start it yourself'}",
                                   "yellow")
        models, script = x["models"], None
        opts = []
        for m in models:
            facts = [m.get("params"), m.get("quant"), f"{m['size'] / 1e9:.1f} GB" if m.get("size") else "",
                     f"up to {ctx_k(m['max_ctx'])}" if m.get("max_ctx") else "", "loaded" if m.get("loaded") else ""]
            opts.append(" · ".join([m["name"], *[f for f in facts if f]]))
        scripts = [f"own script: {sc['name']}" for sc in x.get("scripts") or []]
        if not opts and not scripts:
            hint = f" (in a terminal: {x['hint']})" if x.get("hint") else ""
            return await self.note(f"{x['label']} has no models yet{hint}", "yellow")
        choice = await self.push_screen(Pick(f"{x['label']}: which model?", opts + scripts, False), wait_for_dismiss=True)
        if not choice:
            return
        model, ctx = None, None
        if choice in scripts:
            script = x["scripts"][scripts.index(choice)]["path"]
        else:
            model = models[opts.index(choice)]
            best = model["pick_ctx"]
            sizes = [best] + [c for c in model["ctxs"] if c != best]
            names = [f"{ctx_k(c)}  ({c:,} tokens)" + ("  - last used / suggested" if c == best else "") for c in sizes]
            pick = await self.push_screen(Pick(f"{model['name']}: context", names, True,
                                               body="How much text the model holds at once. More: longer work "
                                                    "without compaction, but more graphics memory. 32K-64K is "
                                                    "comfortable; the agent needs 16K at least. Custom: a number "
                                                    "of tokens (64k works too)."), wait_for_dismiss=True)
            if not pick:
                return
            if pick in names:
                ctx = sizes[names.index(pick)]
            else:
                digits = int(re.sub(r"\D", "", pick) or 0)
                ctx = digits * 1024 if 0 < digits < 1024 else digits or best
        lines = []

        def say(text, pct=None):
            lines.append((text, pct))
        task = asyncio.ensure_future(asyncio.to_thread(launch.start, x["kind"], model and model["id"], ctx, script, say))
        what = (model or {}).get("name") or Path(script).name
        await self.note(f"starting {x['label']}: {what}" + (f" · context {ctx_k(ctx)}" if ctx else "") +
                        " (it keeps loading in the background; /local stop stops it)", "grey50")
        shown, last, started = 0.0, "", time.monotonic()
        while not task.done():
            await asyncio.sleep(1)
            if lines and time.monotonic() - shown >= 10 and lines[-1][0] != last:
                text, pct = lines[-1]
                last, shown = text, time.monotonic()
                await self.note(f"  {int(time.monotonic() - started)} s · {text}" + (f" · {pct:.0f}%" if pct else ""),
                                "grey50")
        done = task.result()
        if not done.get("ok"):
            return await self.note(f"{x['label']} did not start: {done.get('error')}", "red")
        await self.use_server(done["base"], done["kind"],
                              done.get("model") if done["kind"] in ("lmstudio", "ollama") else None)

    async def settings_cmd(self, arg):
        """/settings: every setting in one deck with its value now and what it is for; Enter opens it (its slash
        command). The window's Settings edit the same files."""
        from .. import servers
        from .screens import SettingsDeck, command_badge
        rows = [("model", "/local", "Run a model on this PC", "Pick a program (Strata, LM Studio, Ollama, llama.cpp), a model and a context: maxedcode starts it and switches to it."),
                ("model", "/server", "Model server", "Where the agent's model runs: this PC, another PC on the network (an IP is enough) or a cloud API with a key."),
                ("model", "/models", "Model", "The model the server serves; /model <name> switches to another one."),
                ("model", "/effort", "Reasoning effort", "Faster or smarter answers: off, low, medium, high, ultra."),
                ("model", "/sampling", "Sampling", "Temperature, top_p and top_k of the model; /sampling reset brings the defaults back."),
                ("model", "/cloud", "Cloud help", "A cloud model drafts the hard parts; only the task text is sent, never the project."),
                ("modes", "/plan", "Plan mode", "Reads and searches, never edits. Shift+Tab cycles the modes."),
                ("modes", "/act", "Manual mode", "Asks before every edit and command."),
                ("modes", "/auto", "Auto mode", "Runs tools without asking."),
                ("rules", "/permissions", "Permissions", "allow / deny / ask rules for tools (/allow, /deny add one)."),
                ("rules", "/hooks", "Hooks", "Commands run on agent events, from settings.json."),
                ("tools", "/mcp", "MCP servers", "Extra tools from MCP servers: /mcp add name command."),
                ("tools", "/skill", "Skills", "Packaged instructions the agent loads when it needs them."),
                ("tools", "/tools", "Tools", "The tool catalog in the prompt."),
                ("memory", "/memory", "Memory", "What the agent remembers between sessions; /memory <text> saves a note."),
                ("memory", "/history", "Sessions", "Saved sessions of this folder: open, delete.")]
        label = servers.LABELS.get(config.SERVER_KIND) or "server"
        values = {"/server": f"{label} · {config.BASE_URL}", "/local": f"{label} · context {CTX_WINDOW:,}"}
        for _, cmd, _, _ in rows:
            if cmd not in values:
                with suppress(Exception):
                    values[cmd] = command_badge(self, cmd)[0]
        values["/models"] = values.get("/models") or str(S.get("model") or "")
        mode = self.cur_mode()
        for cmd, m in (("/plan", "plan"), ("/act", "manual"), ("/auto", "auto")):
            values[cmd] = "● on" if mode == m else "off"
        picked = await self.push_screen(SettingsDeck(rows, values), wait_for_dismiss=True)
        if picked:
            await self._command(picked[1] if isinstance(picked, tuple) else picked)

    async def server_cmd(self, arg):
        """/server: the model server of the main agent. No argument: the servers found on this PC (running ones,
        installed ones that are started from here) or a typed address; /server <ip|url> switches at once.
        Saved where the window and the next start read it (config.json, or settings.json when pinned there)."""
        from .. import appsettings, servers
        pick, kind, model = arg, "", None
        if not pick:
            await self.note("looking for model servers on this computer…", "grey50")
            found = await asyncio.to_thread(servers.scan)
            # cloud APIs after the servers of this PC: picking one asks for its key
            found += [{"label": f"Cloud: {name}", "base": url, "kind": kind, "running": True, "cloud": True}
                      for name, (kind, url) in servers.CLOUD.items()]
            labels = []
            for x in found:
                models = x.get("models") or []
                what = "API key needed" if x.get("cloud") else \
                    (x.get("loaded") or (models[0] if len(models) == 1 else f"{len(models)} models")) if x.get("running") \
                    else "installed, not running" + (" - starts it" if x.get("start") else "")
                labels.append(f"{x['label']} · {x['base']} · {what}" + (" · current" if x["base"] == config.BASE_URL else ""))
            choice = await self.push_screen(Pick(f"Model server (now {config.BASE_URL}) - pick one or type an address",
                                                 labels, True), wait_for_dismiss=True)
            if not choice:
                return
            hit = next((x for x, label in zip(found, labels) if label == choice), None)
            if hit:
                pick, kind = hit["base"], hit["kind"]
                if hit.get("cloud"):
                    had = config.API_KEY not in ("", "local") and servers.cloud_kind(config.BASE_URL) == kind
                    keep = f"keep the saved key …{config.API_KEY[-4:]}"
                    key = await self.push_screen(Pick(f"{hit['label']}: paste the API key", [keep] if had else [], True),
                                                 wait_for_dismiss=True)
                    if not key:
                        return
                    if key != keep:
                        config.API_KEY = key.strip()
                        try:
                            await asyncio.to_thread(appsettings.apply, {"api_key": config.API_KEY})
                        except ValueError as exc:
                            await self.note(f"key not saved: {exc}", "red")
                            return
                    # a cloud serves many models and loads none: one is named
                    try:
                        ids = await asyncio.to_thread(servers.model_ids, pick, config.API_KEY, 10) or []
                    except Exception as exc:        # 401 / 403: a wrong key
                        await self.note(f"{hit['label']} refused the key ({exc}); /server to try again", "red")
                        return
                    model = await self.push_screen(Pick(f"{hit['label']}: model", sorted(ids), True),
                                                   wait_for_dismiss=True)
                    if not model:
                        return
                elif not hit.get("running"):
                    await self.note(f"starting {hit['label']}…", "grey50")
                    if not await asyncio.to_thread(servers.start, hit["kind"]):
                        await self.note(f"{hit['label']} did not answer: {hit.get('how') or 'start it'}, then /server again", "yellow")
                        return
                elif hit["kind"] in ("lmstudio", "ollama") and len(hit.get("models") or []) > 1:
                    model = hit.get("loaded") or hit["models"][0]   # they load whatever a request names
            else:
                pick = choice.strip()
        if not kind:        # a typed address in any form: "192.168.0.10", "pc:1234" -> the URL that answers
            pick = await asyncio.to_thread(servers.find_base, pick, config.API_KEY) or pick
        await self.use_server(pick, kind, model)

    async def use_server(self, pick, kind="", model=None):
        """Switch the agent to this server now and save it (/server, /local)."""
        from .. import appsettings, servers
        global CTX_WINDOW, CTX_SRC
        info = await asyncio.to_thread(config.use_server, pick, kind, model)
        if not info.get("online"):
            await self.note(f"{config.BASE_URL} does not answer yet; saved anyway - start the server", "yellow")
        try:
            await asyncio.to_thread(appsettings.apply, {"base_url": config.BASE_URL, "server": config.SERVER_KIND or None,
                                                        "model": model})
        except ValueError as exc:
            await self.note(f"not saved: {exc}", "red")
        self.client = AsyncOpenAI(base_url=config.BASE_URL, api_key=config.API_KEY, **llmretry.client_options())
        CTX_WINDOW, CTX_SRC = config.CTX_WINDOW, config.CTX_SRC
        S["model"] = config.MODEL
        self.refresh_status()
        label = servers.LABELS.get(config.SERVER_KIND) or "server"
        await self.note(f"server: {label} {config.BASE_URL} · model {S['model']} · context {CTX_WINDOW:,} "
                        "(a new server reads the prompt from scratch once)", "green")
        self._model_note = info.get("note") or ""
        if self._model_note:
            await self.note(self._model_note, "grey50")

    async def mcp_cmd(self, arg):
        try:
            a = [x.strip('"') for x in shlex.split(arg, posix=False)]
        except ValueError:
            a = arg.split()
        servers = self.mcp.cfg()
        if a[:1] == ["add"] and len(a) >= 3:
            name = re.sub(r"[^A-Za-z0-9-]", "-", a[1])
            servers[name] = {"url": a[2]} if a[2].startswith("http") else {"command": a[2], "args": a[3:]}
            self.mcp.save(servers); self.mcp.start(name, servers[name])
            await self.note(f'MCP "{name}" added and connecting. Tokens/env — in {MCP_FILE}')
        elif a[:1] == ["remove"] and len(a) == 2:
            servers.pop(a[1], None); self.mcp.save(servers); self.mcp.stop(a[1])
            await self.note(f'MCP "{a[1]}" removed')
        elif a[:1] == ["reload"]:
            for n in list(self.mcp.srv):
                self.mcp.stop(n)
            for n, c in servers.items():
                self.mcp.start(n, c)
            await self.note("MCP reloading…")
        else:
            lines = [f"{n:<16} {st['status']}" for n, st in self.mcp.srv.items()] or ["no servers"]
            await self.note("MCP servers:\n" + "\n".join(lines) + f"\n\nFile: {MCP_FILE}\n"
                            "Add: /mcp add name command [arguments…]  or  /mcp add name https://url", "cyan")

    # ── sessions ──
    async def reset(self):
        self.save_session()
        self.sid, self.title_, self.name_ = new_session_id(), None, ""
        state.track_open_session(self.sid)
        self.sess_path, self.sess_cwd = None, ""   # session file: its own cwd, or any project with /history
        undo.set_sid(self.sid)
        self.msgs = [{"role": "system", "content": SYSTEM}]
        self.archive = []
        self._repo_map_revision += 1
        self._repo_map_dirty = True
        self._tool_schemas_enabled = True   # the prefix starts with the catalog and never changes
        self.build_mem()
        self.marks, self.hctx = [], ""
        S["ctx"] = 0
        self.session_cost = 0.0
        self.saved_tok = 0
        self.queue.clear()
        self.steer_queue.clear()
        self._pending_input = []
        self.todos, self.todo_w = [], None
        if self.progress_w:
            self.progress_w.reset()
        await self.chat.remove_children()
        await self.banner()
        self.save_session()  # persist a pristine session so its tab can always be reopened

    def save_session(self, index=True):
        # Save even an empty session. Otherwise its tab is visible in OPEN_SESSIONS but has no
        # backing file; switching back to that tab raises FileNotFoundError and used to terminate Textual.
        # "msgs" is what the model sees; "archive" keeps the messages compaction replaced with a summary,
        # so a reopened session still shows everything that was said (the model never gets them back).
        # "queued" — messages typed during work that the model hasn't read yet: reopened, they return to the input.
        try:
            f = self.sess_path or session_file(self.sid, self.sess_cwd)
            ts = time.time()
            saved = close_open_calls(self.msgs[self.head:])
            archive = list(getattr(self, "archive", None) or [])
            draft_of = getattr(self, "_draft", None)   # text typed but not sent (e.g. returned by Esc)
            draft = draft_of() if draft_of else ""
            if not self.title_:
                self.title_ = session_title(archive + saved) or None
            child_meta = {}
            if f.exists():
                with suppress(Exception):
                    previous = json.loads(f.read_text(encoding="utf-8"))
                    if previous.get("is_subagent"):
                        child_meta = {key: previous[key] for key in (
                            "is_subagent", "parent_sid", "agent_status", "agent_step", "agent_activity",
                            "agent_task", "agent_report", "agent_index") if key in previous}
            unsent = [e["orig"] for e in getattr(self, "_pending_input", None) or []
                      if any(x is e["text"] for x in e["q"])]
            # through a temporary file: a crash or a closed window mid-write must not break the session
            tmp = f.with_name(f"{f.name}.{time.time_ns()}.tmp")
            tmp.write_text(json.dumps(
                {"id": self.sid, "title": self.title_ or "(untitled)", "name": self.name_, "ts": ts,
                 "cwd": str(self.sess_cwd or Path.cwd()),
                 "msgs": saved, "prefix": getattr(self, "mem", None) or [],
                 "archive": archive, "draft": draft, "queued": unsent, "estimated_cost": self.session_cost,
                 "task_progress": self.progress_w.snapshot() if self.progress_w else None, **child_meta}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)
            # The tab label cached while the session was still "New session" is stale now: after a switch
            # the tab would keep showing "New session" instead of the title just written.
            getattr(self, "_session_headers", {}).pop(self.sid, None)
            if index:
                index_session(archive + saved, self.sid, cwd=self.sess_cwd, path=f, ts=ts)
        except Exception as exc:   # never crash the UI over a save, but leave a trace of it
            log = getattr(self, "_log_ui_exception", None)
            if log:
                log(f"save session {self.sid}", exc)

    async def load(self, sid):
        f = session_path(sid)
        d = json.loads(f.read_text(encoding="utf-8"))
        self.sid, self.title_, self.name_ = d["id"], clean_title(d.get("title")) or None, d.get("name") or ""
        state.track_open_session(self.sid)
        self.sess_path, self.sess_cwd = f, d.get("cwd") or ""
        if d.get("is_subagent"):
            state.upsert_child_session(self.sid, id=self.sid, title=self.title_, parent_sid=d.get("parent_sid") or "",
                                       task=d.get("agent_task") or "", status=d.get("agent_status") or "completed",
                                       step=d.get("agent_step") or 0, activity=d.get("agent_activity") or "",
                                       report=d.get("agent_report") or "", cwd=d.get("cwd") or "", path=str(f),
                                       ts=d.get("ts") or time.time(), index=d.get("agent_index"))
        self._hydrate_child_sessions(d.get("parent_sid") or self.sid)
        self.session_cost = float(d.get("estimated_cost") or 0.0)
        undo.set_sid(self.sid)
        self.msgs = [{"role": "system", "content": SYSTEM}]
        self.build_mem()
        # The saved prefix (memory + env of the day it started) is sent as it was, and only what changed
        # goes into the next message: rebuilt, a new date re-read the whole history (memory.resume_prefix).
        old_prefix, history = memory.split_saved(d["msgs"])
        prefix, self._resume_note = memory.resume_prefix(d.get("prefix") or old_prefix, self.mem, history)
        if prefix is not self.mem:
            self.mem = [dict(m) for m in prefix]
            self.head = 1 + len(self.mem)
            self.msgs = [self.msgs[0]] + self.mem
        self.msgs += history
        self.archive = [m for m in d.get("archive") or [] if isinstance(m, dict)]
        self._repo_map_revision += 1
        self._repo_map_dirty = True
        self._tool_schemas_enabled = True   # a resumed session must keep the same prefix
        if self.progress_w:
            self.progress_w.restore(d.get("task_progress"))
        self.marks, self.hctx = [], ""
        self.saved_tok = 0
        S["ptok"], S["ctx"] = 0, self.est_tokens()   # the fill bar must match the restored session at once
        self.queue.clear()
        self.steer_queue.clear()
        await self.chat.remove_children()
        widgets = self._transcript(self.archive + d["msgs"], archived=len(self.archive), live=True)
        if widgets:
            self.show_home(False)
            await self.chat.mount_all(widgets)
            self.chat.scroll_end(animate=False)
        # messages queued there and never sent (the CLI or the tab was closed first) come back to the input
        unsent = [str(x) for x in d.get("queued") or [] if str(x).strip()]
        self._restore_draft("\n\n".join(x for x in [str(d.get("draft") or ""), *unsent] if x.strip()))
        await self.note(f"— session restored: {self.title_ or 'New session'} —", "grey50")
        if unsent:
            await self.note(f"{len(unsent)} queued message(s) were not sent before the session closed — "
                            "they are back in the input; Enter sends them.", "yellow")
        if self.view_sid == self.sid:   # the shown tab became the live one: show it as such
            await self.leave_peek()
        await self.refresh_session_tabs(force=True)

    # ── agent turn ──
    async def ultra_stage(self, text, message):
        """Ultra effort: the staged run (ultra.py) before the turn's first request; its report joins `message`."""
        self._retract["answered"] = True   # parts edit files: Esc interrupts, it no longer hands the text back
        tools = self.tools()

        async def progress(line, note=True):
            stage, detail = line.removeprefix("ultra · "), ""
            if not note:        # busy-only lines may carry the live step: "planning · step 2 · grep …"
                stage, _, detail = stage.partition(" · ")
            label = stage.split(":")[0].split(",")[0].capitalize() + "…"
            self.set_busy(label + (f" · {detail[:90]}" if detail else ""))
            if note:
                await self.note(line, "#b69cff")
        # Fork at this turn's user message, not at the last tool call of the previous turn: the children
        # share the whole history, so the turn's first request after them reads only the message + report.
        end = next((i for i in range(len(self.msgs) - 1, -1, -1) if self.msgs[i] is message), None)
        # Children take the server one request at a time (subagent.inference_slot), so a /btw can go
        # between their steps; the turn's first step waits for it (btw_settle).
        self._btw_ready = True
        self.btw_kick()
        report = await ultra.run(text, self.sid, subagent.fork_prefix(self.msgs, self.mem, tools, end=end), tools,
                                 model=S["model"], progress=progress)
        if report:
            ultra.attach(message, report)
        self.set_busy("Thinking…")

    async def follow_loaded_model(self):
        """LM Studio / Ollama: name the model loaded there now (the user may have switched it between turns),
        so a request never makes the server load another one. One local request; nothing on other servers."""
        global CTX_WINDOW, CTX_SRC
        if S["model"] != config.MODEL:      # /model or a command's own model: the user's explicit choice
            return
        with suppress(Exception):
            info = await asyncio.to_thread(config.follow_loaded)
            S["model"] = config.MODEL
            CTX_WINDOW, CTX_SRC = config.CTX_WINDOW, config.CTX_SRC
            note = info.get("note") or ""
            if note and note != getattr(self, "_model_note", ""):
                await self.note(note, "grey50")
            self._model_note = note

    async def turn(self, text, shown=None, retract=None):
        """retract — the user's original text: Esc before the model answers returns it to the input."""
        # what earlier turns read from the queues can't be returned; our own (from self.queue) goes with the turn
        own, live = [], []
        for e in self._pending_input:
            (live if any(x is e["text"] for x in e["q"]) else own).append(e)
        self._pending_input = live
        self._retract = {"text": retract, "answered": retract is None, "go": False, "marks": len(self.marks),
                         "title": self.title_, "w": [w for e in own if e["q"] is self.queue for w in e["w"]]}
        retracted = False
        self._turn_event_seq += 1
        turn_event_id = self._turn_event_seq
        self.emit_event("turn_start", turn=turn_event_id)
        prev_answer = next((x for x in reversed(self.msgs) if x.get("role") == "assistant"), None)
        self._turn_request = text   # files it names are deliverables, not probe files (scratch_note)
        # The catalog is part of the cached prefix and is sent in every request, so a mode switch or
        # a plain answer cannot change the prefix and force a full re-read. tool_choice=none still
        # keeps a conversational follow-up from calling tools.
        self._use_tool_schemas, self._allow_tool_calls = tool_request_policy(
            text, self.msgs, self.hctx, self._tool_schemas_enabled, S["mode"])
        self._tool_schemas_enabled = self._use_tool_schemas
        self._use_repo_map = prompt_needs_repo_map(text)
        self._turn_deep = bool(config.DEEP_THINK_RE.search(str(text or "")))
        if self._turn_deep and S["effort"] in ("low", "medium"):
            await self.note("✻ deep thinking for this turn (asked in the message)", "grey50")
        self.compact_gate.reset()
        self._turn_metrics = {"model_ms": 0, "model_calls": 0, "tool_ms": 0, "tool_calls": 0, "tools": {}, "tool_ms_by_name": {}, "cost": 0.0, "finished_by_tool": False}
        self._invalid_tool_retries = 0
        self._think_retries = 0
        self._promise_nudged = False
        self._think_looped = False
        self._turn_had_mutating_or_exec = False
        self.todos, self.todo_w = [], None
        self.task_progress_guard.reset()
        self.loopguard.reset()
        self._todo_final_nudges = 0
        self._length_continues = 0   # text replies cut at the output limit and continued, in this turn
        self._empty_retried = False  # one silent retry of a completely empty reply per turn
        if self.progress_w:
            self.progress_w.reset()
        self.set_busy("Thinking…")
        await self.follow_loaded_model()
        self._btw_ready = False   # a /btw asked now starts after the first step: the last request is stale
        try:
            note = f"<mode_notice>Mode switched to {S['mode']}.</mode_notice>\n" if S["last_mode"] not in (None, S["mode"]) else ""
            note, self._resume_note = getattr(self, "_resume_note", "") + note, ""
            S["last_mode"] = S["mode"]
            # A reasoning model thinks in the language of the last user message; Cyrillic costs about
            # twice the tokens, so the hint rides on every message (append-only, cache-safe).
            lang = f' think_in="{config.THINK_LANG}"' if config.THINK_LANG else ""
            wrapped = f'{note}<user_input mode="{S["mode"]}"{lang}>{text}</user_input>'
            self.marks.append((len(self.msgs), time.time()))   # rewind point: state + files from checkpoints
            undo.set_turn(f"{self.sid}:{self.marks[-1][1]}")   # /undo reverts this whole step
            edit_format = edit_format_for(S["model"])
            edit_hint = ("<edit_strategy>Use whole-file editing for this model: read_file first, then write_file with the complete intended content. Preserve unrelated code.</edit_strategy>\n"
                         if edit_format == "whole" else
                         "<edit_strategy>Use apply_diff with a standard unified diff for existing-file changes. Keep unrelated blocks untouched.</edit_strategy>\n"
                         if edit_format == "udiff" else "")
            # The map and any pending side-channel context go in BEFORE the user message: a message
            # added after it would move the "last user message" boundary and re-read the turn.
            self._retract["n"] = len(self.msgs)   # a rolled-back turn also takes back its repo map
            if self._use_repo_map:
                await self.refresh_repo_map(wait=3.0)
            # Non-workspace turns keep a dirty map pending for the next code task.
            if self._late_ctx:
                attach_late(self.msgs, self._late_ctx)
                self._late_ctx = []
            self.msgs.append({"role": "user", "content": build_content(edit_hint + wrapped, text)})
            self._retract["msg"] = self.msgs[-1]
            if self.hctx:
                self.msgs.append({"role": "system", "content": "<hook>\n" + self.hctx.strip() + "</hook>"})
                self.hctx = ""
            self.title_ = self.title_ or (shown or text).replace("\n", " ")[:80]
            # on disk before the first request: an ultra stage or a long first step can run for many minutes,
            # and a window closed meanwhile must not take the request with it
            self.save_session(index=False)
            self._retract["echo"] = await self.put(SelStatic(Text("❯ " + (shown or text)), classes="user"))
            if ultra.enabled(S["effort"], S["mode"]) and self._use_tool_schemas:
                await self.ultra_stage(text, self._retract["msg"])
            turn_steps = 0
            while True:
                # A side answer streaming in the gap after the last step holds the server; it also must
                # not see compaction rewrite the prefix it forked from.
                await self.btw_settle()
                # No repo-map refresh inside the loop: a new <repo_map> user message between two
                # assistant steps re-reads the whole turn. The map is refreshed at the next turn start.
                self._late_ctx.extend(runners.take_bg_notifications())
                self._late_ctx.extend(subagent.take_background_results())
                if self._late_ctx:
                    attach_late(self.msgs, self._late_ctx)
                    self._late_ctx = []
                drain_pending(self.msgs, self.steer_queue)     # only real user input (steer) is a user message
                turn_steps += 1
                # Only the summary compacts: no trimming of old tool results (it rewrote the cached
                # history and, once the summary attempts ran out, fired again on every step).
                # No fixed number per turn (compact.Gate): a long task fills the window many times.
                if self.compact_gate.due(self.fill_pct()):
                    await self.note(f"⚠ context {self.fill_pct()}% ≥ {compact.threshold()}% — auto-compact…", "yellow")
                    if await self.compact(auto=True):
                        self.compact_gate.ok()
                    elif self.compact_gate.failed() == 2:
                        # A summarizer hiccup is usually transient: retried a few steps later, and a real
                        # overflow is still compacted by the request's own handler. The turn goes on.
                        await self.note("Auto-compact failed twice; retrying in a few steps.", "bold red")
                message_event_id = f"ui-{turn_event_id}-{time.monotonic_ns()}"
                message_started = time.monotonic()
                self.emit_event("message_start", id=message_event_id, turn=turn_event_id)
                no_think, self._think_looped = self._think_looped, False
                try:
                    async def retrying(exc, attempt, wait):
                        await self.note(f"— server failed ({llmretry.describe(exc)}); retry {attempt}/"
                                        f"{llmretry.RETRIES} in {wait:g} s (Esc stops) —", "yellow")
                    content, reasoning, calls, think_w, text_w, cut = await llmretry.call(
                        lambda: self.stream_once(think_off=self._think_retries >= 1, no_think=no_think), retrying)
                except BaseException as exc:
                    self.emit_event("message_end", id=message_event_id,
                                    ms=max(0, int((time.monotonic() - message_started) * 1000)),
                                    ok=False, error=type(exc).__name__)
                    raise
                else:
                    self.emit_event("message_end", id=message_event_id,
                                    ms=max(0, int((time.monotonic() - message_started) * 1000)), ok=True)
                self._btw_ready = True   # the server is idle while the tools run: a queued /btw goes now
                self.btw_kick()
                parsed = []
                if not calls and content:
                    # The server left the call in the text: run it instead of showing raw XML/JSON as the answer.
                    parsed, content = text_tool_calls(content, f"textcall_{time.monotonic_ns()}", self.tools())
                if not calls and not parsed and "<tool_call>" in (reasoning or "") and not split_think(content or "")[1].strip():
                    # Qwen3 sometimes emits the call before </think>; a server that splits reasoning out
                    # then returns an empty reply, and the retry for it costs a whole model step.
                    parsed = text_tool_calls(reasoning, f"thinkcall_{time.monotonic_ns()}")[0]
                if parsed:
                    # Same name/argument repair as parsed calls: "Read" -> read_file, not an unknown tool.
                    calls = self._canon_calls([{"id": x["id"], "name": x["function"]["name"], "args": x["function"]["arguments"]}
                                               for x in parsed], self.tools())
                if self._think_looped and not no_think:
                    # Thinking again from the same history loops again; one step without thinking
                    # acts on what was already worked out. Nothing is added to the history.
                    await self.note("— reasoning got stuck repeating itself — stopped, this step again without thinking", "yellow")
                    continue
                self._think_looped = False
                if cut and not content and not calls:
                    if not (reasoning or "").strip() and not self._empty_retried:
                        # Nothing at all, not even reasoning: a server hiccup, not a cut thought. The same
                        # request once more (cached prefix, nothing added); "you ran out while thinking"
                        # would be false here.
                        self._empty_retried = True
                        await self.note("— the reply came back empty — asking again", "yellow")
                        continue
                    # The reply produced neither text nor a tool call. Retry (first time without
                    # the thinking-budget cap, then again with the nudge), then a clear reason
                    # instead of a silent end of the turn.
                    if self._think_retries < 3:
                        self._think_retries += 1
                        # Sticky: without this the cap is re-enabled on the next turn and the same
                        # turn dies again with the same error.
                        self._budget_off = True
                        await self.note("— reply ended while still thinking, no answer — retrying without a thinking budget cap", "yellow")
                        # Inside the last tool result, not a new mid-turn message (cache, see attach_late).
                        attach_late(self.msgs, [THINK_CUT_NUDGE])
                        continue
                    # Say what actually limited the reply: when AGENT_THINK_BUDGET is not set, the
                    # adaptive cap (not a user-configured budget) is what cut the answer.
                    cap = f"AGENT_THINK_BUDGET={THINK_BUDGET}" if THINK_BUDGET else f"the adaptive cap (AGENT_THINK_CAP={THINK_CAP}, effort '{S['effort']}')"
                    await self.note(f"The reply used all thinking room ({self._last_budget} tokens via {cap}) while still thinking, "
                                    f"so it has no answer. Nothing was changed — ask again, or raise AGENT_THINK_CAP/AGENT_ANSWER_ROOM "
                                    f"(answer room is {answer_room(S['effort'])} tokens at this effort).", "red")
                    return
                # Do not accept a final answer while the model-authored checklist still has
                # unfinished items. Ask for one explicit final todo_write reconciliation.
                unfinished = bool(self.todos) and any(item["status"] != "done" for item in self.todos)
                if (not calls and unfinished and self._turn_had_mutating_or_exec
                        and self.task_progress_guard.strict and self._todo_final_nudges < 1):
                    self._todo_final_nudges += 1
                    for widget in (text_w, think_w):
                        if widget is not None:
                            with suppress(Exception):
                                await widget.remove()
                    attach_late(self.msgs, [
                        "[CLI TASK CHECKPOINT] Do not finalize yet. Reconcile the live task checklist "
                        "with actual work using todo_write. Mark a task done only if the work was completed; "
                        "keep failed or unfinished work active. Then provide the final answer."])
                    continue
                invalid_calls = [c for c in calls if c.get("args_error")]
                calls = [c for c in calls if not c.get("args_error")]
                for c in [*calls, *invalid_calls]:
                    self._turn_metrics["tool_calls"] += 1
                    name = c.get("name") or "unknown"
                    self._turn_metrics["tools"][name] = self._turn_metrics["tools"].get(name, 0) + 1
                if content or calls:
                    # A turn that produced only rejected calls leaves nothing to remember: a canned
                    # assistant line here becomes a few-shot example the model parrots back later,
                    # so drop the message instead of inventing speech.
                    e = {"role": "assistant", "content": content}
                    if reasoning:
                        e["reasoning_content"] = reasoning
                    if calls:
                        # Only schema-valid tool calls are written as assistant.tool_calls.
                        e["tool_calls"] = [{"id": c["id"], "type": "function",
                                            "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]
                    self.msgs.append(e)

                invalid_errors = []
                for bad in invalid_calls:
                    raw_bad = bad.get("raw_args") or bad.get("args") or "{}"
                    bad_args = repair_json(raw_bad)
                    if not isinstance(bad_args, dict):
                        bad_args = {"arguments_sent": raw_bad[:1000]}
                    bad_card = await self.put(Card(bad["name"], bad_args))
                    bad_card.finish(False, bad["args_error"])
                    invalid_errors.append(f"{bad['name']}: {bad['args_error']}")
                if invalid_errors:
                    self._invalid_tool_retries += 1
                elif calls:
                    # The budget is for malformed replies in a row: a clean batch is progress, so a
                    # long turn is not stopped by a few scattered slips.
                    self._invalid_tool_retries = 0
                too_many_invalid_retries = self._invalid_tool_retries > 4  # same budget as headless

                if not calls:
                    # A background task that ended during the final step: the turn goes on with its output.
                    self.steer_queue.extend(runners.take_bg_notifications())
                    if self.steer_queue:
                        while self.steer_queue:
                            q = self.steer_queue.pop(0)
                            self.msgs.append({"role": "user", "content": build_content(q, q)})
                        continue
                    if invalid_errors:
                        if too_many_invalid_retries:
                            await self.note("Stopped after repeated malformed tool calls; no file changes were made by those calls.", "red")
                            return
                        attach_late(self.msgs, [
                            "<tool_validation_feedback>These tool calls were not executed because their arguments were invalid:\n" +
                            "\n".join(invalid_errors) +
                            "\nRetry after correcting the arguments to match the tool schema.</tool_validation_feedback>"])
                        continue
                    visible = split_think(content or "")[1]
                    if getattr(self, "_last_finish", None) == "length" and visible.strip() \
                            and self._length_continues < LENGTH_CONTINUES:
                        # The server stopped the text at the output limit (a local server limits it by the
                        # context left): half a report is not an answer. Continue it; a full context is
                        # compacted by the normal check at the start of the next step.
                        self._length_continues += 1
                        await self.note("— the reply was cut by the output limit — continuing it", "yellow")
                        self.msgs.append({"role": "user", "content": length_continue(visible)})
                        continue
                    if broken_text_call(visible) and self._invalid_tool_retries < 4:
                        # Unreadable tool-call markup is a failed call, not an answer (no self-check request).
                        self._invalid_tool_retries += 1
                        await self.note("— the reply had a tool call in an unreadable form — asking to send it again", "yellow")
                        self.msgs.append({"role": "user", "content": BROKEN_CALL_FEEDBACK})
                        continue
                    if not self._promise_nudged and (stalled_promise(visible) or (
                            PROMISE_CHECK and self._turn_metrics["tool_calls"] and needs_judge(visible)
                            and await self.judge_reply())):
                        self._promise_nudged = True
                        await self.note("— the reply announced a step but made no tool call — asking it to continue", "yellow")
                        self.msgs.append({"role": "user", "content": PROMISE_NUDGE})
                        continue
                    test_result = await hooks.run_pending_auto_tests(self.sid, os.getcwd())
                    if test_result and not hooks.automatic_tests_passed(test_result):
                        await self.note(test_result, "red")
                        self.msgs.append({"role": "user", "content":
                            "<automatic_test_feedback>The configured test command failed after the edits. "
                            "Do not claim that tests passed. Inspect this output, fix the underlying issue if possible, "
                            "and retry verification:\n" + test_result + "\n</automatic_test_feedback>"})
                        continue
                    if test_result:
                        await self.note(test_result, "green")
                    if self.todos and any(item["status"] != "done" for item in self.todos):
                        await self.note("Task checklist still has unfinished items; they were not marked done automatically.", "yellow")
                    return
                finish_call = next((c for c in calls if c["name"] == "finish"), None)
                if finish_call and not invalid_errors:
                    if len(calls) > 1:
                        # finish is terminal: never execute sibling calls generated in the same batch.
                        for c in calls:
                            out = ("Error: finish must be called alone; no tool in this batch was executed. Re-issue finish separately."
                                   if c is finish_call else "Not executed: finish was present in the same tool batch; re-issue this call if it is still needed.")
                            card = await self.put(Card(c["name"], repair_json(c.get("args") or "{}") or {}))
                            card.finish(False, out)
                            self.msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                        attach_late(self.msgs, ["<finish_tool_feedback>Call finish by itself, only after any remaining work is complete.</finish_tool_feedback>"])
                        continue
                    test_result = await hooks.run_pending_auto_tests(self.sid, os.getcwd())
                    if test_result and not hooks.automatic_tests_passed(test_result):
                        out = "Error: configured tests failed; finish was blocked. Fix the failure and retry.\n" + test_result
                        card = await self.put(Card(finish_call["name"], repair_json(finish_call.get("args") or "{}") or {}))
                        card.finish(False, out)
                        self.msgs.append({"role": "tool", "tool_call_id": finish_call["id"], "content": out})
                        continue
                    if test_result:
                        await self.note(test_result, "green")
                    out = await self.run_call(finish_call)
                    self.msgs.append({"role": "tool", "tool_call_id": finish_call["id"], "content": out})
                    if not out.startswith(("Error", "Tool error")):
                        final_text = (repair_json(finish_call.get("args") or "{}") or {}).get("text", "")
                        self._turn_metrics["finished_by_tool"] = True
                        self.task_progress_guard.finish_by_tool()
                        if final_text:
                            self.msgs.append({"role": "assistant", "content": final_text})
                            await self.put(SelStatic(Markdown(chat_md(final_text)), classes="ans"))
                        if self.todos and any(item["status"] != "done" for item in self.todos):
                            await self.note("Task checklist still has unfinished items; finish was explicit, but incomplete tasks remain.", "yellow")
                        return
                    self.set_busy("Thinking…")
                    continue
                pending = list(calls)
                self.loopguard.batch = len(calls)
                try:
                    ask_call = next((c for c in calls if c["name"] == "ask_user"), None)
                    if ask_call and len(calls) > 1:
                        # Tool calls in one assistant message were generated before the user answered.
                        # Running those stale calls after the answer can act on the wrong assumption.
                        for c in calls:
                            if c is ask_call:
                                out = await self.run_call(c)
                            else:
                                out = "Not executed: this tool batch paused for a user answer. Re-issue the call after receiving that answer."
                                skipped = await self.put(Card(c["name"], repair_json(c.get("args") or "{}") or {}))
                                skipped.finish(False, out)
                            self.msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                            pending.remove(c)
                    elif len(calls) > 1 and all(
                        parallel_safe(c["name"], repair_json(c.get("args") or "{}") or {}) for c in calls
                    ):
                        self.set_busy(f"{len(calls)} tools in parallel")
                        outs = await asyncio.gather(*[self.run_call(c) for c in calls])
                        for c, out in zip(calls, outs):
                            self.msgs.append({"role": "tool", "tool_call_id": c["id"], "content": self.tool_content(c, out)})
                            pending.remove(c)
                    else:
                        for c in calls:
                            out = await self.run_call(c)
                            self.msgs.append({"role": "tool", "tool_call_id": c["id"], "content": self.tool_content(c, out)})
                            pending.remove(c)
                            self.set_busy("Thinking…")
                finally:
                    for c in pending:
                        self.msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "Interrupted by the user."})
                    if self.loopguard.collapse:
                        removed = collapse_loop(self.msgs, self.loopguard.collapse)
                        self.loopguard.collapse = set()
                        self.instate.shift(removed)
                        if removed:
                            await self.note(f"✂ loop: {len(removed)} repeated messages removed from the context", "grey50")
                    # Every step is on disk: a crash or a closed window mid-turn keeps what was done.
                    # The search index is updated once, at the end of the turn.
                    self.save_session(index=False)
                if invalid_errors:
                    if too_many_invalid_retries:
                        await self.note("Stopped after repeated malformed tool calls; valid calls already completed are preserved.", "red")
                        return
                    # Valid calls/results are now paired; only now add validation feedback.
                    # Into the tool results just paired above: a user message here would re-read the turn.
                    attach_late(self.msgs, [
                        "<tool_validation_feedback>These tool calls were not executed because their arguments were invalid:\n" +
                        "\n".join(invalid_errors) +
                        "\nRetry after correcting the arguments to match the tool schema.</tool_validation_feedback>"])
                    continue
        except asyncio.CancelledError:
            retracted = self.retract_turn()
            if not retracted:
                await self.note("— interrupted —", "red")
            raise
        except Exception as e:
            await self.note(f"Error: {type(e).__name__}: {e}", "red")
        finally:
            self._retract = None
            # Save before any await below: a second Esc cancels the task again and would skip the
            # save at the end, so a reopened session lost the interrupted turn.
            self.save_session(index=False)
            if self.busy and not retracted:
                m = self._turn_metrics
                tool_mix = ", ".join(f"{name}×{count}" for name, count in sorted(m["tools"].items())) or "none"
                tool_times = ", ".join(
                    f"{name} {dur(ms / 1000)}" for name, ms in
                    sorted(m["tool_ms_by_name"].items(), key=lambda item: item[1], reverse=True)[:4]
                ) or "none"
                timing = (
                    f"⏱ total {dur(time.monotonic() - self.t0)} · model stream {dur(m['model_ms'] / 1000)} / "
                    f"{m['model_calls']} requests · tools {m['tool_calls']} calls / "
                    f"{dur(m['tool_ms'] / 1000)} summed exec ({tool_mix}; {tool_times}) · "
                    f"cost {cost_text(m.get('cost', 0), S['model'])}"
                    f"{' · closed by finish tool' if m.get('finished_by_tool') else ''}")
                # Kept on the turn's last answer so a session reopened from /history shows it too;
                # wire() never sends this field to the server.
                last = next((x for x in reversed(self.msgs) if x.get("role") == "assistant"), None)
                if last is not None and last is not prev_answer:   # a turn that failed before answering keeps none
                    last["turn_time"] = timing
                    self.save_session(index=False)
                await self.note(timing, "grey50")
            h = await hooks.run("Stop", {"session_id": self.sid, "cwd": os.getcwd(), "event": "Stop"})
            if h["message"]:
                await self.note(h["message"].strip(), "cyan")
            self.set_busy(None)
            self._btw_ready = True   # a /btw still queued (the turn ended or failed first) forks from the history
            self.btw_kick()
            self.save_session()
            self.emit_event("turn_end", turn=turn_event_id, complete=True)
            if self.queue and not self._draining_queue:
                self.run_worker(self.drain_queue(), exclusive=False)

    async def drain_queue(self):
        if self._draining_queue:
            return
        self._draining_queue = True
        try:
            while self.queue:
                queued = self.queue.pop(0)
                orig = next((e["orig"] for e in self._pending_input if e["text"] is queued), queued)
                await self.turn(queued, shown=orig if orig != queued else None, retract=orig)
        finally:
            self._draining_queue = False

    async def refresh_repo_map(self, wait=0.0):
        """Build the map in the background; wait up to `wait` s for it and consume it when ready.

        Call it only at the start of a turn, before the user message is appended (see turn()).
        """
        task = self._repo_map_task
        if task is None:
            if not self._repo_map_dirty:
                return
            self._repo_map_dirty = False
            self._repo_map_task_revision = self._repo_map_revision
            task = self._repo_map_task = asyncio.create_task(asyncio.to_thread(build_repo_map, os.getcwd()))
        if not task.done() and wait > 0:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), wait)
        if not task.done():
            return
        revision = self._repo_map_task_revision
        self._repo_map_task = None
        self._repo_map_task_revision = None
        try:
            mapped = task.result()
        except Exception as e:
            with suppress(Exception):
                self.notify(f"Repo map skipped: {type(e).__name__}: {e}", severity="warning")
            return
        # If files changed while the worker was scanning, discard its stale snapshot and
        # schedule a fresh one without blocking the current model/tool cycle.
        if revision != self._repo_map_revision:
            self._repo_map_dirty = True
            await self.refresh_repo_map()
            return
        if not mapped:
            return
        last = next((m.get("content", "") for m in reversed(self.msgs)
                     if m.get("role") == "user" and str(m.get("content", "")).startswith("<repo_map ")), "")
        if last != mapped:
            self.msgs.append({"role": "user", "content": mapped})

    def tools(self):
        """Stable tool schema prefix: mode-specific restrictions are enforced by plan_refuse()."""
        if not self._use_tool_schemas:
            return []
        return session_catalog(self.mcp, allowed=None, include_search=True)

    async def judge_reply(self):
        """The model checks its own short final reply (loopguard.judge_stalled); counted as a model call.
        Same messages, tools and body as the last request, so the server reuses its cache."""
        tool_specs = self.tools()
        eb = dict((getattr(self, "_last_kw", None) or {}).get("extra_body") or self.extra())
        self.set_busy("Checking the reply…")
        t0 = time.monotonic()
        stalled = await judge_stalled(self.client, wire(sanitize_tool_history(self.safe_ctx(), tool_specs)),
                                      model=S["model"], tools=tool_specs, extra_body=eb,
                                      disabled_fields=self._disabled_extra_fields_by_model.get(S["model"], set()),
                                      **sampling_for(S["model"]))
        self._turn_metrics["model_calls"] += 1
        self._turn_metrics["model_ms"] += int((time.monotonic() - t0) * 1000)
        self.set_busy("Thinking…")
        return stalled

    async def stream_once(self, think_off=False, no_think=False):
        global CTX_WINDOW, CTX_SRC
        overflow_retries, budget_retries, budget_shrink, disable_usage = 0, 0, 1, False
        disable_budget = False
        disabled_extra_fields = self._disabled_extra_fields_by_model.setdefault(S["model"], set())
        while True:
            # compact() mutates self.msgs. Build a fresh payload every attempt; otherwise
            # the retry keeps sending the oversized pre-compact history to the server.
            tool_specs = self.tools()
            source_messages = self.safe_ctx()
            api_messages = sanitize_tool_history(source_messages, tool_specs)
            eb = self.extra()
            # The budget must be computed from the same fill the bar shows, not from the stale server
            # number alone: tool results appended after the last request are not in S["ptok"], so a budget
            # built from it can exceed the room the server really has and the reply is cut mid-thinking.
            est_now = compact.est_tokens(self.msgs, self.head, compact.prefix_cost(SYSTEM, tool_specs, self.mem))
            used = compact.used_tokens(est_now, S["ptok"] or S["ctx"], self.saved_tok, self.est_sent())
            self._est_pending = est_now
            # "think hard" raises only the budget: reasoning_effort is rendered into the prompt, so changing
            # it for one turn would make the server re-read the whole conversation.
            effort = "high" if getattr(self, "_turn_deep", False) and S["effort"] in ("low", "medium") else S["effort"]
            budget = 0 if (disable_budget or think_off or no_think or self._budget_off) else think_budget(used, effort, step=quick_step(self.msgs), plan=planning_step(self.msgs))
            if no_think:
                # After a looped reasoning: a short think, not reasoning_effort=none. The effort is rendered
                # into the system turn, so switching it re-read the whole conversation (seen live at ~40k
                # tokens); the budget is a sampling field and leaves the cached prompt as it is.
                budget = LOOP_RETRY_THINK
            self._last_budget = budget
            if budget:
                eb["reasoning_budget_tokens"] = max(MIN_THINK, budget // budget_shrink)
            for field in disabled_extra_fields:
                eb.pop(field, None)
            kw = dict(model=S["model"], messages=wire(api_messages), tools=tool_specs, stream=True,
                      extra_body=eb, **sampling_for(S["model"]))
            if TOOL_CHOICE_NONE and tool_specs and not self._allow_tool_calls:
                kw["tool_choice"] = "none"
            if CACHE_DEBUG:
                await self.note(cachedebug.report(kw["messages"], tool_specs, eb, kw.get("tool_choice")), "grey50")
            if S["usage"] and not disable_usage:
                kw["stream_options"] = {"include_usage": True}
            model_started = time.monotonic()
            if not config.TUI_STREAM:
                # Plain request read through the same loop below (config.TUI_STREAM; usage comes with the reply).
                kw["stream"] = False
                kw.pop("stream_options", None)
            try:
                stream = await self.client.chat.completions.create(**kw)
                if not config.TUI_STREAM and hasattr(stream, "choices"):
                    stream = OneChunkStream(stream)
                self._last_kw = kw
                break
            except openai.BadRequestError as e:
                self._turn_metrics["model_calls"] += 1
                self._turn_metrics["model_ms"] += int((time.monotonic() - model_started) * 1000)
                message = str(e)
                if BUDGET_RE.search(message) and budget_retries < 2:
                    budget_retries += 1
                    if budget_retries == 1:
                        budget_shrink = 2
                        self.notify("Thinking budget left no room for the answer — halving it and retrying",
                                    severity="warning")
                    else:
                        disable_budget = True
                        self.notify("The server still rejects the thinking budget, sending without it",
                                    severity="warning")
                    continue
                if re.search(r"context|too long|exceed|n_ctx|prompt processing|input length|token limit", message, re.I):
                    if overflow_retries >= 2:
                        raise RuntimeError("Context overflow remains after two auto-compact retries. "
                                           "Check the server's actual context limit or reduce system/tool prompt size.") from e
                    m = CTX_SIZE_RE.search(str(e))
                    n = int(m.group(1) or m.group(2)) if m else 0
                    if n and n != CTX_WINDOW:
                        CTX_WINDOW, CTX_SRC = n, "server, from the request error"
                        config.CTX_WINDOW = n
                    self.compact_gate.done += 1
                    await self.note("— context overflow — auto-compact…", "yellow")
                    keep = None if overflow_retries == 0 else 2
                    compacted = await self.compact(keep=keep, auto=True)
                    if not compacted:
                        compacted = await self.compact(keep=1, auto=True, fallback=True)
                    if not compacted:
                        raise RuntimeError("Context overflow, but no further conversation history can be compressed. "
                                           "Reduce the system/tool prompt or correct AGENT_CTX to match the server.") from e
                    overflow_retries += 1
                    continue
                candidates = set(kw["extra_body"])
                if "stream_options" in kw:
                    candidates.add("stream_options")
                rejected = unsupported_optional_fields(message, candidates) or set()
                retry = False
                if "stream_options" in rejected and "stream_options" in kw and not disable_usage:
                    S["usage"] = False
                    disable_usage = True
                    retry = True
                    rejected.discard("stream_options")
                new_fields = (rejected & set(kw["extra_body"])) - disabled_extra_fields
                if new_fields:
                    disabled_extra_fields.update(new_fields)
                    retry = True
                    self.notify("Server rejected optional request field(s): " + ", ".join(sorted(new_fields)) +
                                "; retrying while preserving remaining cache settings", severity="warning")
                if retry:
                    continue
                detail = message.replace("\n", " ")
                raise RuntimeError(
                    "API rejected the request with HTTP 400. No cache/sampling options were removed "
                    "because the error did not identify an unsupported optional parameter. "
                    f"Check the request/history. Server detail: {detail[:1200]}"
                ) from e
        content = reasoning = ""
        request_usage = None
        calls, think_w, text_w, collapsed, last, finish = {}, None, None, False, 0.0, None
        checked = 0   # reasoning length at the last loop check
        # Until the first token the server is reading the prompt, not thinking: on a long local
        # context that is often the longest wait, and it must not look like the model is stuck.
        self.set_busy("Reading context…")
        try:
            async for ch in stream:
                if getattr(ch, "usage", None):
                    raw_usage = getattr(ch.usage, "model_dump", lambda: {})()
                    request_usage = {"prompt_tokens": ch.usage.prompt_tokens or 0,
                                     "completion_tokens": ch.usage.completion_tokens or 0,
                                     "cached_tokens": cached_tokens(raw_usage)}
                    S["ptok"] = request_usage["prompt_tokens"]
                    S["ctx"] = S["ptok"] + request_usage["completion_tokens"]
                    self._est_sent = getattr(self, "_est_pending", 0)   # the estimate of the request just measured
                    S["cached"] = request_usage["cached_tokens"]
                    self.saved_tok = 0     # this request was sent after the trim: the server number already excludes it
                    if CACHE_DEBUG:
                        await self.note(f"cache-debug: server reused {request_usage['cached_tokens']:,} of "
                                        f"{request_usage['prompt_tokens']:,} prompt tokens", "grey50")
                if not ch.choices:
                    continue
                if ch.choices[0].finish_reason:
                    finish = ch.choices[0].finish_reason
                d = ch.choices[0].delta
                x = d.model_extra or {}
                r = x.get("reasoning_content") or x.get("reasoning")
                if r:
                    reasoning += r
                    self.set_busy("Thinking…")
                    if not content and not calls and len(reasoning) - checked >= 64:
                        checked = len(reasoning)
                        if think_loop(reasoning):
                            # Left alone it repeats until the thinking budget is gone (thousands of tokens).
                            self._think_looped = True
                            reasoning = squash_loops(reasoning)
                            with suppress(Exception):
                                await stream.aclose()   # stop generation on the server too
                            break
                if d.content:
                    content += d.content
                    self.set_busy("Answering…")
                    if self._retract and not self._retract["answered"] and split_think(content)[1].strip():
                        self._retract["answered"] = True   # the answer is already on screen: Esc only interrupts
                if d.tool_calls and self._retract:
                    self._retract["answered"] = True
                for tc in d.tool_calls or []:
                    c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    c["id"] = tc.id or c["id"]
                    if tc.function:
                        c["name"] += tc.function.name or ""
                        c["args"] += tc.function.arguments or ""
                        track_tool_call(c, tc.function.arguments or "")
                    self.set_busy(tool_call_status(c, len(calls), TITLES))
                if time.monotonic() - last < 0.08:
                    continue
                last = time.monotonic()
                think_w, text_w, collapsed = await self.paint(content, reasoning, think_w, text_w, collapsed)
        except asyncio.CancelledError:
            self._turn_metrics["model_calls"] += 1
            self._turn_metrics["model_ms"] += int((time.monotonic() - model_started) * 1000)
            with suppress(Exception):
                await stream.aclose()   # stop generation on the server right away, not just here
            if content:
                # With its reasoning, as every other reply (keep all reasoning): without it the next request
                # differed from what the server generated at this message.
                e = {"role": "assistant", "content": content}
                if reasoning:
                    e["reasoning_content"] = reasoning
                self.msgs.append(e)
            raise
        if config.TUI_STREAM and not self._think_looped:
            # A stream that stopped without a finish reason was cut, not answered: half a reply used to be
            # taken as the final answer. Raised before anything is added to the history (llmretry retries).
            llmretry.stream_ended(finish)
        if request_usage:
            request_cost = usage_cost(request_usage, S["model"])
            self.session_cost += request_cost
            self._turn_metrics["cost"] = self._turn_metrics.get("cost", 0.0) + request_cost
        await self.paint(content, reasoning, think_w, text_w, collapsed, final=True)
        self._turn_metrics["model_calls"] += 1
        self._turn_metrics["model_ms"] += int((time.monotonic() - model_started) * 1000)
        out = self._canon_calls([calls[i] for i in sorted(calls)], tool_specs, finish)
        # No text and no tool call at all: the model stopped at a random point while still
        # thinking. Servers often report "stop" (or nothing) instead of "length", so do not
        # trust finish_reason here — an empty reply must never end the turn silently.
        cut = not content and not out
        self._last_finish = finish
        return content, reasoning, out, think_w, text_w, cut

    def tool_content(self, c, out):
        """Tool message content: an image read_file result carries the picture (util.tool_content)."""
        return tool_content(c["name"], repair_json(c.get("args") or "{}") or {}, out)

    def _canon_calls(self, out, tool_specs, finish=None):
        """Advertised tool names and normalized arguments for the calls of one reply."""
        for i, c in enumerate(out):
            c["id"] = c["id"] or f"call_{len(self.msgs)}_{i}"
            c["name"] = canonical_tool_name(c["name"], tool_specs, repair_json(c["args"] or "{}"))
            raw_args = c["args"] or "{}"
            original_args = repair_json(raw_args)
            _parsed, canonical, arg_error = normalize_tool_arguments(c["name"], raw_args, tool_specs, finish)
            c["args"] = canonical
            if original_args != _parsed:
                c["raw_args"] = raw_args   # lets the result explain path/type/alias repairs to the model
            if arg_error:
                c["raw_args"] = raw_args   # keep what the model sent: the canonical value is "{}"
                c["args_error"] = arg_error
        return out

    async def paint(self, content, reasoning, think_w, text_w, collapsed, final=False):
        tt, vis = split_think(content)
        think = (reasoning + tt).strip()
        if think and think_w is None:
            inner = SelStatic("")
            think_w = Collapsible(inner, title="✻ Reasoning", collapsed=False)
            think_w.inner = inner
            await self.put(think_w)
        if think_w is not None:
            think_w.inner.update(Text(think, style="italic"))
        if vis.strip() and text_w is None:
            text_w = await self.put(SelStatic("", classes="ans"))
        if text_w is not None:
            text_w.update(Markdown(chat_md(vis.strip())))
            if think_w is not None and not collapsed:
                think_w.collapsed, collapsed = True, True
        if final and think_w is not None:
            think_w.collapsed = True
        if self.chat.follow:
            self.chat.scroll_end(animate=False)
        return think_w, text_w, collapsed

    # ── tools ──
    def plan_refuse(self, name, a):
        """Plan mode: digging in files is fine, touching the disk is not."""
        if S["mode"] != "plan":
            return None
        if name in EDIT_TOOLS:
            return "Error: plan mode — editing is forbidden. Give the plan and stop, then /run to implement."
        if name == SHELL_TOOL and WRITE_PS.search(a.get("command") or ""):
            return "Error: plan mode — this command changes the disk. Put it in the plan, do not run it."
        if name == "py_run" and permissions.dangerous_python(a.get("code") or ""):
            return "Error: plan mode — this code may change files or execute a destructive command. Put it in the plan, do not run it."
        return None

    def needs_approval(self, name, a, permission=None):
        if self.plan_refuse(name, a):
            return False
        # The prepared permission decision is reused across the approval/execute pipeline;
        # do not re-read mutable rules for the same tool call.
        k = permission if permission is not None else permissions.check(name, key_of(a))
        # Hard safety checks take precedence over broad allow rules. In AUTO mode only the
        # irreversible ones still confirm: ordinary work (editing, writing output) runs silently.
        why = permissions.dangerous(name, a.get("command") or "")
        if name == "py_run":
            why = why or permissions.dangerous_python(a.get("code") or "", auto=S["auto"])
        if why and (not S["auto"] or why in permissions.SERIOUS):
            return True
        if k == "ask":
            return True   # explicit ask rules override allow rules and AUTO mode
        if k == "allow":
            return False
        if name in ("ask_user", "read_file", "grep", "web_search", "fetch_url", "py_source", "subagent", "bg_output", "bg_kill", "todo_write", "finish", "tool_search"):
            return False
        if name == SHELL_TOOL and ps_readonly(a.get("command") or ""):
            return False
        if name == "browser" and a.get("action") in browser_tool.READ_ACTIONS and not a.get("task"):
            return False   # looking at a page changes nothing
        if name.startswith("mcp__"):
            return True
        if S["mode"] == "plan":
            return False  # read-only powershell / py_run: no confirmation
        if S["mode"] == "accept-edits" and name in EDIT_TOOLS:
            return False  # edits without confirmation, everything else is asked
        return not S["auto"]

    async def run_call(self, c):
        name = c["name"]
        a = repair_json(c["args"] or "{}")
        if not isinstance(a, dict):
            a = {}
            arg_error = c.get("args_error") or (
                f"Error: invalid JSON arguments for {name}; no tool was run. Retry with valid JSON."
            )
        else:
            arg_error = c.get("args_error") or validate_tool_arguments(name, a, self.tools())
        if arg_error:
            # Show the failed call honestly, but don't draw a fake file diff or touch the disk.
            card = await self.put(Card(name, a))
            card.finish(False, arg_error)
            return arg_error
        if name != "todo_write":
            checkpoint_error = self.task_progress_guard.blocked_reason(name, a, self.todos)
            if checkpoint_error:
                card = await self.put(Card(name, a))
                card.finish(False, checkpoint_error)
                return checkpoint_error
        if name == "todo_write":
            validation_error = validate_todos(a.get("todos"))
            if validation_error:
                card = await self.put(Card(name, a))
                card.finish(False, validation_error)
                return validation_error
            if self.todo_w is None:
                self.todo_w = await self.put(TodoPanel())
            card = self.todo_w
            old_tasks = [{"content": item["content"].strip(), "status": item["status"]}
                         for item in self.todos]
            self.todos = [{"content": item["content"].strip(), "status": item["status"]}
                          for item in normalize_todos(a["todos"])]
            self.task_progress_guard.plan_updated(old_tasks, self.todos)
            card.update_list(self.todos)
            if self.progress_w:
                self.progress_w.set_tasks(self.todos)
        else:
            card = await self.put(Card(name, a))
        r = self.plan_refuse(name, a)
        if r:
            card.finish(False, r)
            # Refused calls count for the loop guard too (as in headless): a small model hammering a
            # protected path or a refused command used to go on until the step limit.
            return r + self.loopguard.after(name, a, r, False)
        h = await hooks.run("PreToolUse", {"session_id": self.sid, "cwd": os.getcwd(), "event": "PreToolUse",
                                           "tool_name": name, "tool_input": a})
        if h["input"]:
            a = h["input"]
            rewritten_error = validate_tool_arguments(name, a, self.tools()) if isinstance(a, dict) else                 f"Error: PreToolUse hook returned invalid arguments for {name}; no tool was run."
            if rewritten_error:
                card.finish(False, rewritten_error)
                return rewritten_error
        if h["message"]:
            await self.note(h["message"].strip(), "cyan")
        if h["decision"] == "deny":
            card.finish(False, "blocked by hook")
            msg = "Error: blocked by a hook (PreToolUse)."
            return msg + self.loopguard.after(name, a, msg, False)
        for f, body in memory.rules_for(key_of(a)):          # path-scoped rules, injected once per rule
            if str(f).lower() not in self.rules_seen:   # lower case: instate.seen() gives them so after a rewind
                self.rules_seen.add(str(f).lower())
                self.instate.add("rule", f, len(self.msgs))
                self._late_ctx.append(f"<rule {f}>\n{body}\n</rule>")
        for f, body in memory.docs_for(key_of(a)):           # folder instruction file, injected once per folder
            k = str(f).lower()
            if k not in self.mem_files and k not in self.docs_seen:
                self.docs_seen.add(k)
                self.instate.add("doc", f, len(self.msgs))
                self._late_ctx.append(memory.doc_block(f, body))
        k = permissions.check(name, key_of(a))
        if k == "deny":
            msg = permissions.deny_message(name, key_of(a))
            card.finish(False, "protected path" if "protected path" in msg else "blocked by /deny rule")
            return msg + self.loopguard.after(name, a, msg, False)
        if name == "cloud_code" and k == "ask":
            # The cloud helper immediately opens a dedicated approval screen showing the full exact payload.
            k = "allow"
        pre = self.loopguard.before(name, a)
        if pre:
            card.finish(False, pre)
            return pre
        if name == "ask_user":
            self.set_busy("Waiting for your answer…")
            ans = await self.push_screen(Pick(a.get("question", ""), a.get("options") or []), wait_for_dismiss=True)
            card.finish(bool(ans), f"→ {ans}" if ans else "no answer")
            return ans or "The user did not answer."
        if name == "exit_plan_mode":
            ch = await self.push_screen(Pick("Plan ready. Start implementing?", ["Yes, implement", "No, keep planning"],
                                             False, a.get("plan") or ""), wait_for_dismiss=True)
            if ch and ch.startswith("Yes"):
                if S["mode"] == "plan":
                    self.set_mode(S["pre_plan"])
                card.finish(True, "plan approved")
                return f"Plan approved, mode is {self.cur_mode()}. Start implementing."
            card.finish(False, "plan not approved")
            return "The user did not approve the plan. Stay in plan mode and do not touch the disk."
        if h["decision"] != "allow" and self.needs_approval(name, a, permission=k):
            self.set_busy("Waiting for your answer…")
            why = permissions.explain(name, key_of(a))
            body = a.get("command") or a.get("path") or json.dumps(a, ensure_ascii=False)
            if card.diff:
                body = str(card.diff)[:600]
            ch = await self.push_screen(Pick(f"It {why} — is that OK?", ["Yes", "No", "Yes, always (this exact call)"], False, body),
                                        wait_for_dismiss=True)
            if ch and ch.startswith("Yes, always"):
                permissions.add("allow", f"{name}(^{re.escape(key_of(a))}$)")  # only this exact call (anchored: not "git status; …")
            elif ch != "Yes":
                card.finish(False, "rejected")
                msg = "The user rejected the action."
                return msg + self.loopguard.after(name, a, msg, False)
        event_call_id = str(c.get("id") or f"ui-tool-{time.monotonic_ns()}")
        event_started = time.monotonic()
        event_ok = False
        self.emit_event("tool_execution_start", id=event_call_id, tool=name, input=a, decision="allow")
        try:
            self.set_busy(f"Running: {TITLES.get(name, name)}…")
            undo.set_message_id(c.get("id"))
            # The card must show what really landed on disk, not what the model asked for.
            before = disk_text(a["path"]) if name in EDIT_TOOLS and isinstance(a.get("path"), str) else None
            named = watched_files(name, a)
            snap = await self.watch_files(self.fwatch.before, os.getcwd(), named) if named is not None else None
            try:
                out = await self.exec_tool(name, a)
            finally:
                undo.set_message_id(None)
            if snap is not None:
                # Any file a shell command, script, subagent or MCP tool changed gets the same diff card as an edit.
                changes = await self.watch_files(self.fwatch.diff, os.getcwd(), named, snap) or []
                # /undo also reverts what a command, a script or a subagent changed (only edit tools made
                # checkpoints): the bytes before the call, exactly; a new file is deleted.
                for kind, path, _old, _new in changes:
                    if kind == "new":
                        undo.snap(path, "", new=True, message_id=c.get("id"))
                    elif path in self.fwatch.old_raw:
                        undo.snap(path, self.fwatch.old_raw[path], message_id=c.get("id"))
                for kind, path, old, new in changes[:20]:
                    await self.put(ChangeCard(name, kind, path, old, new))
                if len(changes) > 20:
                    await self.note(f"… {len(changes) - 20} more files changed", "grey50")
                if name in (SHELL_TOOL, "py_run") and isinstance(out, str):
                    out += scratch_note(changes, os.getcwd(), self._turn_request)
                # Kept on the calling answer so /history shows the same cards; wire() never sends it.
                owner = next((x for x in reversed(self.msgs) if x.get("role") == "assistant"
                              and any(t.get("id") == c.get("id") for t in x.get("tool_calls") or [])), None)
                if changes and owner is not None:
                    owner.setdefault("file_changes", {})[c["id"]] = saved_changes(changes)
            if c.get("raw_args"):
                notice = normalization_notice(repair_json(c.get("raw_args")), a)
                if notice:
                    out = notice + "\n" + str(out)
            # Subagents are enforced read-only; treating one as a mutation caused an unnecessary
            # repo-map rebuild and strict checklist nudge after every delegated investigation.
            if name in EDIT_TOOLS or name == "py_run" or (name == SHELL_TOOL and not ps_readonly(a.get("command") or "")):
                self._repo_map_revision += 1
                self._repo_map_dirty = True
            ok = not out.startswith(("Error", "Tool error"))
            if ok and name in EDIT_TOOLS and memory.is_instruction(a.get("path")):
                self._late_ctx.append(memory.updated_block(a["path"]))
            # A failed edit also shows "no changes on disk" instead of the diff it proposed.
            after = disk_text(a["path"]) if before is not None else None
            if before is not None and not card.disk_change(before, after) \
                    and ok and "nothing changed" not in out:
                out += "\nWarning: the file on disk is byte-for-byte the same as before this call."
            if before is not None and after is not None and before != after \
                    and max(len(before), len(after)) <= KEEP_TEXT:
                # /history draws the same diff: rebuilt from the call alone, it compared the disk with itself.
                owner = next((x for x in reversed(self.msgs) if x.get("role") == "assistant"
                              and any(t.get("id") == c.get("id") for t in x.get("tool_calls") or [])), None)
                if owner is not None:
                    owner.setdefault("edit_changes", {})[c["id"]] = [before, after]
            if ok and (name in EDIT_TOOLS or name == "py_run"
                       or (name == SHELL_TOOL and not ps_readonly(a.get("command") or ""))):
                self._turn_had_mutating_or_exec = True
            out += self.loopguard.after(name, a, out, ok)
            self.task_progress_guard.observe_tool(name, a, ok)
            if self.todos and is_verification_tool(name, a):
                out += (
                    "\n\n[Task checklist checkpoint] Verification has returned. Before the final answer, "
                    "call todo_write to reflect the actual result: mark verification done only if it passed; "
                    "otherwise keep it active and investigate the failure."
                )
            h2 = await hooks.run("PostToolUse", {"session_id": self.sid, "cwd": os.getcwd(), "event": "PostToolUse",
                                                 "tool_name": name, "tool_input": a, "tool_response": out})
            if h2["message"]:
                await self.note(h2["message"].strip(), "cyan")
            if h2["context"]:
                out += "\n[hook]\n" + h2["context"].strip()
            out = normalize_tool_output(out, config.output_limit(name))
            card.finish(ok, out)
            event_ok = ok
            # the diff is already drawn in the card itself: a second SelStatic(card.diff) here duplicated the edit in the chat
            return out
        finally:
            self.emit_event("tool_execution_end", id=event_call_id, tool=name, ok=event_ok,
                            ms=max(0, int((time.monotonic() - event_started) * 1000)))

    async def watch_files(self, fn, *args):
        """Run a FileWatch step off the UI thread; a failure only loses the display, never the tool call."""
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception:
            return None

    async def background_done(self, message):
        """Display completion immediately and steer its full output into the next model call.

        During a turn the notice is declined (False) and rides inside the next tool result: a user
        message between two assistant steps moves the chat template's last-user boundary and the
        server re-reads the whole turn."""
        await self.note(message, "cyan")
        if self.busy:
            return False
        self.steer_queue.append(message)

    async def exec_tool(self, name, a):
        started = time.monotonic()
        try:
            return await self._exec_tool_impl(name, a)
        finally:
            metrics = getattr(self, "_turn_metrics", None)
            if metrics is not None:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                metrics["tool_ms"] += elapsed_ms
                metrics["tool_ms_by_name"][name] = metrics["tool_ms_by_name"].get(name, 0) + elapsed_ms

    async def _exec_tool_impl(self, name, a):
        try:
            if not isinstance(a, dict):
                return f"Error: arguments for {name} must be a JSON object"
            arg_error = validate_tool_arguments(name, a, self.tools())
            if arg_error:
                return arg_error
            if name == "todo_write":
                return tool_todo(a.get("todos"))
            if name == SHELL_TOOL:
                if a.get("run_in_background"):
                    result = runners.bg_start(a["command"])
                    match = re.search(r"Background id (\d+): started", result)
                    if match:
                        asyncio.create_task(runners.watch_background(match.group(1), self.background_done))
                    return result
                return await runners.run_ps(a["command"], a.get("timeout"), promote=True)
            if name == "read_file":
                return await asyncio.to_thread(tool_read, a["path"], a.get("start_line"), a.get("end_line"), a.get("offset"), a.get("limit"))
            if name == "edit_file":
                return await asyncio.to_thread(tool_edit, a["path"], a["old_text"], a["new_text"])
            if name == "apply_diff":
                return await asyncio.to_thread(tool_apply_diff, a["path"], a["diff"])
            if name == "patch":
                from ..tools.fs import tool_patch
                return await asyncio.to_thread(tool_patch, a["path"], a["blocks"])
            if name == "tool_search":
                if not self.mcp.deferred_active:
                    return "MCP deferral is inactive; the connected MCP schemas are already available. Call the desired mcp__ tool directly."
                return self.mcp.tool_search(a.get("name"))
            if name == "finish":
                if not FINISH_TOOL_ENABLED:
                    return "Error: finish tool is disabled by AGENT_FINISH_TOOL=0."
                return "Turn closed by finish tool."
            if name == "grep":
                return await asyncio.to_thread(tool_grep, a["pattern"], a.get("path") or ".", a.get("glob"), a.get("context"), a.get("output_mode"))
            if name == "glob":
                return await asyncio.to_thread(tool_glob, a["pattern"], a.get("path") or ".", a.get("limit", 100))
            if name == "code_mode":
                async def dispatch(inner_name, inner_args):
                    return await self.run_call({"id": f"code-mode-{int(time.time() * 1000)}", "name": inner_name,
                                                 "args": json.dumps(inner_args, ensure_ascii=False)})
                return await run_code(a["code"], dispatch, allowed_tools=[spec["function"]["name"] for spec in self.tools()])
            if name == "write_file":
                return await asyncio.to_thread(tool_write, a["path"], a["content"])
            if name == "cloud_code":
                return await cloud.run_tool(a, source="parent")
            if name == "web_search":
                return await web.search(a["query"])
            if name == "fetch_url":
                return await web.fetch(a["url"])
            if name == "py_source":
                return await asyncio.to_thread(tool_pysrc, a["symbol"], a.get("max_lines"))
            if name == "py_run":
                return await runners.run_py(a["code"], promote=True)
            if name == "browser":
                if a.get("action") and not a.get("task"):     # one step here; a task goes to a browser helper
                    return await browser_tool.act(a, read_only=S["mode"] == "plan")
                tools = self.tools()
                return await browser_tool.delegate(a, self.sid, subagent.fork_prefix(self.msgs, self.mem, tools), tools,
                                                   S["model"], read_only=S["mode"] == "plan")
            if name == "subagent":
                tools = self.tools()
                shared = {"parent_sid": self.sid, "prefix": subagent.fork_prefix(self.msgs, self.mem, tools),
                          "tools": tools, "model": S["model"],
                          "report_chars": a.get("report_chars"), "agent": a.get("agent")}
                if a.get("background"):
                    return "Error: delegated model work is synchronous by design to protect local GPU/RAM. Retry with background=false."
                if a.get("batch_id"):
                    out = await subagent.continue_batch(a["batch_id"], **{k: v for k, v in shared.items() if k != "agent"})
                elif a.get("tasks") is not None:
                    out = await subagent.fan_out(a["tasks"], **{k: v for k, v in shared.items() if k != "agent"},
                                                  agent=a.get("agent"), allow_write=True)
                elif a.get("prompt"):
                    out = await subagent.run_child(a["prompt"], **shared, allow_write=False)
                else:
                    return "Error: provide either a non-empty prompt or a tasks array with 2–6 tasks."
                h = await hooks.run("SubagentStop", {"session_id": self.sid, "cwd": os.getcwd(),
                                                     "event": "SubagentStop", "agent": a.get("agent") or "general",
                                                     "tool_response": out})
                if h["context"]:
                    hook_context = str(h["context"]).strip()
                    # Keep the control marker/batch_id intact so the parent can continue the queue.
                    hook_context = hook_context[:300] if (a.get("tasks") is not None or a.get("batch_id")) else hook_context
                    out += f"\n[hook]\n{hook_context}"
                if a.get("tasks") is not None or a.get("batch_id"):
                    # Child report is capped inside subagent.run; do not clip the batch continuation
                    # instruction, which is needed for the parent-driven next step.
                    return out
                return subagent.limit_report(out, a.get("report_chars"))
            if name == "remember":
                f, line = memory.note(a["note"], a.get("type") or "project", a.get("why") or "",
                                      a.get("apply") or "", a.get("scope") or "project")
                return f"memory saved in {f}: {line}" if f else f"Error: {line}"
            if name == "skill":
                x, body = skills.skill(a["name"], a.get("args") or "")
                return clip(f"skill {a['name']} ({x['path']}):\n{body}") if x else f"Error: no skill {a['name']}"
            if name == "bg_output":
                return await runners.bg_wait(a["id"], a.get("wait"))
            if name == "bg_kill":
                return runners.bg_kill(a["id"])
            if name.startswith("mcp__"):
                return clip(await self.mcp.call(name, a))
            return f"Error: unknown tool {name}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
