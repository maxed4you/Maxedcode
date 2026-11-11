"""Headless mode (like -p in Claude Code): python local_agent.py --print "prompt" [--json].

No UI, everything is auto-approved, the answer goes to stdout. Flags:
  --output-format text|json|stream-json   (json = result+usage, stream-json = JSONL, one event per line)
  --max-turns N                            loop limit
  --allowed-tools "grep,read_file,..."     only these tools
  --continue / -c                          continue the last session of this cwd
Hooks from settings.json work here too (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
SubagentStop, PreCompact, Stop).
"""
import asyncio
import json
import os
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from openai import AsyncOpenAI, BadRequestError
from . import cloud, compact, hooks, memory, permissions, skills, subagent, state
from . import config, instruction_state, llmretry, ultra
from .config import API_KEY, BASE_URL, BUDGET_RE, COMPACT_AT, CTX_SIZE_RE, ENV_BLOCK, FINISH_TOOL_ENABLED, MIN_THINK, MODEL, SETTINGS, SYSTEM, cost_text, edit_format_for, effort_body, summary_effort, max_turns_for, sampling_for, think_budget, quick_step, planning_step, THINK_CUT_NUDGE, top_k_for, min_p_for, usage_cost
from .util import attach_late, build_content, cached_tokens, tool_content, clip, last_session, load_session, normalize_tool_output, repair_json, save_session, server_ctx, session_file, split_think, wire
from .tools import BUILTIN, WRITE_PS, WRITE_PY, last_assistant_has_tool_calls, parallel_safe, prompt_needs_repo_map, prompt_needs_tools, ps_readonly
from .tools import browser, runners, web
from .tools.mcp import MCP
from .tools.todo import normalize as normalize_todos, tool_todo
from .tools.fs import attach_files, tool_apply_diff, tool_edit, tool_glob, tool_grep, tool_read, tool_write
from .tools.catalog import session_catalog
from .tools.code_mode import run_code
from . import cachekey
from .shelltool import NAME as SHELL_TOOL
from .cachekey import extra_body as cache_extra_body
from .request_compat import unsupported_optional_fields
from .streaming import STREAM_USAGE, stream_completion
from .cachedebug import report as cache_debug_report
from .tools.pysrc import tool_pysrc
from .loopguard import LoopGuard, PROMISE_NUDGE, collapse as collapse_loop, judge_stalled, needs_judge, stalled_promise
from .repo_map import build_repo_map
from .filewatch import FileWatch, scratch_note, watched_files
from .events import EventEmitter, EventMetrics
from .tool_validation import (canonical_tool_name, normalize_tool_arguments, sanitize_tool_history,
                              validate_required_arguments, validate_tool_arguments, normalization_notice,
                              repair_tool_arguments, name_did_you_mean, unknown_tool_error, text_tool_calls,
                              broken_text_call, BROKEN_CALL_FEEDBACK, arguments_text, content_text)

MAX_TURNS = 50
ALLOWED = set()
FORMAT = "text"
MCPS = None
# AGENT_EFFORT, else the TUI's last /effort (state.json): the effort is rendered into the system turn, so the
# same level keeps the prefix cache shared with the CLI. Without any the Qwen template reasons at xhigh.
EFFORT = config.norm_effort(state.S["effort"])
# The desktop window's mode switch (Manual / Accept edits / Auto / Plan, as the TUI's): set, the prompt is wrapped
# like the TUI's, as <user_input mode="plan|act">, with a <mode_notice> when it changed since the chat's last message.
_APP_MODE = (os.environ.get("AGENT_MODE") or "").strip().lower()
WINDOW_MODE = {"plan": "plan", "auto": "act", "manual": "act", "accept-edits": "act"}.get(_APP_MODE)
if WINDOW_MODE:
    state.S["mode"] = WINDOW_MODE       # subagents and ultra read it
# Manual and Accept edits: the window answers approvals (an "approval" event out, the decision back as a file in
# AGENT_APPROVAL_DIR, written by the window's server), as the TUI asks on its screen.
APPROVE_MODE = _APP_MODE if _APP_MODE in ("manual", "accept-edits") and os.environ.get("AGENT_APPROVAL_DIR") else ""
APPROVAL_DIR = os.environ.get("AGENT_APPROVAL_DIR") or ""
# tools that never ask (the TUI's needs_approval list): reading, asking, planning
NO_ASK = {"ask_user", "read_file", "grep", "glob", "web_search", "fetch_url", "py_source", "subagent", "bg_output",
          "bg_kill", "todo_write", "finish", "tool_search", "skill"}
EDITS = {"edit_file", "write_file", "patch", "apply_diff"}
ASSUME = False       # --assume: ask_user is answered with 'decide yourself' instead of pausing the run
YES = False          # --yes: do not refuse dangerous commands
GUARD = LoopGuard()
FILES = FileWatch(texts=False)   # which files a shell command or script created in the project
REQUEST = [""]                   # the user's request of this run: files it names are deliverables
BG_WARNED = set()        # background tasks the model was told will stop when the run exits
DOCS_SEEN = set()      # folder instruction files in the memory message (they stay in the prefix)
INSTATE = instruction_state.InstructionState()   # folder docs and path rules injected later, with their position
LIVE = []                # [msgs, sid, name] of the running run: saved when the run dies on an exception
EDIT_NAMES = {"edit_file", "write_file", "patch", "apply_diff"}
T = {"model_ms": 0, "model_calls": 0, "tool_ms": 0, "tool_calls": 0,
     "tools": {}, "tool_ms_by_name": {}, "cost": 0.0, "finished_by_tool": False,
     "stream_ms": 0, "wall_ms": 0,
     "gen_tokens": 0, "gen_ms": 0, "read_tokens": 0, "read_ms": 0}  # accumulated request metrics
EVENTS = EventEmitter()
EVENT_METRICS = EventMetrics()
RUN_STARTED = 0.0
USAGE = {}               # last real server usage; the fill bar needs it, not only the estimate
EST_SENT = 0             # our estimate of the request USAGE measured: the growth since then is added to it
SAVED = 0                # tokens freed since that measurement (nothing frees them now: stays 0)
HEAD = 1                 # index where the history starts: everything before it is the cached prefix
PREFIX = []              # snapshot of that prefix (system + memory + hook context)
HISTORY = []             # the run's history (compaction rewrites it in place): subagents fork from it
GATE = compact.Gate()    # when auto-compact may run: no fixed number per run (a long task fills the window often)
TODOS = []               # the model's live checklist (todo_write): restored after a compaction, checked at the end
OVERFLOW_RE = re.compile(r"context|too long|exceed|n_ctx|prompt processing|input length|token limit", re.I)


def speed_parts(r, usage, m_ms):
    """Split one request's time into reading the prompt and generating, for honest tokens/s in the window.

    The server's own timings (llama.cpp) are exact; otherwise a streamed reply splits at its first token; a
    request with neither counts as generation only."""
    tm = getattr(r, "timings", None) or (getattr(r, "model_extra", None) or {}).get("timings") or {}
    first = getattr(r, "first_ms", None)
    gen_n = usage.get("completion_tokens") or 0
    new_n = max(0, (usage.get("prompt_tokens") or 0) - (usage.get("cached_tokens") or 0))
    if isinstance(tm, dict) and tm.get("predicted_ms"):
        gen_ms, read_ms = tm["predicted_ms"], tm.get("prompt_ms") or 0
        gen_n, new_n = tm.get("predicted_n") or gen_n, tm.get("prompt_n") or new_n
    elif isinstance(first, int) and 0 < first < m_ms:
        gen_ms, read_ms = m_ms - first, first
    else:
        gen_ms, read_ms = m_ms, 0
    T["gen_tokens"] += int(gen_n)
    T["gen_ms"] += int(gen_ms)
    T["read_tokens"] += int(new_n) if read_ms else 0
    T["read_ms"] += int(read_ms)


def fill_pct(msgs, tools, freed=0):
    """Fill as a percentage of the context window, from the estimate and the last real usage.
    freed: extra tokens to treat as freed (on top of SAVED)."""
    # PREFIX[0] is the system message, already counted as SYSTEM: with the whole PREFIX the system prompt
    # (~6k tokens) was counted twice, and on a small window every compaction left the fill "at 100%".
    est = compact.est_tokens(msgs, HEAD, compact.prefix_cost(SYSTEM, tools, PREFIX[1:]))
    return compact.fill_pct(est, USAGE.get("prompt_tokens", 0), SAVED + freed, config.CTX_WINDOW, EST_SENT), est


async def summarize(client, msgs, sid):
    """Hook context first, then the model; headless has no UI, so failures go to stderr.

    A quantized model often answers with prose instead of the section template. That summary cannot
    drive continuation, so the result is checked and one nudge is retried before giving up."""
    h = await hooks.run("PreCompact", {"session_id": sid, "cwd": os.getcwd(), "event": "PreCompact"})
    if h["context"].strip():
        return h["context"].strip()
    tools = current_tool_specs()
    # The summarizer request carries the cached prefix too, so its room is smaller by that prefix.
    tr = compact.transcript(msgs, config.CTX_WINDOW,
                            compact.reserve(config.CTX_WINDOW) + compact.prefix_cost(SYSTEM, tools, PREFIX[1:]))
    # After the run's own prefix first (the cache survives the compaction); a standalone request only
    # when that one fails, e.g. the model calls a tool instead of summarizing.
    for shared in (True, False):
        try:
            summary = await ask_summary(client, tr, sid, shared=shared)
            if summary and not compact.filled(summary):
                # One nudge, not a failure: the section template is what continuation needs.
                retry = await ask_summary(client, tr, sid, prev=summary, shared=shared)
                if compact.filled(retry):
                    summary = retry
        except Exception as e:
            print(f"[compact] summarizer failed: {type(e).__name__}: {e}", file=sys.stderr)
            summary = ""
        if summary:
            return summary
    return ""


async def ask_summary(client, tr, sid, prev="", shared=True):
    """One transcript summary request. shared: after the run's prefix with its tools and body
    (see compact.transcript_request), "" on a tool call; otherwise a standalone request."""
    if shared:
        tools = current_tool_specs()
        msgs = wire(sanitize_tool_history(PREFIX, tools)) + [compact.transcript_request(state.COMPACT_PROMPT, tr)]
        kw = dict(tools=tools, extra_body=summary_body(sid))
    else:
        msgs = [{"role": "system", "content": state.COMPACT_PROMPT}, {"role": "user", "content": tr}]
        kw = dict(extra_body={**cachekey.prompt_cache(), **effort_body(summary_effort(EFFORT))})
    if prev:
        msgs += [{"role": "assistant", "content": prev}, {"role": "user", "content": compact.NUDGE}]
    r = await client.chat.completions.create(model=MODEL, messages=msgs, **kw, **sampling_for(MODEL))
    m = r.choices[0].message
    return "" if shared and m.tool_calls else split_think(str(m.content or ""))[1].strip()


def summary_body(sid):
    """The run's own request body (effort and cache key) with the summary budget and prefix pin."""
    eb = {"top_k": top_k_for(MODEL), "min_p": min_p_for(MODEL),
          **cache_extra_body(os.getcwd(), sid), **effort_body(EFFORT)}
    return compact.summary_body(eb, HEAD)


async def inplace_summary(client, msgs, sid):
    """Summary from the same messages the run already sent (KV cache hit): the summarizer sees the
    whole history. "" when it does not fit or the answer is unusable (then the transcript is used)."""
    tools = current_tool_specs()
    # The calibrated fill, not the bare estimate: the estimate falls behind on a long run and an
    # in-place request that does not really fit fails after a full read of the history.
    est = fill_pct(msgs, tools)[1]
    if not compact.inplace_fits(compact.used_tokens(est, USAGE.get("prompt_tokens", 0), SAVED, EST_SENT), config.CTX_WINDOW):
        return ""
    # Same tools and effort as the run: both are rendered into the system turn.
    try:
        r = llmretry.reply(await client.chat.completions.create(model=MODEL, tools=tools, extra_body=summary_body(sid), **sampling_for(MODEL),
            messages=wire(sanitize_tool_history(msgs, tools)) + [compact.inplace_request(state.COMPACT_PROMPT)]))
    except Exception as e:
        print(f"[compact] in-place summary failed, using the transcript: {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    m = r.choices[0].message
    out = "" if m.tool_calls else str(m.content or "").strip()
    return out if compact.filled(out) else ""


async def compact_history(client, msgs, sid, fallback=False):
    """Rewrite the history as one summary + kept tail, reusing the existing anchor.
    fallback: no model summary -> a summary built from the history itself (context overflow)."""
    cut = compact.auto_cut(msgs, HEAD)
    if cut is None or cut <= HEAD:
        return False
    # opencode rule: while the whole history fits inside (window - reserve) it is summarized as it
    # is. Only when it does not fit is the history cut to the kept tail.
    window = config.CTX_WINDOW
    room = window - compact.reserve(window)
    whole = compact.without_tail(msgs, HEAD)
    part = whole if compact.est_tokens(whole, HEAD, compact.prefix_cost(SYSTEM, current_tool_specs(), PREFIX[1:])) <= room else msgs[HEAD:cut]
    summary = await inplace_summary(client, msgs, sid)
    if summary:
        cut = compact.step_cut(msgs, cut)    # it saw the tail: keep only the last step verbatim
    else:
        summary = await summarize(client, part, sid)
    if not summary and fallback:
        summary = compact.basic_summary(part)
        print("[compact] the summarizer failed; using a summary built from the history itself", file=sys.stderr)
    if not summary:
        return False
    a = compact.anchor(msgs) or f"{sid}-summary"
    tail = [m for m in msgs[cut:] if not (m.get("role") == "user"
            and str(m.get("content") or "").startswith("<pending_work>"))]
    body = compact.anchor_body(summary, msgs[HEAD:cut])
    msgs[:] = PREFIX + [{"role": "user", "content": f'<compact id="{a}">\n{body}\n</compact>'}] + tail
    GUARD.forget_reads()   # what it read before is gone from its context: reading again is allowed
    INSTATE.compacted(cut, len(PREFIX) + 1)   # docs/rules that went into the summary come again on the next touch
    pending = [f"- [{t['status']}] {t['content']}" for t in TODOS if t.get("status") != "done"]
    if pending:      # same as the TUI: the checklist outlives the summary
        msgs.append({"role": "user", "content": "<pending_work>\n" + "\n".join(pending) + "\n</pending_work>"})
    return True


def flags():
    a = sys.argv[1:]
    def val(n, d=None):
        return a[a.index(n) + 1] if n in a and a.index(n) + 1 < len(a) else d
    return {"format": val("--output-format") or ("json" if "--json" in a else "text"),
            "max_turns": int(val("--max-turns") or max_turns_for(MODEL)),
            "allowed": {t.strip() for t in (val("--allowed-tools") or "").split(",") if t.strip()},
            "continue": "--continue" in a or "-c" in a,
            "session": session_flag(val("--session")),
            "btw": "--btw" in a,
            "assume": "--assume" in a, "yes": "--yes" in a}


def session_flag(sid):
    """--session ID: continue that session, or start it under that id (a GUI keeps one per chat).
    The id becomes a file name, so only letters, digits, '-' and '_' are accepted."""
    if sid is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid):
        raise SystemExit(f"--session: invalid id {sid!r} (letters, digits, '-' and '_' only)")
    return sid


async def judge_reply(client, msgs, tools, eb, disabled_fields):
    """The model checks its own short final reply (loopguard.judge_stalled); counted as a model call."""
    t0 = time.monotonic()
    stalled = await judge_stalled(client, wire(sanitize_tool_history(msgs, tools)), model=MODEL, tools=tools,
                                  extra_body=eb, disabled_fields=disabled_fields, **sampling_for(MODEL))
    T["model_calls"] += 1
    T["model_ms"] += int((time.monotonic() - t0) * 1000)
    if stalled:
        print("[promise check] the reply only announced a step; asking the model to continue", file=sys.stderr)
    return stalled


def key_of(a):
    return a.get("command") or a.get("code") or a.get("path") or a.get("task") or a.get("action") or ""


def out(x):
    if FORMAT == "stream-json":
        print(json.dumps(x, ensure_ascii=False), flush=True)


def live_delta(request_id):
    """stream-json only: the new reasoning/text of a streamed reply as `delta` events (a GUI shows the
    reply as it is written). A retried request starts its text again: `reset` tells the reader to drop
    what it showed for this id."""
    if FORMAT != "stream-json":
        return None
    seen = {"r": 0, "t": 0}

    def on_delta(reasoning, content):
        r, t = reasoning[seen["r"]:], content[seen["t"]:]
        seen.update(r=len(reasoning), t=len(content))
        if r or t:
            out({"type": "delta", "id": request_id, **({"reasoning": r} if r else {}), **({"text": t} if t else {})})

    def restart():
        """Called before every attempt: after a failed one the shown text is dropped."""
        if seen["r"] or seen["t"]:
            out({"type": "delta", "id": request_id, "reset": True})
        seen.update(r=0, t=0)
    on_delta.restart = restart
    return on_delta


def notice(kind, text):
    """A run-level note for the user (stderr), and in stream-json a `notice` event a GUI shows in the work list:
    cut (the reply hit the output limit and is being continued), empty (asked again), loop (repeats removed)."""
    print(text, file=sys.stderr)
    if FORMAT == "stream-json":
        out({"type": "notice", "kind": kind, "text": text})


def model_notice(note):
    """Which model the server has loaded, when it is not the configured one (servers.pick_model): said once per
    change, not on every turn of the window (each turn is a run of its own)."""
    seen = config.HOME / "model_note.txt"
    with suppress(OSError):
        if seen.read_text(encoding="utf-8") == note:
            return
    with suppress(OSError):
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.write_text(note, encoding="utf-8")
    if note:
        notice("model", note)


def emit_event(name, **fields):
    """Emit a normalized event; legacy stream-json aliases remain emitted separately."""
    event = EVENTS.emit(name, **fields)
    EVENT_METRICS.consume(event)
    if FORMAT == "stream-json":
        out({"type": "event", **event})
    return event


def current_tool_specs():
    """The run's tool list: the same session_catalog as the TUI, so the system turn (tools are rendered
    into it) and its prefix cache are shared between the CLI, headless runs and the desktop window.

    Mode-specific rules (plan mode, exit_plan_mode without a user) are enforced in exec_tool(), not by
    dropping schemas; bg_output/bg_kill stay with no jobs for the same reason.
    """
    return session_catalog(MCPS, allowed=ALLOWED or None, include_search=True)


def approval_reason(name, a, permission):
    """Why the window must ask before this call in Manual / Accept edits (None: it runs). The TUI's needs_approval:
    dangerous commands and explicit ask rules always ask; reads never; Accept edits lets file edits through."""
    if name == "cloud_code":     # asks on its own (ask_cloud), with the exact task the privacy guard let through
        return None
    why = permissions.dangerous(name, a.get("command") or "")
    if not why and name == "py_run":
        why = permissions.dangerous_python(a.get("code") or "", auto=False)
    if why:
        return why
    if permission == "ask":
        return permissions.explain(name, key_of(a))
    if permission == "allow" or name in NO_ASK or permissions.kind(name) == "read":
        return None
    if name == "browser" and a.get("action") in browser.READ_ACTIONS and not a.get("task"):
        return None
    if name == SHELL_TOOL and ps_readonly(a.get("command") or ""):
        return None
    if APPROVE_MODE == "accept-edits" and name in EDITS:
        return None
    return permissions.explain(name, key_of(a))


async def ask_window(name, a, why):
    """Asks the window and waits for the answer: allow, deny or always (this exact call from now on)."""
    rid = f"a{time.monotonic_ns()}"
    out({"type": "approval", "id": rid, "tool": name, "input": a, "why": why})
    answer = os.path.join(APPROVAL_DIR, rid + ".json")
    while True:
        await asyncio.sleep(0.2)
        with suppress(OSError, ValueError):
            with open(answer, encoding="utf-8") as f:
                decision = str(json.load(f).get("decision") or "deny")
            with suppress(OSError):
                os.remove(answer)
            break
    if decision == "always" and name == "cloud_code":
        with suppress(OSError, ValueError):
            cloud.set_option("autosend", "on")          # a cloud task never repeats: "always" = stop asking
    elif decision == "always":
        permissions.add("allow", f"{name}(^{re.escape(key_of(a))}$)")   # as the TUI: only this exact call, anchored
    out({"type": "approval_result", "id": rid, "decision": decision})
    return decision in ("allow", "always")


async def ask_cloud(task, source="parent"):
    """The window's CloudApprovalScreen: an approval card with the exact text that would leave this PC."""
    return await ask_window("cloud_code", {"task": task, "source": source}, "sends this text to a cloud model")


if APPROVAL_DIR:           # the window answers in every mode; without it the cloud module fails closed
    cloud.set_approval_handler(ask_cloud)


def child_activity(tool):
    """A child's progress line ("step 3 · browser click → thinking") as a `child` event: the window shows it under
    the running card, otherwise a helper's minutes of work looked like a frozen spinner."""
    async def tell(text):
        out({"type": "child", "tool": tool, "text": str(text)[:200]})

    async def think(step, text):
        # the step's reasoning: the window shows it under the card, folded
        out({"type": "child", "tool": tool, "step": step, "think": str(text)[-6000:]})
    tell.think = think
    return tell


def plan_mode():
    """Read-only run: the window's Plan mode, else the mode from settings or state."""
    return (WINDOW_MODE or str(SETTINGS.get("mode") or state.S.get("mode") or "act")).lower() == "plan"


def wrap_prompt(prompt, history):
    """The window's message for its mode. Auto goes as the plain text the user typed, as the window always sent
    it; Plan is wrapped like the TUI's (<user_input mode="plan">), and the first Auto message after Plan carries
    the switch notice, so the model knows the read-only rule is over."""
    if not WINDOW_MODE:
        return prompt
    last = next((str(m.get("content", "")) for m in reversed(history)
                 if m.get("role") == "user" and '<user_input mode="' in str(m.get("content", ""))), "")
    found = re.search(r'<user_input mode="([^"]+)"', last)
    if WINDOW_MODE == "act" and not (found and found.group(1) == "plan"):
        return prompt
    note = (f"<mode_notice>Mode switched to {WINDOW_MODE}.</mode_notice>\n"
            if found and found.group(1) != WINDOW_MODE else "")
    lang = f' think_in="{config.THINK_LANG}"' if config.THINK_LANG else ""
    return f'{note}<user_input mode="{WINDOW_MODE}"{lang}>{prompt}</user_input>'


async def exec_tool(name, a, sid=""):
    if ALLOWED and name not in ALLOWED:
        return f"Error: tool not allowed: {name} (--allowed-tools)"
    if name == "ask_user":
        return "Headless mode: no user to ask. Decide yourself and state the assumption in the answer."   # only with --assume
    if not isinstance(a, dict):
        return f"Error: invalid tool arguments for {name}; expected a JSON object. No tool was run."
    if plan_mode():
        if name in {"edit_file", "write_file", "patch", "apply_diff"}:
            return "Error: plan mode is read-only; describe the intended change without modifying files."
        # The same rule as the TUI's plan_refuse: ps_readonly() is the parallel-safety check and refused
        # every pipeline, so "Get-ChildItem | Select-Object" was blocked and the plan had no facts.
        if name == SHELL_TOOL and (WRITE_PS.search(a.get("command") or "")
                                   or permissions.dangerous(SHELL_TOOL, a.get("command") or "")):
            return "Error: plan mode is read-only; this command changes the disk. Put it in the plan, do not run it."
        if name == "py_run" and (WRITE_PY.search(a.get("code") or "") or permissions.dangerous_python(a.get("code") or "")):
            return "Error: plan mode is read-only; mutating Python is blocked."
    specs = current_tool_specs()
    # Include hidden schemas for defensive validation if a caller provides an explicit allowed tool.
    specs += [x for x in BUILTIN if x["function"]["name"] == name and x not in specs]
    if MCPS and name.startswith("mcp__"):
        specs += [x for x in MCPS._all_specs() if x["function"]["name"] == name and x not in specs]
    arg_error = validate_tool_arguments(name, a, specs)
    if arg_error:
        return arg_error
    h = await hooks.run("PreToolUse", {"session_id": sid, "cwd": os.getcwd(), "event": "PreToolUse",
                                       "tool_name": name, "tool_input": a})
    if h["decision"] == "deny":
        return "Error: blocked by a hook (PreToolUse): " + (h["message"].strip() or "")
    if h["input"]:
        a = h["input"]
    permission = permissions.check(name, key_of(a))
    if permission == "deny":
        return permissions.deny_message(name, key_of(a))
    approved = False                     # the user said yes in the window: the refusals below are for nobody-to-ask runs
    if APPROVE_MODE and not plan_mode():
        why = approval_reason(name, a, permission)
        if why and not await ask_window(name, a, why):
            return "The user rejected the action. Do not retry it; ask what to do instead or continue without it."
        approved = bool(why)
    if not YES and not approved:
        if permission == "ask" and name != "cloud_code":
            return (f"Error: NOT run: permission rule requires approval for {name}. "
                    "Headless mode cannot ask for confirmation; rerun with explicit approval after reviewing the action.")
        if name.startswith("mcp__") and permission != "allow":
            return ("Error: NOT run: MCP tools may have side effects and headless mode cannot ask for approval. "
                    "Review the MCP call and rerun with explicit approval if appropriate.")
        why = permissions.dangerous(name, a.get("command") or "")
        if not why and name == "py_run":
            # Headless is AUTO: a plain write was refused here while write_file and Set-Content ran, so the
            # model wrote JSON through PowerShell (BOM).
            why = permissions.dangerous_python(a.get("code") or "", auto=True)
        # Same line as the TUI in AUTO (permissions.SERIOUS): ordinary deletes and renames run, only
        # irreversible damage or work outside the project is refused. Refusing every delete cost a step
        # each time the model removed its own temp file (X1) and made file-organising tasks impossible.
        if why in permissions.SERIOUS:
            return (f"Error: NOT run: this action {why}. Headless mode cannot ask for confirmation. "
                    "Do not retry or work around it, and do not call ask_user (a yes would not unblock it): answer that it "
                    "was NOT done, with the exact command to run, or rerun with --yes.")
    out({"type": "tool_event", "event": "progress", "tool": name, "message": "validated; executing"})
    try:
        if name == "todo_write":
            o = tool_todo(a.get("todos"))
            if not str(o).startswith("Error"):
                TODOS[:] = [{"content": str(t.get("content") or "").strip(), "status": t.get("status")}
                            for t in normalize_todos(a.get("todos") or []) if isinstance(t, dict)]
            return o
        if name == SHELL_TOOL:
            if a.get("run_in_background"):
                result = runners.bg_start(a["command"])
                import re
                match = re.search(r"Background id (\d+): started", result)
                if match:
                    asyncio.create_task(runners.watch_background(match.group(1)))
                return result
            return await runners.run_ps(a["command"], a.get("timeout"), promote=True)
        # Reads run in threads (as in the TUI): called directly they blocked the event loop, so the
        # asyncio.gather of a parallel batch ran them one by one.
        if name == "read_file":
            return await asyncio.to_thread(tool_read, a["path"], a.get("start_line"), a.get("end_line"),
                                           a.get("offset"), a.get("limit"))
        if name == "glob":
            return await asyncio.to_thread(tool_glob, a["pattern"], a.get("path") or ".", a.get("limit", 100))
        if name == "code_mode":
            return await run_code(a["code"], lambda tool, args: exec_tool(tool, args, sid),
                                   allowed_tools=[spec["function"]["name"] for spec in current_tool_specs()])
        if name == "edit_file":
            return tool_edit(a["path"], a["old_text"], a["new_text"])
        if name == "apply_diff":
            return tool_apply_diff(a["path"], a["diff"])
        if name == "patch":
            from .tools.fs import tool_patch
            return tool_patch(a["path"], a["blocks"])
        if name == "tool_search":
            if not MCPS or not MCPS.deferred_active:
                return "Error: MCP tool deferral is not active; call the MCP tool directly."
            return MCPS.tool_search(a.get("name"))
        if name == "finish":
            if not FINISH_TOOL_ENABLED:
                return "Error: finish tool is disabled by AGENT_FINISH_TOOL=0."
            return "Turn closed by finish tool."
        if name == "grep":
            return await asyncio.to_thread(tool_grep, a["pattern"], a.get("path") or ".", a.get("glob"),
                                           a.get("context"), a.get("output_mode"))
        if name == "write_file":
            return tool_write(a["path"], a["content"])
        if name == "cloud_code":
            # The window asks (ask_cloud); plain --print fails closed unless /cloud autosend on or the env opt-in.
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
            if a.get("action") and not a.get("task"):
                # One step is done here: a helper per step cost a whole child run (15-50 s each, live 2026-11-03)
                return await browser.act(a, read_only=plan_mode())
            parent_tools = current_tool_specs()
            return await browser.delegate(a, sid, subagent.fork_prefix(HISTORY, list(PREFIX), parent_tools),
                                          parent_tools, MODEL, read_only=plan_mode(), activity=child_activity("browser"))
        if name == "subagent":
            parent_tools = current_tool_specs()
            parent_prefix = subagent.fork_prefix(HISTORY, list(PREFIX), parent_tools)
            common = {"parent_sid": sid, "prefix": parent_prefix, "tools": parent_tools,
                      "model": MODEL, "report_chars": a.get("report_chars")}
            if a.get("background"):
                return "Error: delegated model work is synchronous by design to protect local GPU/RAM. Retry with background=false."
            if a.get("batch_id"):
                return await subagent.continue_batch(a["batch_id"], **common)
            if a.get("tasks") is not None:
                return await subagent.fan_out(a["tasks"], **common, agent=a.get("agent"), allow_write=True)
            if not a.get("prompt"):
                return "Error: provide a non-empty prompt, a tasks array with 2–6 tasks, or a batch_id to continue a batch."
            return await subagent.run_child(a["prompt"], agent=a.get("agent"), **common, allow_write=False,
                                            activity=child_activity("subagent"))
        if name == "remember":
            f, line = memory.note(a["note"], a.get("type") or "project", a.get("why") or "",
                                  a.get("apply") or "", a.get("scope") or "project")
            return f"memory saved in {f}: {line}" if f else f"Error: {line}"
        if name == "skill":
            x, body = skills.skill(a["name"], a.get("args") or "")
            return clip(f"skill {a['name']} ({x['path']}):\n{body}") if x else f"Error: no skill {a['name']}"
        if name == "exit_plan_mode":
            return "Headless mode: no user to confirm the plan. State the plan in the answer."
        if name == "bg_output":
            return await runners.bg_wait(a["id"], a.get("wait"))
        if name == "bg_kill":
            return runners.bg_kill(a["id"])
        if name.startswith("mcp__") and MCPS:
            return clip(await MCPS.call(name, a))
        return unknown_tool_error(name, specs) or f"Error: unknown tool {name}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def guarded_exec(name, a, sid=""):
    pre = GUARD.before(name, a)
    if pre:
        return pre
    named = watched_files(name, a) if name in (SHELL_TOOL, "py_run") else None
    snap = None
    if named is not None:
        with suppress(Exception):
            snap = await asyncio.to_thread(FILES.before, os.getcwd(), named)
    o = await exec_tool(name, a, sid)
    if snap is not None:
        with suppress(Exception):
            o += scratch_note(await asyncio.to_thread(FILES.diff, os.getcwd(), named, snap), os.getcwd(), REQUEST[0])
    return o + GUARD.after(name, a, o, not o.startswith(("Error", "Tool error")))


async def timed_exec(name, a, sid="", validation_error=None, call_id=None,
                     *, result_prefix="", result_suffix="", post_hook=True):
    """Run a tool and finish its event only after output handling and PostToolUse."""
    call_id = str(call_id or f"{name}-{time.monotonic_ns()}")
    decision = "deny" if validation_error else "allow"
    if not validation_error:
        policy = permissions.check(name, key_of(a))
        if policy == "deny":
            decision = "deny"
        elif policy == "ask" and not YES:
            decision = "ask"
        why = permissions.dangerous(name, a.get("command") or "")
        if not why and name == "py_run":
            why = permissions.dangerous_python(a.get("code") or "", auto=True)
        if why in permissions.SERIOUS and not YES:
            decision = "ask"
    emit_event("tool_execution_start", id=call_id, tool=name, input=a, decision=decision)
    t0 = time.monotonic()
    if validation_error:
        emit_event("tool_execution_end", id=call_id, tool=name, ok=False, ms=0)
        return validation_error, 0  # invalid calls are rejected before any side effect
    T["tool_calls"] += 1
    T["tools"][name] = T["tools"].get(name, 0) + 1
    from .tools import undo
    undo.set_message_id(call_id)
    try:
        o = await guarded_exec(name, a, sid)
    finally:
        undo.set_message_id(None)
    o = normalize_tool_output(o, config.output_limit(name))
    if result_prefix:
        o = str(result_prefix) + "\n" + str(o)
    if result_suffix:
        o = str(o) + "\n" + str(result_suffix)
    if post_hook:
        hook = await hooks.run("PostToolUse", {
            "session_id": sid, "cwd": os.getcwd(), "event": "PostToolUse",
            "tool_name": name, "tool_input": a, "tool_response": o,
        })
        if hook["context"] or hook["message"]:
            o += "\n[hook]\n" + (hook["context"] + hook["message"]).strip()
    o = normalize_tool_output(o, config.output_limit(name))
    ms = max(0, int((time.monotonic() - t0) * 1000))
    T["tool_ms"] += ms
    T["tool_ms_by_name"][name] = T["tool_ms_by_name"].get(name, 0) + ms
    emit_event("tool_execution_end", id=call_id, tool=name,
               ok=not str(o).startswith(("Error", "Tool error")), ms=ms)
    return o, ms


BTW_ASK = ("<btw>Side question from the user. Answer it briefly, without tools; "
           "do not continue any task here: {q}</btw>")


LOCAL_COMMANDS = {
    "/undo": "undo every file change of the last turn",
    "/redo": "put back what the last /undo reverted",
    "/compact": "compress the history into a summary (the last steps stay as they are)",
    "/context": "what goes into the context: system prompt, tools, memory, history",
    "/cache": "prefix cache: cached tokens of the last request and the engine hit rate",
    "/help": "these commands",
}


RUN_PROMPT = "Continue: implement what you planned in the plan. No new plan."
INIT_PROMPT = ("Look at the project in the current folder: its folder tree, README, config and test files. Write (or improve, "
               "if it exists) its instruction file for an agent, CLAUDE.md or AGENTS.md: how to run and test this project, "
               "what lives in which folder, the conventions and what to avoid. Up to 120 lines of plain markdown, facts only, "
               "nothing you did not read.")


def expand_command(prompt):
    """The TUI's prompt commands for --print (the window expands them itself): /run, /init, /skill name args and
    /<custom command or skill> become the text the model gets; any other prompt comes back unchanged."""
    m = re.match(r"/([\w-]+)[ \t]*([\s\S]*)$", prompt.strip())
    if not m:
        return prompt
    cmd, arg = m.group(1).lower(), m.group(2).strip()
    if cmd == "run":
        return arg or RUN_PROMPT
    if cmd == "init":
        return INIT_PROMPT
    name, args = (arg.split(None, 1) + [""])[:2] if cmd == "skill" else (m.group(1), arg)
    _fm, body = skills.command_text(name, args)
    if body:
        return body
    x, body = skills.skill(name, args)
    return f'<skill name="{x["name"]}" loaded_by="user">\n{body}\n</skill>' if body else prompt


def _redo_file():
    from .tools import undo
    return undo.CKPT / undo.sid / "redo.json"


def _undo():
    """/undo for this session (the TUI's): files of the last turn back; their newer bytes are kept for /redo."""
    import base64
    from .tools import undo
    now = {}
    for rec in undo.last_group():
        with suppress(OSError):
            p = Path(rec["path"])
            now.setdefault(rec["path"], base64.b64encode(p.read_bytes()).decode() if p.exists() else None)
    done = undo.undo_step()
    if not done:
        return "Nothing to undo (no file changes recorded in this chat)."
    stack = []
    with suppress(Exception):
        stack = json.loads(_redo_file().read_text(encoding="utf-8"))
    stack.append(now)
    _redo_file().write_text(json.dumps(stack[-20:]), encoding="utf-8")
    return f"/undo restored {len(done)} file(s) of the last turn:\n" + "\n".join(done)


def _redo():
    import base64
    from .tools import undo
    stack = []
    with suppress(Exception):
        stack = json.loads(_redo_file().read_text(encoding="utf-8"))
    if not stack:
        return "Nothing to redo; run /undo first."
    files, turn = stack.pop(), f"redo-{time.time_ns()}"     # one group: the next /undo reverts the whole redo
    for path, content in files.items():
        p = Path(path)
        with suppress(OSError):
            undo.snap(path, p.read_bytes() if p.exists() else "", new=not p.exists(), turn=turn)
            if content is not None:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(base64.b64decode(content))
            elif p.exists():
                p.unlink()
    _redo_file().write_text(json.dumps(stack), encoding="utf-8")
    return f"/redo re-applied {len(files)} file(s):\n" + "\n".join(files)


def _context_report(msgs):
    from .util import tok
    tools = current_tool_specs()
    parts = {"system prompt": tok(SYSTEM), "tool descriptions": tok(json.dumps(tools, ensure_ascii=False)),
             "memory and environment": sum(tok(str(m.get("content") or "")) for m in msgs[1:HEAD]),
             "conversation": sum(tok(json.dumps(m, ensure_ascii=False)) for m in msgs[HEAD:])}
    total = sum(parts.values())
    win = config.CTX_WINDOW
    lines = [f"Context window: {win:,} tokens ({config.CTX_SRC}); in use about {total:,} ({total * 100 // max(1, win)}%)"]
    lines += [f"  {k}: {v:,}" for k, v in parts.items()]
    lines.append(f"Auto-compact at {compact.threshold()}% of the window.")
    return "\n".join(lines)


def _cache_report(msgs):
    from .util import cache_stats
    q, h = cache_stats()
    last = next((m for m in reversed(msgs) if m.get("role") == "assistant" and m.get("usage")), None)
    use = (last or {}).get("usage") or {}
    s = [f"last request: {int(use.get('prompt_tokens') or 0):,} prompt tokens, cached {cached_tokens(use):,}"
         if use else "no request in this chat yet"]
    s.append(f"engine prefix cache: {int(h):,} of {int(q):,} requests ({h * 100 // q}%)" if q else
             "the server does not publish /metrics")
    return "\n".join(s)


async def local_command(prompt, client, msgs, sid, name):
    """The TUI's local slash commands for headless and the window: run here, never sent to the model and never
    added to the history (an extra user message would move the cached prefix). -> True when handled."""
    word = prompt.strip().split(None, 1)[0].lower() if prompt.strip() else ""
    if word not in LOCAL_COMMANDS:
        return False
    if word == "/undo":
        answer = _undo()
    elif word == "/redo":
        answer = _redo()
    elif word == "/context":
        answer = _context_report(msgs)
    elif word == "/cache":
        answer = _cache_report(msgs)
    elif word == "/compact":
        ok = await compact_history(client, msgs, sid)
        if ok:
            save_session(msgs, sid, name=name)
        answer = "History compressed into a summary; the next turn reads the summary instead." if ok else \
            "Nothing to compress yet (the history is short)."
    else:
        answer = "\n".join(f"{k}  {v}" for k, v in LOCAL_COMMANDS.items())
    if FORMAT == "stream-json":
        out({"type": "result", "result": answer, "session_id": sid, "timing": dict(T), "command": word})
    elif FORMAT == "json":
        print(json.dumps({"result": answer, "session_id": sid, "command": word}, ensure_ascii=False))
    else:
        print(answer)
    return True


async def side_answer(client, msgs, sid, q):
    """--btw: one side question after the session's history (the TUI's /btw); nothing is saved.

    The request is the session's own prefix (same tools, effort and cache key) plus the question, so the
    server reads only the question; the fork point is pinned like the TUI's, so the side tail does not
    push out the checkpoint the conversation resumes from."""
    tools = current_tool_specs()
    eb = {"top_k": top_k_for(MODEL), "min_p": min_p_for(MODEL),
          **cache_extra_body(os.getcwd(), sid), **effort_body(EFFORT)}
    prefix = wire(sanitize_tool_history(msgs, tools))
    cachekey.pin(eb, len(prefix))
    ask = {"role": "user", "content": BTW_ASK.format(q=q.strip())}

    async def request():
        return await stream_completion(client, live_delta("btw"), model=MODEL, messages=[*prefix, ask],
                                       tools=tools, extra_body=eb, **sampling_for(MODEL))
    t0 = time.monotonic()
    r, _looped = await llmretry.call(request)
    m = r.choices[0].message
    answer = split_think(str(m.content or ""))[1].strip() or \
        "(no answer: the model tried to call a tool — ask again, or ask in the main chat)"
    T.update(model_calls=1, model_ms=int((time.monotonic() - t0) * 1000))
    if FORMAT == "stream-json":
        out({"type": "result", "result": answer, "session_id": sid, "timing": dict(T), "btw": True})
    elif FORMAT == "json":
        print(json.dumps({"result": answer, "session_id": sid, "btw": True}, ensure_ascii=False))
    else:
        print(answer)


async def _run_impl(prompt, as_json=False, sid=None):
    global FORMAT, ALLOWED, MCPS, ASSUME, YES
    f = flags()
    state.S["effort"] = EFFORT          # subagents read it: their prefix must match this run's
    global MODEL
    if not os.environ.get("AGENT_CTX"):
        # real window, model id (model_settings; on LM Studio / Ollama the loaded one), reasoning support
        server = await asyncio.to_thread(config.resolve_server)
    else:
        await asyncio.to_thread(config.resolve_keep_reasoning)
        server = await asyncio.to_thread(config.follow_loaded)    # never a second model loaded on LM Studio / Ollama
    MODEL = config.MODEL
    global EVENTS, EVENT_METRICS, RUN_STARTED
    FORMAT = f["format"] or ("json" if as_json else "text")
    model_notice(server.get("note") or "")
    ALLOWED = f["allowed"]
    ASSUME, YES = f["assume"], f["yes"]
    GUARD.reset()
    EVENTS = EventEmitter()
    EVENT_METRICS = EventMetrics()
    RUN_STARTED = time.monotonic()
    T.update(model_ms=0, model_calls=0, tool_ms=0, tool_calls=0, tools={}, tool_ms_by_name={}, cost=0.0,
             finished_by_tool=False, stream_ms=0, wall_ms=0, gen_tokens=0, gen_ms=0, read_tokens=0, read_ms=0)
    emit_event("agent_start", cwd=os.getcwd())
    USAGE.clear()
    global SAVED, HEAD, PREFIX, HISTORY, EST_SENT
    SAVED, EST_SENT = 0, 0
    GATE.reset()
    TODOS.clear()
    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY, **llmretry.client_options())
    sid = sid or f["session"] or (last_session() if f["continue"] else None) or time.strftime("%Y%m%d-%H%M%S")
    msgs = [{"role": "system", "content": SYSTEM}]
    HISTORY = msgs
    saved_history = []
    name = ""                 # the session's own name (/save name, or a chat renamed in the window): kept on save
    if (f["continue"] or f["session"]) and sid:
        d = load_session(sid)
        if d:
            # Keep the invariant [system prompt] -> [memory message] -> [rest of history].
            saved_history = list(d[1])
            sid, name = d[0], d[2]
    t = memory.text() + skills.listing()      # same memory + skills message as in the UI
    DOCS_SEEN.update(str(f).lower() for _, f in memory.startup_files())   # already in that message
    INSTATE.entries.clear()
    LIVE[:] = [msgs, sid, name]
    fresh = ([{"role": "system", "content": "# Memory\n" + t}] if t else []) + \
        [{"role": "user", "content": ENV_BLOCK}]          # after memory: the system turn stays shared
    resume_note = ""
    if saved_history:
        # The saved prefix (memory + env of the day it started) is sent as it was; what changed since rides
        # in this run's user message (memory.resume_prefix): rebuilt, a new date re-read the whole history.
        old_prefix, saved_history = memory.split_saved(saved_history)
        fresh, resume_note = memory.resume_prefix(old_prefix, fresh, saved_history)
    msgs.extend(dict(m) for m in fresh)
    HEAD = len(msgs)                    # history starts here; the prefix above it must stay byte-identical
    PREFIX = list(msgs)
    msgs.extend(saved_history)
    # file checkpoints per chat (the window keeps one session per chat), grouped per run: /undo reverts a turn
    from .tools import undo
    undo.set_sid(sid)
    undo.set_turn(f"{sid}-{time.time_ns()}")
    if f["btw"]:
        LIVE.clear()        # a side question saves nothing, not even when it fails
        return await side_answer(client, msgs, sid, prompt)
    if await local_command(prompt, client, msgs, sid, name):
        return
    shown_prompt = prompt              # images are attached from the paths the person typed, not from file bodies
    prompt = await asyncio.to_thread(attach_files, expand_command(prompt))
    # Run hooks before any fast path; injected hook context requires the normal model loop.
    MCPS = None
    startup_hook = await hooks.run("SessionStart", {"session_id": sid, "cwd": os.getcwd(), "event": "SessionStart", "source": "startup"})
    startup_context = bool(startup_hook["context"] or startup_hook["message"])
    if startup_context:
        msgs.append({"role": "system", "content": "<hook>\n" + (startup_hook["context"] + startup_hook["message"]).strip() + "</hook>"})
    h = await hooks.run("UserPromptSubmit", {"session_id": sid, "cwd": os.getcwd(), "event": "UserPromptSubmit", "prompt": prompt})
    if h["decision"] == "deny":
        print("blocked by hook: " + (h["message"].strip() or ""), file=sys.stderr)
        return
    # The intent is not guessed from the words (the user may write in any language), so every request
    # may use tools. MCP servers start only when some are configured: no config, no startup wait.
    needs_tools = bool(prompt_needs_tools(prompt) or last_assistant_has_tool_calls(msgs)
                       or startup_context or h["context"] or h["message"])
    MCPS = MCP() if needs_tools and MCP.cfg() else None
    if MCPS is not None:
        for n, c in MCP.cfg().items():
            MCPS.start(n, c)
        for _ in range(100):                  # wait up to 10 s until the servers connect or fail
            if not any(st["status"].startswith("connecting") for st in MCPS.srv.values()):
                break
            await asyncio.sleep(0.1)
    repo_map_enabled = prompt_needs_repo_map(prompt)
    repo_map = await asyncio.to_thread(build_repo_map, os.getcwd()) if repo_map_enabled else ""
    last_repo_map = next((m.get("content", "") for m in reversed(msgs)
                          if m.get("role") == "user" and str(m.get("content", "")).startswith("<repo_map ")), "")
    if repo_map and repo_map != last_repo_map:
        msgs.append({"role": "user", "content": repo_map})
    repo_map_dirty = False
    edit_format = edit_format_for(MODEL)
    edit_hint = ("<edit_strategy>Use whole-file editing: read_file first, then write_file with the complete intended content. Preserve unrelated code.</edit_strategy>\n"
                 if edit_format == "whole" else
                 "<edit_strategy>Use apply_diff with standard unified diff hunks for existing-file changes. Keep unrelated blocks untouched.</edit_strategy>\n"
                 if edit_format == "udiff" else "")
    msgs.append({"role": "user", "content": build_content(resume_note + edit_hint + wrap_prompt(prompt, saved_history),
                                                          shown_prompt)})
    user_msg = msgs[-1]
    if h["context"]:
        msgs.append({"role": "system", "content": "<hook>\n" + h["context"].strip() + "</hook>"})
    if ultra.enabled(EFFORT, "plan" if plan_mode() else "auto") and needs_tools:
        # Staged run before the first request: its report rides inside the unsent user message.
        async def progress(line, note=True):
            if FORMAT == "stream-json":
                out({"type": "ultra", "text": line})
            elif note:
                print(line, file=sys.stderr, flush=True)
        parent_tools = current_tool_specs()
        # Fork at this run's user message: the whole history before it is shared with the children,
        # so the first request after the staged run reads only the user message and the report.
        end = next(i for i, m in enumerate(msgs) if m is user_msg)
        report = await ultra.run(prompt, sid, subagent.fork_prefix(HISTORY, list(PREFIX), parent_tools, end=end),
                                 parent_tools, model=MODEL, progress=progress)
        if report:
            ultra.attach(user_msg, report)
    usage, answer = USAGE, ""
    invalid_retries = 0
    promise_nudged = False   # one "you announced a step but made no call" nudge per run
    todo_nudged = False      # one "the checklist still has open items" nudge per run
    cut_parts = []           # text replies cut at the output limit: the final answer is them + the last part
    empty_retried = False    # one silent retry of a completely empty reply per run
    think_retries, budget_retries, budget_shrink, disable_budget = 0, 0, 1, False
    overflow_retries = 0     # forced compactions for the current request (reset when a request succeeds)
    no_think_next = False    # the last reply's reasoning looped: the next request goes without reasoning
    disabled_extra_fields = set()
    stopped = (f"Stopped after {f['max_turns']} model turns without a final answer. "
               "The session was saved; narrow the request or continue it to finish the task.")
    # One extra step past max_turns, only for the answer: a small model often spends the last step on
    # tools anyway (R4: tests green, no report). On that step tool calls are not executed.
    for turn_index in range(f["max_turns"] + 1):
        emit_event("turn_start", turn=turn_index + 1)
        # Background results ride inside the freshest tool result; a user message here would move the
        # "last user message" boundary and make the server re-read the run. The repo map is built once
        # at the start (a mid-run refresh is the same kind of message).
        attach_late(msgs, [*runners.take_bg_notifications(), *subagent.take_background_results()])
        if turn_index and turn_index == f["max_turns"] - 1:
            # The last allowed step: without this the run ends with "Stopped after N turns" even when the
            # work is done. Rides inside the last tool result, like other late context (cache-safe).
            attach_late(msgs, ["<turn_limit>This is the last step of the run. Do not call tools. Answer now: "
                               "what you found, what you changed and verified, and what is still open.</turn_limit>"])
        if turn_index == f["max_turns"]:
            if not last_assistant_has_tool_calls(msgs):
                answer = answer or stopped      # the limit step did not run tools (retry nudge etc.): no wrap-up
                break
            attach_late(msgs, ["<turn_limit>The step limit is reached: tool calls will not run any more. "
                               "Answer now in text: what you found, what you changed and verified, and what "
                               "is still open.</turn_limit>"])
        repo_map_dirty = False
        # Keep the tool catalog in the prefix for the whole run. Dropping it on a conversational
        # follow-up changes the prompt prefix and invalidates the KV cache, so the server re-reads
        # everything. tool_choice=none keeps the fast path without breaking the cache.
        tools = current_tool_specs()
        # Compress before the request, not only after the server refuses it. The fill bar must use the
        # estimate too: the last measured usage does not include tool results appended after that request.
        pct, est = fill_pct(msgs, tools)
        # Only the summary compacts: no trimming of old tool results (see the TUI loop).
        if GATE.due(pct):
            print(f"[compact] context {pct}% >= {compact.threshold()}% -> summary", file=sys.stderr)
            if await compact_history(client, msgs, sid):
                GATE.ok()
                SAVED = 0
                USAGE.clear()
                print(f"[compact] done ({GATE.done} in this run), context {fill_pct(msgs, tools)[0]}%", file=sys.stderr)
            elif GATE.failed() == 2:
                print("[compact] summarizer failed twice, retrying in a few steps", file=sys.stderr)
        pct, est = fill_pct(msgs, tools)          # the numbers used for the budget must be post-compact
        t0 = time.monotonic()
        eb = {"top_k": top_k_for(MODEL), "min_p": min_p_for(MODEL),
              **cache_extra_body(os.getcwd(), sid), **effort_body(EFFORT)}
        if not disable_budget:
            # The budget is taken from the same fill the bar shows, so it can never be larger than the
            # room the server really has; otherwise the reply is cut off while still thinking.
            b = think_budget(compact.used_tokens(est, USAGE.get("prompt_tokens", 0), SAVED, EST_SENT),
                             EFFORT, step=quick_step(msgs), plan=planning_step(msgs))
            if b:
                eb["reasoning_budget_tokens"] = max(MIN_THINK, b // budget_shrink)
        if no_think_next:
            # The last reply's reasoning looped and was cut: this step gets a short think (as in the TUI).
            # Not reasoning_effort=none: the effort is rendered into the system turn, and switching it
            # makes the server re-read the whole run; the budget leaves the cached prompt as it is.
            eb["reasoning_budget_tokens"] = config.LOOP_RETRY_THINK
            no_think_next = False
        for field in disabled_extra_fields:
            eb.pop(field, None)
        allow_calls = needs_tools or last_assistant_has_tool_calls(msgs)
        T["model_calls"] += 1
        request_id = f"{sid}-request-{T['model_calls']}"
        emit_event("message_start", id=request_id, turn=turn_index + 1)
        try:
            kw = {"tool_choice": "none"} if config.TOOL_CHOICE_NONE and tools and not allow_calls else {}
            api_messages = wire(sanitize_tool_history(msgs, tools))
            if config.CACHE_DEBUG:
                print(cache_debug_report(api_messages, tools, eb, kw.get("tool_choice")), file=sys.stderr)
            live = live_delta(request_id)

            async def request():
                if config.HEADLESS_STREAM:
                    if live:
                        live.restart()
                    return await stream_completion(client, live, model=MODEL, messages=api_messages,
                                                   tools=tools, extra_body=eb, **sampling_for(MODEL), **kw)
                # see config.HEADLESS_STREAM: a streamed reply made the server re-read it in the next request
                return llmretry.reply(await client.chat.completions.create(
                    model=MODEL, messages=api_messages, tools=tools, extra_body=eb, **sampling_for(MODEL), **kw)), False

            def retrying(exc, attempt, wait):
                print(f"(server failed: {llmretry.describe(exc)}; retry {attempt}/{llmretry.RETRIES} in {wait:g} s)",
                      file=sys.stderr)
            r, looped = await llmretry.call(request, retrying)
        except BadRequestError as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            T["model_ms"] += elapsed
            emit_event("message_end", id=request_id, ms=elapsed, ok=False, error=str(e)[:300])
            if BUDGET_RE.search(str(e)) and budget_retries < 2:
                budget_retries += 1
                if budget_retries == 1:
                    budget_shrink = 2
                    print("thinking budget left no room for the answer, halving it and retrying", file=sys.stderr)
                else:
                    disable_budget = True
                    print("thinking budget rejected again, sending without it", file=sys.stderr)
                continue
            if OVERFLOW_RE.search(str(e)):
                m = CTX_SIZE_RE.search(str(e))
                n = int(m.group(1) or m.group(2)) if m else 0
                if not n:
                    n = await asyncio.to_thread(server_ctx) or 0
                if n and n != config.CTX_WINDOW:
                    config.CTX_WINDOW = n
                print(f"[compact] server context overflow (fill {fill_pct(msgs, tools)[0]}%, n_ctx={config.CTX_WINDOW}) -> forced compact", file=sys.stderr)
                if overflow_retries < 2 and await compact_history(client, msgs, sid, fallback=True):
                    overflow_retries += 1
                    GATE.done += 1
                    SAVED = 0
                    USAGE.clear()
                    continue
                # A crash here loses the whole run: report the reason and save the session instead.
                answer = ("Context overflow remains after two auto-compact retries. Check the server's actual "
                          "context limit or reduce the system/tool prompt size.")
                print(f"[compact] {answer}", file=sys.stderr)
                break
            rejected = unsupported_optional_fields(str(e), set(eb) | {"stream_options"}) or set()
            if "stream_options" in rejected and STREAM_USAGE[0]:
                STREAM_USAGE[0] = False
                print("server rejected stream_options; streaming without usage", file=sys.stderr)
                continue
            new_fields = rejected - disabled_extra_fields
            if new_fields:
                disabled_extra_fields.update(new_fields)
                print("server rejected optional request field(s): " + ", ".join(sorted(new_fields)) +
                      "; retrying while preserving remaining cache settings", file=sys.stderr)
                continue
            raise
        m_ms = int((time.monotonic() - t0) * 1000)
        overflow_retries = 0
        T["model_ms"] += m_ms
        T["stream_ms"] += m_ms
        emit_event("message_end", id=request_id, ms=m_ms, ok=True)
        u = getattr(r, "usage", None)
        if u:
            # Keep the shared dict: fill_pct() needs the last real measurement, so rebinding
            # `usage` to a fresh dict here would leave the fill bar stuck on the estimate.
            usage.clear()
            usage.update({"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                          "cached_tokens": cached_tokens(u.model_dump() if hasattr(u, "model_dump") else {})})
            SAVED = 0     # this request was sent after the trim: the server number already excludes it
            EST_SENT = est
            if config.CACHE_DEBUG:
                print(f"cache-debug: server reused {usage['cached_tokens']:,} of "
                      f"{usage['prompt_tokens']:,} prompt tokens", file=sys.stderr)
            T["cost"] += usage_cost(usage, MODEL)
            speed_parts(r, usage, m_ms)
        m = r.choices[0].message
        if looped:
            # Nothing is added to the history (TUI parity): the next request repeats this step without
            # reasoning. A cut reply had no text and no call, so there is nothing to keep but the loop.
            print("(reasoning got stuck repeating itself: stopped, this step again without thinking)", file=sys.stderr)
            no_think_next = True
            continue
        # name null, arguments as an object, content as a list of parts: shapes real servers send
        calls = [{"id": tc.id or f"call_{T['model_calls']}_{i}", "type": "function",
                  "function": {"name": str(getattr(tc.function, "name", None) or ""),
                               "arguments": arguments_text(getattr(tc.function, "arguments", None))}}
                 for i, tc in enumerate(m.tool_calls or [])]
        content = content_text(m.content)
        if not calls and content:
            # The server left the call in the text: run it instead of printing raw XML/JSON as the answer.
            calls, content = text_tool_calls(content, f"textcall_{T['model_calls']}", tools)
        if not calls and not split_think(content)[1].strip():
            # Qwen3 sometimes emits the call before </think>; a server that splits reasoning out
            # then returns an empty reply, and the retry for it costs a whole model step.
            thought = getattr(m, "reasoning_content", None) or (getattr(m, "model_extra", None) or {}).get("reasoning_content") or ""
            if "<tool_call>" in thought:
                calls = text_tool_calls(thought, f"thinkcall_{T['model_calls']}")[0]
        validation_errors = {}
        normalization_notices = {}
        for call in calls:
            fn = call["function"]
            fn["name"] = canonical_tool_name(fn["name"], tools, repair_json(fn["arguments"] or "{}"))
            raw_args = fn["arguments"]
            original_args = repair_json(raw_args)
            _parsed, canonical, arg_error = normalize_tool_arguments(
                fn["name"], raw_args, tools, getattr(r.choices[0], "finish_reason", None))
            if config.CACHE_DEBUG and canonical != raw_args:
                # The history then differs from what the model generated: the server may re-read this reply.
                print(f"cache-debug: {fn['name']} arguments rewritten for the history: "
                      f"{raw_args[:400]!r} -> {canonical[:400]!r}", file=sys.stderr)
            fn["arguments"] = canonical
            note = normalization_notice(original_args, _parsed)
            if note:
                normalization_notices[call["id"]] = note
            if arg_error:
                validation_errors[call["id"]] = arg_error
        if turn_index == f["max_turns"] and calls:
            # The wrap-up step asked for tools again: they do not run (and are not saved unpaired, so
            # --continue works); whatever text came with them is the answer.
            answer = content.strip() or stopped
            if content.strip():
                msgs.append({"role": "assistant", "content": answer})
            print(f"(wrap-up step after {f['max_turns']} turns still called tools; not executed)", file=sys.stderr)
            break
        e = {"role": "assistant", "content": content}
        if calls:
            e["tool_calls"] = calls
        reasoning = getattr(m, "reasoning_content", None) or (getattr(m, "model_extra", None) or {}).get("reasoning_content")
        msgs.append({**e, "reasoning_content": reasoning} if reasoning else e)   # wire() decides whether it is sent
        if FORMAT == "stream-json":
            out({"type": "assistant", "message": e, "usage": usage, "model_ms": m_ms})
        finish_call = next((c for c in calls if c["function"]["name"] == "finish"), None)
        if finish_call:
            if len(calls) > 1:
                for c in calls:
                    msg = ("Error: finish must be called alone; no tool in this batch was executed. Re-issue finish separately."
                           if c is finish_call else "Not executed: finish was in the same tool batch; re-issue if still needed.")
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": msg})
                    if FORMAT == "stream-json":
                        out({"type": "tool_result", "id": c["id"], "tool": c["function"]["name"], "content": msg, "ms": 0})
                attach_late(msgs, ["<finish_tool_feedback>Call finish by itself, only after any remaining work is complete.</finish_tool_feedback>"])
                continue
            if finish_call["id"] not in validation_errors:
                args = repair_json(finish_call["function"].get("arguments") or "{}") or {}
                test_result = await hooks.run_pending_auto_tests(sid, os.getcwd())
                if test_result and not hooks.automatic_tests_passed(test_result):
                    o = "Error: configured tests failed; finish was blocked. Fix the failure and retry.\n" + test_result
                    msgs.append({"role": "tool", "tool_call_id": finish_call["id"], "content": o})
                    if FORMAT == "stream-json":
                        out({"type": "tool_result", "id": finish_call["id"], "tool": "finish", "content": o, "ms": 0})
                    continue
                o, ms = await timed_exec("finish", args, sid, validation_errors.get(finish_call["id"]),
                                         result_suffix=test_result)
                msgs.append({"role": "tool", "tool_call_id": finish_call["id"], "content": o})
                if FORMAT == "stream-json":
                    out({"type": "tool_result", "id": finish_call["id"], "tool": "finish", "content": o, "ms": ms})
                if not o.startswith(("Error", "Tool error")):
                    answer = str(args.get("text") or "")
                    T["finished_by_tool"] = True
                    msgs.append({"role": "assistant", "content": answer})
                    break
                continue
        if not calls:
            answer = content
            # A task may finish during this model request, so drain results even when no task is pending now.
            background_results = subagent.take_background_results()
            if subagent.background_pending():
                completed = await subagent.wait_for_background(timeout=45)
                background_results.extend(subagent.take_background_results())
                if not completed and FORMAT == "stream-json":
                    out({"type": "tool_event", "event": "progress", "tool": "subagent", "message": "background task still running after 45 seconds; result may arrive later"})
            # A one-shot run kills its background tasks on exit: a clone or download the model "left
            # running" would be cut off silently. Say so once; the model waits (bg_output wait=N) if it
            # needs the result, or answers. Waiting here instead would hang on a server for 10 minutes.
            running = [hid for hid in runners.running_bg() if hid not in BG_WARNED]
            if running:
                BG_WARNED.update(running)
                background_results.append(
                    f"<background_still_running ids=\"{', '.join(running)}\">This one-shot run ends with your answer and "
                    "then stops these tasks. If the task needs their result, wait with bg_output(id, wait=600); "
                    "otherwise answer now and say they were stopped.</background_still_running>")
            background_results.extend(runners.take_bg_notifications())
            if background_results:
                for result in background_results:
                    msgs.append({"role": "user", "content": result})
                answer = ""
                continue
            if not answer:
                if not (reasoning or "").strip() and not empty_retried and msgs and msgs[-1].get("role") == "assistant" \
                        and not msgs[-1].get("content") and not msgs[-1].get("tool_calls"):
                    # Nothing at all, not even reasoning: a server hiccup, not a cut thought. The same request
                    # once more (the empty reply is taken back, so the bytes are identical).
                    empty_retried = True
                    msgs.pop()
                    notice("empty", "(the reply came back empty; asking again)")
                    continue
                # The reply ended with no text and no tool call. finish_reason is unreliable on
                # local servers (they often say "stop" mid-thinking), so retry regardless of it:
                # first without the thinking-budget cap, then again with the nudge.
                if think_retries < 3:
                    think_retries += 1
                    disable_budget = True   # retry without the cap: the model needs more room to think
                    msgs.append({"role": "user", "content": THINK_CUT_NUDGE})
                    continue
                answer = "No answer: the reply ended while still thinking without producing text. " \
                         "Set AGENT_THINK_BUDGET / AGENT_ANSWER_ROOM, or reasoning_budget_tokens in strata-<model>.json."
            elif getattr(r.choices[0], "finish_reason", None) == "length" and split_think(answer)[1].strip() \
                    and len(cut_parts) < config.LENGTH_CONTINUES:
                # Cut at the output limit (a local server limits it by the context left): continue it, and
                # the printed answer is the parts joined. A full context is compacted at the next step.
                cut_parts.append(split_think(answer)[1])
                notice("cut", "(the reply was cut by the output limit; continuing it)")
                msgs.append({"role": "user", "content": config.length_continue(split_think(answer)[1])})
                answer = ""
                continue
            elif broken_text_call(split_think(answer)[1]) and invalid_retries < 4:
                # Tool-call markup that no parser could read is a failed call, not an answer: say so
                # at once instead of paying for the self-check request first.
                invalid_retries += 1
                msgs.append({"role": "user", "content": BROKEN_CALL_FEEDBACK})
                answer = ""
                continue
            elif not promise_nudged and (stalled_promise(split_think(answer)[1]) or (
                    config.PROMISE_CHECK and T["tool_calls"] and needs_judge(split_think(answer)[1])
                    and await judge_reply(client, msgs, tools, eb, disabled_extra_fields))):
                promise_nudged = True
                msgs.append({"role": "user", "content": PROMISE_NUDGE})
                answer = ""
                continue
            if (not todo_nudged and turn_index < f["max_turns"] - 1 and any(t.get("status") != "done" for t in TODOS)
                    and any(n in T["tools"] for n in ("edit_file", "write_file", "patch", "apply_diff", SHELL_TOOL, "py_run"))):
                # Same checkpoint as the TUI: a final answer with open checklist items after real work is
                # often an early stop on a long task. Once per run; the answer stays in the history.
                todo_nudged = True
                open_items = "\n".join(f"- [{t['status']}] {t['content']}" for t in TODOS if t.get("status") != "done")
                msgs.append({"role": "user", "content":
                    "<task_checkpoint>Your checklist still has open items:\n" + open_items + "\nIf work is left, "
                    "continue it now. If it is all done, update todo_write and give the final answer; if an item "
                    "cannot be done, say so in the answer.</task_checkpoint>"})
                answer = ""
                continue
            test_result = await hooks.run_pending_auto_tests(sid, os.getcwd())
            if test_result and not hooks.automatic_tests_passed(test_result):
                msgs.append({"role": "user", "content":
                    "<automatic_test_feedback>The configured test command failed after the edits. "
                    "Do not claim that tests passed. Inspect this output, fix the underlying issue if possible, "
                    "and retry verification:\n" + test_result + "\n</automatic_test_feedback>"})
                answer = ""
                continue
            if test_result:
                print(test_result.splitlines()[0], file=sys.stderr)
            if cut_parts:
                answer = "".join(cut_parts) + split_think(answer)[1]
            break
        ask = next((c for c in calls if c["function"]["name"] == "ask_user"), None)
        if ask and not ASSUME:
            # One-shot run: a question to the user ENDS the run. It is the answer; nothing else is executed.
            qa = repair_json(ask["function"]["arguments"] or "{}") or {}
            opts = qa.get("options") or []
            # The text written before the question is part of the answer (real M1 run: the findings were
            # lost, only the question was returned); with ask_user({}) it is the question itself (M17).
            question, text = str(qa.get("question") or "").strip(), content.strip()
            question = "\n\n".join(x for x in (text, question) if x and not (x is question and x in text))
            answer = question + ("\n" + "\n".join(f"- {o}" for o in opts) if opts else "")
            for c in calls:      # keep every assistant tool_call paired with a tool result (no HTTP 400 on --continue)
                o = "Run paused: waiting for the user's answer." if c is ask else "Not executed: the run paused for a user question."
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": o})
                if FORMAT == "stream-json":
                    out({"type": "tool_result", "id": c["id"], "tool": c["function"]["name"], "content": o})
            break
        args_list = [repair_json(c["function"]["arguments"] or "{}") or {} for c in calls]
        names = [c["function"]["name"] for c in calls]
        late_docs = []
        for n, a in zip(names, args_list):
            for rule_path, body in memory.rules_for(key_of(a)):   # path-scoped rules, the same key as the TUI
                if not INSTATE.already("rule", rule_path):
                    INSTATE.add("rule", rule_path, len(msgs))
                    late_docs.append(f"<rule {rule_path}>\n{body}\n</rule>")
            for doc_path, body in memory.docs_for(a.get("path") or ""):   # folder instruction file, once per folder
                k = str(doc_path).lower()
                if k not in DOCS_SEEN and not INSTATE.already("doc", k):
                    INSTATE.add("doc", k, len(msgs))
                    late_docs.append(memory.doc_block(doc_path, body))
        if FORMAT == "stream-json":
            for c, n, a in zip(calls, names, args_list):
                out({"type": "tool_event", "event": "started", "id": c["id"], "tool": n, "input": a})
        GUARD.batch = len(calls)
        if len(calls) > 1 and all(parallel_safe(n, a) for n, a in zip(names, args_list)):
            outs = await asyncio.gather(*(timed_exec(
                n, a, sid, validation_errors.get(c["id"]), call_id=c["id"],
                result_prefix=normalization_notices.get(c["id"], ""))
                for c, n, a in zip(calls, names, args_list)))
        else:
            # Preserve the model's call order whenever a batch contains an operation that may
            # change state or require interaction. Concurrent writes/runs can race each other.
            outs = []
            for c, n, a in zip(calls, names, args_list):
                outs.append(await timed_exec(
                    n, a, sid, validation_errors.get(c["id"]), call_id=c["id"],
                    result_prefix=normalization_notices.get(c["id"], "")))
        if any(n in {"edit_file", "write_file", "patch", "apply_diff", "py_run", SHELL_TOOL} for n in names):
            repo_map_dirty = True
        for c, a, (o, ms) in zip(calls, args_list, outs):
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": tool_content(c["function"]["name"], a, o)})
            if c["function"]["name"] in EDIT_NAMES and not o.startswith(("Error", "Tool error")) \
                    and memory.is_instruction(a.get("path")):
                late_docs.append(memory.updated_block(a["path"]))
        attach_late(msgs, late_docs)       # after the tool results: they must follow the assistant tool_calls directly
        # The file only, after every step: a server crash used to lose the whole run (saved at the end only).
        save_session(msgs, sid, name=name, index=False)
        if GUARD.collapse:
            removed = collapse_loop(msgs, GUARD.collapse)
            GUARD.collapse = set()
            if removed:
                notice("loop", f"(loop: {len(removed)} repeated messages removed from the context)")
        for c, (o, ms) in zip(calls, outs):
            if FORMAT == "stream-json":
                out({"type": "tool_event", "event": "finished", "id": c["id"], "tool": c["function"]["name"], "ok": not o.startswith(("Error", "Tool error")), "ms": ms})
                out({"type": "tool_result", "id": c["id"], "tool": c["function"]["name"], "content": o, "ms": ms})
        if validation_errors:
            invalid_retries += 1
            errors = [f"{c['function']['name']}: {validation_errors[c['id']]}" for c in calls
                      if c["id"] in validation_errors]
            if invalid_retries > 4:
                answer = "Stopped after repeated invalid tool arguments; those calls were not executed."
                break
            # Into the tool results just added: a user message between two steps re-reads the run.
            attach_late(msgs, [
                "<tool_validation_feedback>These tool calls were rejected before execution because their arguments were invalid:\n"
                + "\n".join(errors) + "\nRetry only after correcting the required arguments.</tool_validation_feedback>"])
        else:
            # The budget is for malformed replies in a row: a clean batch resets it (same as the TUI).
            invalid_retries = 0
    else:
        # Do not return here: the common cleanup below must stop MCP servers and save the session.
        answer = stopped
        print(f"(no final answer: exceeded {f['max_turns']} turns; saving session)", file=sys.stderr)
    emit_event("turn_end", turn=turn_index + 1 if 'turn_index' in locals() else 0, complete=True)
    if MCPS is not None:
        for n in list(MCPS.srv):
            MCPS.stop(n)
    await asyncio.sleep(0.2)
    h = await hooks.run("Stop", {"session_id": sid, "cwd": os.getcwd(), "event": "Stop"})
    if h["message"]:
        print(h["message"].strip(), file=sys.stderr)
    save_session(msgs, sid, name=name)
    emit_event("agent_end", session_id=sid, ok=not str(answer).startswith(("Error", "Stopped")))
    T["wall_ms"] = max(0, int((time.monotonic() - RUN_STARTED) * 1000))
    event_timing = EVENT_METRICS.result()
    T["event_tool_calls"] = event_timing["tool_calls"]
    T["event_tool_ms"] = event_timing["tool_ms"]
    timing = {**T, "cost_label": cost_text(T.get("cost", 0), MODEL)}
    if FORMAT == "json":
        print(json.dumps({"result": answer, "usage": usage, "session_id": sid, "timing": timing}, ensure_ascii=False, indent=2))
    elif FORMAT == "stream-json":
        out({"type": "result", "result": answer, "usage": usage, "session_id": sid, "timing": timing,
             "ctx": config.CTX_WINDOW})
    else:
        print(answer)


async def run(prompt, as_json=False, sid=None):
    """Public runner that guarantees child-process cleanup on every return/cancellation path."""
    REQUEST[0] = str(prompt or "")
    LIVE.clear()
    for stream in (sys.stdout, sys.stderr):
        # A lone UTF-16 surrogate the model sent as an escape in its JSON crashed the run at the first print of
        # it; escaped, a stream-json line stays valid JSON that decodes back to the same text.
        with suppress(AttributeError, ValueError):
            stream.reconfigure(errors="backslashreplace")
    try:
        return await _run_impl(prompt, as_json=as_json, sid=sid)
    except BaseException as error:
        if LIVE:
            # A connection error or a crash ends the run: keep what was done, so --continue can resume it.
            save_session(LIVE[0], LIVE[1], name=LIVE[2])
        import openai
        if isinstance(error, (openai.APIError, *llmretry.RETRYABLE)):
            # the server is down or refused (after llmretry's retries; a stream that kept breaking, a reply with
            # no choices): one line, not a traceback; the window and the browser panel show the last stderr line
            print(f"model server error ({config.BASE_URL}): {error}", file=sys.stderr, flush=True)
            raise SystemExit(1) from None
        raise
    finally:
        if MCPS is not None:
            for server_name in list(MCPS.srv):
                with suppress(Exception):
                    MCPS.stop(server_name)
        runners.cleanup()
