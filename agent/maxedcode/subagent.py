"""Subagent: one focused side task with its own small loop (read-only),
returns findings as text. Like Task/subagent in Claude Code."""
import asyncio
import json
import os
import re
import time
import uuid
import weakref
from pathlib import Path
from copy import deepcopy
from openai import AsyncOpenAI
from .config import API_KEY, BASE_URL, MODEL, SUBAGENT_MAX_CONCURRENT, SYSTEM, CTX_WINDOW, sampling_for, top_k_for, min_p_for
from . import compact, config, hooks, permissions, skills, state
from .tools import READONLY, WRITE_PY, WRITE_PS, BUILTIN, parallel_safe
from .tools import runners, web
from .tools.fs import disk_text, tool_apply_diff, tool_edit, tool_grep, tool_patch, tool_read, tool_write
from .tools.pysrc import tool_pysrc
from openai import BadRequestError
from .config import BUDGET_RE, LOOP_RETRY_THINK, MIN_THINK, THINK_CAP, THINK_CUT_NUDGE, answer_room, think_budget
from .request_compat import unsupported_optional_fields
from .streaming import STREAM_USAGE, stream_completion
from .util import attach_late, cached_tokens, normalize_tool_output, repair_json, split_think, wire, session_file, index_session, tool_content
from .tool_validation import (broken_text_call, canonical_tool_name, cut_off_error, sanitize_tool_history,
                              text_tool_calls, validate_tool_arguments, unknown_tool_error)
from . import cachekey
from .shelltool import NAME as SHELL_TOOL
from . import cloud
from . import llmretry

SUB_SYSTEM = ("You are a subagent running one focused task. Use tools to find the answer, then report "
              "findings concisely with paths and line numbers. No questions, no file changes, no plans.")
def _env_int(name, default, low, high):
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default

MAX_STEPS = _env_int("AGENT_SUBAGENT_MAX_STEPS", 12, 1, 100)
SUB_REPORT_CHARS = _env_int("AGENT_SUB_REPORT_CHARS", 1500, 200, 10000)
# Room a forked child needs for its own history (fork_prefix). It is not a trim budget: a child trims
# only when the window itself runs out, or below AGENT_SUBAGENT_CTX when that is set explicitly.
SUB_CTX = min(CTX_WINDOW, _env_int("AGENT_SUBAGENT_CTX", 6000, 1000, max(1000, CTX_WINDOW)))
SUB_CTX_LIMIT = SUB_CTX if os.environ.get("AGENT_SUBAGENT_CTX") else None
TRIM_TO = 0.6          # share of the history budget left after a trim
# A shorter result saves less than the re-read of everything after it costs (live run: a 162-char
# error swapped for a 100-char marker made the server re-read 38k tokens, 341 s).
TRIM_MIN_CHARS = 1500
SUBAGENT_READONLY = {"read_file", "glob", "grep", "py_source", "web_search", "fetch_url", "cloud_code"}
SUBAGENT_WRITERS = {"edit_file", "write_file", "patch", "apply_diff"}
SUB_TOOLS = [t for t in BUILTIN if t["function"]["name"] in SUBAGENT_READONLY]

# Limit parallel subagent inference requests to the locally-confirmed server capacity.
# A semaphore is per event loop and is recreated if configuration changes (e.g. in tests).
_SEMAPHORES = weakref.WeakKeyDictionary()
def _inference_semaphore():
    """Hard-serialize child inference; the env flag cannot enable local concurrency."""
    loop = asyncio.get_running_loop()
    row = _SEMAPHORES.get(loop)
    if row is None or row[0] != 1:
        row = (1, asyncio.Semaphore(1))
        _SEMAPHORES[loop] = row
    return row[1]


inference_slot = _inference_semaphore   # a /btw takes the same slot: it goes between two child steps

# child session id -> its last request as sent: a /btw in that child's tab forks from it (cache hit)
LAST_REQUEST = {}
LAST_REQUEST_KEEP = 16


def _report_limit(value=None):
    try:
        return max(200, min(10000, int(SUB_REPORT_CHARS if value is None else value)))
    except (TypeError, ValueError):
        return SUB_REPORT_CHARS


def _report(text, limit=None):
    """Return a hard-bounded, concise fact report."""
    limit = _report_limit(limit)
    value = str(text or "(no findings)").strip()
    if len(value) <= limit:
        return value
    marker = "\n[report truncated]"
    return value[:max(0, limit - len(marker))].rstrip() + marker


def limit_report(text, limit=None):
    """Public hard cap for reports after hooks or wrappers have appended extra context."""
    return _report(text, limit)


def _prefix_messages(prefix):
    """Normalize a caller's cache prefix without altering any supplied prefix bytes."""
    if not prefix:
        return None
    copied = [dict(m) for m in prefix]
    if copied and copied[0].get("role") == "system" and copied[0].get("content") == SYSTEM:
        return copied
    return [{"role": "system", "content": SYSTEM}, *copied]


def fork_prefix(history, base, tools, end=None):
    """Prefix for a child: the parent's history up to the delegating call, or `base` when it does not fit.

    end — fork before history[end] instead (ultra: the turn's user message, before any request of the
    turn): the children then share the whole earlier history with the turn's next request.

    The engine keeps one conversation. A child that starts a new one (system + memory only) makes the
    engine drop the parent's checkpoints, and the parent's next request reads its whole history again
    (measured: 4.8k of 10.7k tokens, 25 s; 5 minutes on a 40k history). A child that continues the
    parent's history, with that point pinned (strata_prefix), leaves it in place: the parent's next
    request read 62 tokens. The child also sees what the parent already read and does not read it again.
    """
    idx = end if end is not None else next((i for i in range(len(history) - 1, -1, -1)
                if history[i].get("role") == "assistant" and history[i].get("tool_calls")), None)
    if not idx:
        return base
    h = sanitize_tool_history(history[:idx], tools)
    if not h or h[0].get("role") != "system":
        return base
    need = compact.prefix_cost(h[0].get("content", ""), tools, []) + compact.est_tokens(h, 1, 0) + SUB_CTX
    return h if need <= int(config.CTX_WINDOW * 0.85) else base


def _forked(prefix):
    """The prefix carries the parent's conversation (its user request at least), not only system + memory + env."""
    return any(m.get("role") in ("assistant", "tool") or
               (m.get("role") == "user" and not str(m.get("content") or "").startswith("<env>"))
               for m in prefix or [])


def _fixed_cost(tools, base_prefix):
    """Tokens of the shared prefix (system, tools, memory, the parent's history)."""
    return compact.prefix_cost(base_prefix[0].get("content", ""), tools, []) + compact.est_tokens(base_prefix, 1, 0)


def _trim_history(msgs, head, tools, base_prefix, ctx_limit=None):
    """Trim old tool output inside the subagent history; the shared prefix is immutable.

    Every trim rewrites a message the server already holds, and the server re-reads everything after
    it. So the budget is the window the reply leaves (ctx_limit, when given, lowers it), not a small
    fixed share: with 6000 tokens a child trimmed on every step and lost the files it had just read
    before the model saw them (live run: engine.js read four times in pieces)."""
    fixed = _fixed_cost(tools, base_prefix)
    # The immutable shared prefix is mandatory. Budget the variable subagent history separately,
    # while also respecting the actual server window: the reply (reasoning + answer) needs room too.
    room = config.CTX_WINDOW - fixed - answer_room("high") - THINK_CAP
    target = fixed + max(800, min(ctx_limit, room) if ctx_limit else room)
    def estimate():
        return compact.est_tokens(msgs, head, fixed)
    before = estimate()
    if before <= target:
        return 0
    # Once over, trim well below the budget: each trim rewrites an older message and the server re-reads
    # the child's history from there, so trimming a little on every step cost a re-read per step.
    low = fixed + int((target - fixed) * TRIM_TO)
    # Keep the delegation request and the most recent interaction intact; trim older tool results
    # first. Editing only messages at/after `head` can never mutate the parent's shared prefix.
    # Results after the last reply are not read by the model yet: they are never "old".
    last_reply = max((i for i in range(head, len(msgs)) if msgs[i].get("role") == "assistant"), default=head)
    stop = max(head, min(len(msgs) - 6, last_reply))
    trimmed = 0
    names = {c.get("id"): c.get("function", {}).get("name", "tool")
             for m in msgs for c in m.get("tool_calls", [])}
    for m in msgs[head:stop]:
        if estimate() <= low:
            break
        if m.get("role") != "tool":
            continue
        content = str(m.get("content") or "")
        if len(content) < TRIM_MIN_CHARS or content.startswith("[subagent history trimmed:"):
            continue
        marker = f"[subagent history trimmed: {names.get(m.get('tool_call_id'), 'tool')} result, {len(content)} chars; retrieve only the needed fact if required]"
        injected = re.findall(r"<injected>.*?</injected>", content, re.S)
        m["content"] = marker + ("\n" + "\n".join(injected) if injected else "")
        trimmed += 1
    # If the window itself would overflow, trim any output (oldest first, the newest last) as a last
    # resort while keeping its message/tool_call structure valid. The prefix remains byte-identical.
    if estimate() > target:
        for m in msgs[head:]:
            if estimate() <= target:
                break
            if m.get("role") == "tool":
                content = str(m.get("content") or "")
                if len(content) > 500 and not content.startswith("[subagent history trimmed:"):
                    marker = f"[subagent history trimmed: {names.get(m.get('tool_call_id'), 'tool')} result, {len(content)} chars]"
                    injected = re.findall(r"<injected>.*?</injected>", content, re.S)
                    m["content"] = marker + ("\n" + "\n".join(injected) if injected else "")
                    trimmed += 1
    return trimmed

async def _exec_call(c, a, allow_write, tool_specs, browser=None):
    """One call of a child step; a call cut off by the output limit is reported, not run."""
    if c.get("cut"):
        return c["cut"]
    out = await exec(c["name"], a, allow_write, tool_specs=tool_specs, browser=browser)
    # The same cap as the main loop: one shell listing into node_modules came back as 287k chars.
    return normalize_tool_output(out, config.output_limit(c["name"])) if isinstance(out, str) else out


async def exec(name, a, allow_write=False, tool_specs=None, browser=None):
    """browser: "act" / "read" for the browser helper (tools/browser.py), None for every other child."""
    if not isinstance(a, dict):
        return f"Error: invalid tool arguments for {name}; expected a JSON object. No tool was run."
    if name == "browser":
        if not browser:
            return "subagent: the browser is used by the browser helper only"
        from .tools import browser as browser_tool
        return await browser_tool.act(a, read_only=browser == "read")
    active_specs = tool_specs or BUILTIN
    arg_error = validate_tool_arguments(name, a, active_specs)
    if arg_error:
        return arg_error
    if name in SUBAGENT_WRITERS and not allow_write:
        return "subagent: writing is not allowed"
    key = a.get("command") or a.get("code") or a.get("path") or ""
    # the subagent goes through the same rules as the main agent: hooks, deny rules, protected paths
    h = await hooks.run("PreToolUse", {"cwd": os.getcwd(), "event": "PreToolUse", "tool_name": name,
                                       "tool_input": a, "subagent": True})
    if h["decision"] == "deny":
        return "Error: blocked by a hook (PreToolUse): " + (h["message"].strip() or "")
    if h["input"]:
        a = h["input"]
        key = a.get("command") or a.get("code") or a.get("path") or ""
    decision = permissions.check(name, key)
    if decision == "deny":
        return permissions.deny_message(name, key)
    if decision == "ask" and name != "cloud_code":
        return f"Error: permission rule requires user approval: {name}; subagents cannot request approval, so the tool was not run."
    if name == "cloud_code":
        # The cloud receives only this explicit task; the shared UI approval callback shows the exact text.
        return await cloud.run_tool(a, source="subagent")
    if name in {"subagent", "code_mode", "ask_user", "remember", "todo_write", "bg_output", "bg_kill", "finish", "tool_search", "skill", "exit_plan_mode"}:
        return f"subagent: tool {name} is not available inside read-only subagent execution"
    if name.startswith("mcp__"):
        return "subagent: MCP tools are not executed by read-only subagents"
    why = permissions.dangerous(name, a.get("command") or "")
    if not why and name == "py_run":
        # A child allowed to write may write from Python too (write_file would do the same); deleting,
        # renaming and dangerous commands still need an approval it cannot ask for.
        why = permissions.dangerous_python(a.get("code") or "", auto=bool(allow_write))
    if why:
        return f"Error: subagents cannot request approval for dangerous actions ({why}); the tool was not run."
    try:
        if name in SUBAGENT_WRITERS:
            if not allow_write:
                return "subagent: writing is not allowed"
            if name == "edit_file":
                return await asyncio.to_thread(tool_edit, a["path"], a["old_text"], a["new_text"])
            if name == "apply_diff":
                return await asyncio.to_thread(tool_apply_diff, a["path"], a["diff"])
            if name == "patch":
                return await asyncio.to_thread(tool_patch, a["path"], a["blocks"])
            return await asyncio.to_thread(tool_write, a["path"], a["content"])
        if name == "read_file":
            return tool_read(a["path"], a.get("start_line"), a.get("end_line"), a.get("offset"), a.get("limit"))
        if name == "glob":
            from .tools.fs import tool_glob
            return tool_glob(a["pattern"], a.get("path") or ".")
        if name == "grep":
            return tool_grep(a["pattern"], a.get("path") or ".", a.get("glob"), a.get("context"), a.get("output_mode"))
        if name == "py_source":
            return tool_pysrc(a["symbol"], a.get("max_lines"))
        if name == "web_search":
            return await web.search(a["query"])
        if name == "fetch_url":
            return await web.fetch(a["url"])
        if name == "py_run":
            if not allow_write and (WRITE_PY.search(a.get("code") or "") or permissions.dangerous_python(a.get("code") or "")):
                return "subagent: writing or destructive Python is not allowed"
            return await runners.run_py(a["code"])
        if name == SHELL_TOOL:
            from .tools import ps_readonly
            if not allow_write and not ps_readonly(a.get("command") or ""):
                return "subagent: only simple read-only shell commands are allowed"
            return await runners.run_ps(a["command"], a.get("timeout"))
        return unknown_tool_error(name, SUB_TOOLS) or f"Error: unknown tool {name}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

STEPS_LEFT = ("<turn_limit>Two steps are left before your report: run the checks your task asks for now, "
              "then fix what they show.</turn_limit>")


def _note_edit(owner, call_id, before, after):
    """Real before/after of an edit on the reply that made it, for the tab's diff card (UI only: wire()
    never sends it). Long texts are dropped as in the main loop's file_changes."""
    from .filewatch import KEEP_TEXT
    if after is None or before == after or len(before) > KEEP_TEXT or len(after) > KEEP_TEXT:
        return
    owner.setdefault("edit_changes", {})[call_id] = [before, after]


WRAP_UP = ("<turn_limit>This is your last step: tool calls will not run any more. Report now: what you "
           "changed (paths), what you checked and its result, and what is still open.</turn_limit>")


def _partial(head_line, msgs, head, changed):
    """Report of a child that stopped without one: what it wrote and its last words, so the parent
    does not redo (or trust) work blindly."""
    lines = [head_line]
    if changed:
        lines.append("Files changed: " + ", ".join(changed))
    said = next((split_think(str(m.get("content") or ""))[1].strip() for m in reversed(msgs[head:])
                 if m.get("role") == "assistant" and split_think(str(m.get("content") or ""))[1].strip()), "")
    if said:
        lines.append("Last note: " + said[:600])
    return "\n".join(lines)


def _child_id():
    return f"agent-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

def _call_brief(name, args):
    """One short line for a tool call on the busy line: the tool and its main argument."""
    a = args if isinstance(args, dict) else {}
    arg = next((str(a[k]) for k in ("path", "pattern", "command", "query", "url", "code") if a.get(k)), "")
    if a.get("path"):
        arg = (Path(arg).name if os.name == "nt" else re.split(r"[\\/]", arg.rstrip("\\/"))[-1]) or arg
    arg = re.sub(r"\s+", " ", arg).strip()
    return f"{name} {arg[:40] + '…' if len(arg) > 40 else arg}".strip()


async def _tell_think(activity, step, text):
    """The step's reasoning to a caller that shows it (`activity.think`: the window's browser/subagent card)."""
    think = getattr(activity, "think", None)
    if think and str(text or "").strip():
        try:
            await think(step, str(text).strip())
        except Exception:
            pass


async def _tell(activity, text):
    """Report live progress to the caller; a broken callback never stops the subagent."""
    if activity:
        try:
            await activity(text)
        except Exception:
            pass


def _cache_note(usage):
    """"12.1k/12.4k cached" from a response's usage: how much of the prompt the server's cache served."""
    if not usage:
        return ""
    data = usage.model_dump() if hasattr(usage, "model_dump") else (usage if isinstance(usage, dict) else {})
    prompt = int(data.get("prompt_tokens") or 0)
    if not prompt:
        return ""
    return f"{cached_tokens(data) / 1000:.1f}k/{prompt / 1000:.1f}k cached"


def _live_view(session_id):
    """Streams the reply being written into the child's tab (state only: the session file is written per step)."""
    if not session_id:
        return None
    return lambda think, text: state.upsert_child_session(session_id, live_think=think, live_text=text)


async def _complete(client, kw, on_delta=None):
    """One streamed child request -> (response, looped). Taken one at a time (inference_slot); a server that
    refuses the thinking budget or stream usage gets the request again without that field."""
    for _ in range(3):
        try:
            async with _inference_semaphore():
                if not config.SUBAGENT_STREAM:   # see config.TUI_STREAM: plain request, no live view
                    return await client.chat.completions.create(**kw), False
                return await stream_completion(client, on_delta=on_delta, **kw)
        except BadRequestError as exc:
            message, body = str(exc), kw.get("extra_body") or {}
            if BUDGET_RE.search(message) and "reasoning_budget_tokens" in body:
                kw["extra_body"] = {k: v for k, v in body.items() if k != "reasoning_budget_tokens"}
                continue
            if STREAM_USAGE[0] and "stream_options" in (unsupported_optional_fields(message, {"stream_options"}) or ()):
                STREAM_USAGE[0] = False
                continue
            raise
    raise RuntimeError("the server kept rejecting the subagent request")


def _child_title(index, prompt):
    clean = re.sub(r"\s+", " ", str(prompt or "")).strip()
    return f"Agent {index}: {clean[:58]}" if clean else f"Agent {index}"

def _persist_child_session(session_id, title, parent_sid, task, msgs=None, head=0, status="queued",
                           step=0, activity="queued", report="", cwd=None, index=None):
    """Persist only the child's variable transcript; the shared cache prefix is not duplicated."""
    cwd = str(cwd or os.getcwd())
    history = [dict(m) for m in (msgs or [])[head:]] if msgs is not None else [
        {"role": "user", "content": str(task or "")}
    ]
    now = time.time()
    path = session_file(session_id, cwd)
    payload = {
        "id": session_id, "title": title, "name": title, "ts": now, "cwd": cwd,
        "msgs": history, "is_subagent": True, "parent_sid": str(parent_sid or ""),
        "agent_status": status, "agent_step": int(step or 0), "agent_activity": str(activity or ""),
        "agent_task": str(task or ""), "agent_report": str(report or ""),
        "agent_index": index,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        if status in {"completed", "failed", "cancelled"}:
            index_session(history, session_id, cwd=cwd, path=path, ts=now)
    except OSError:
        # Keep the live task running even if session history cannot be persisted.
        pass
    state.upsert_child_session(session_id, id=session_id, title=title, parent_sid=str(parent_sid or ""),
                               task=str(task or ""), status=status, step=int(step or 0),
                               activity=str(activity or ""), report=str(report or ""),
                               cwd=cwd, path=str(path), ts=now, index=index,
                               live_think="", live_text="")   # the saved transcript now holds the reply
    return path

def _update_child_session(session_id, title, parent_sid, task, msgs, head, status, step, activity,
                          report="", index=None):
    if session_id:
        _persist_child_session(session_id, title, parent_sid, task, msgs, head, status, step, activity,
                               report, index=index)

def _set_child_status(session_id, status, activity, report="", step=None):
    """Update status while preserving the persisted transcript, even on setup/API failures."""
    row = dict(state.CHILD_SESSIONS.get(session_id) or {})
    path = row.get("path")
    payload = {}
    if path:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload.update({"id": session_id, "agent_status": status, "agent_activity": activity,
                    "agent_report": report or payload.get("agent_report", ""), "ts": time.time()})
    if step is not None:
        payload["agent_step"] = int(step or 0)
    if path:
        try:
            target = Path(path)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
            if status in {"completed", "failed", "cancelled"}:
                index_session(payload.get("msgs") or [], session_id, cwd=payload.get("cwd") or os.getcwd(),
                              path=target, ts=payload.get("ts") or time.time())
        except OSError:
            pass
    state.upsert_child_session(session_id, status=status, activity=activity, report=report or row.get("report", ""),
                               step=int(step if step is not None else row.get("step", 0)), ts=time.time())

def _new_queued_child(task, parent_sid, index):
    sid = _child_id()
    title = _child_title(index, task)
    _persist_child_session(sid, title, parent_sid, task, status="queued", step=0,
                           activity="waiting for parent", index=index)
    return {"id": sid, "title": title, "task": task, "index": index}

async def run_child(prompt, parent_sid=None, prefix=None, tools=None, report_chars=None, model=None,
                     agent=None, allow_write=False, index=1, max_steps=None, activity=None, ctx=None, browser=None):
    """Run one task in a persistent child-session tab, synchronously."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: a non-empty child task is required."
    if not parent_sid or prefix is None or tools is None:
        return "Error: child sessions require parent_sid, the exact parent prefix, and parent tool schemas."
    job = _new_queued_child(prompt.strip(), parent_sid, index)
    _set_child_status(job["id"], "running", "starting", step=0)
    try:
        result = await run(prompt.strip(), agent=agent, parent_sid=parent_sid, prefix=prefix, tools=tools,
                           report_chars=report_chars, model=model, allow_write=allow_write,
                           force_readonly=not allow_write, session_id=job["id"],
                           session_title=job["title"], session_index=index, max_steps=max_steps,
                           activity=activity, ctx=ctx, browser=browser)
        return _report(result, report_chars)
    except asyncio.CancelledError:
        _set_child_status(job["id"], "cancelled", "cancelled")
        raise
    except Exception as exc:
        result = _report(f"Error: {type(exc).__name__}: {exc}", report_chars)
        _set_child_status(job["id"], "failed", "failed", report=result)
        return result

async def run(prompt=None, agent=None, allow_write=False, force_readonly=False, parent_session=None,
              prefix=None, parent_sid=None, tools=None, report_chars=None, model=None,
              session_id=None, session_title=None, session_index=None, max_steps=None, activity=None,
              ctx=None, browser=None):
    """Run one focused task. `activity` (async, optional) gets a short line per step and tool batch.
    ctx — tokens the child's own history may take before old tool results are trimmed (default SUB_CTX);
    every trim rewrites cached history, so long staged parts get the room the window leaves.

    When `prefix` is supplied by the parent, its message bytes, model, and tool schemas are reused
    unchanged. Delegation instructions live in the first new user message, not in system/schema
    fields, so requests can share the parent's cache prefix.
    """
    if not str(prompt or "").strip():
        return "Error: a non-empty prompt is required for a single subagent task."
    parent_sid = parent_sid or parent_session
    cache_prefix = _prefix_messages(prefix)
    shared_prefix = cache_prefix is not None
    d = skills.agents().get(agent) if agent else None

    if shared_prefix:
        # Never silently claim cache affinity when the caller omitted the parent session/schema set.
        if not parent_sid or tools is None:
            return "Error: shared-prefix subagents require parent_sid and the exact parent tools list."
        # A parent-backed subagent must use exactly the same model and schema catalog as the parent.
        tool_specs = deepcopy(tools)
        active_model = model or MODEL
        head = len(cache_prefix)
        report_limit = _report_limit(report_chars)
        extra_instructions = ("\nAdditional task instructions: " + d["body"].strip()) if d and d.get("body", "").strip() else ""
        if allow_write and not force_readonly:
            role_rules = ("You are a delegated implementation agent. Complete only the self-contained task below. "
                          "You may edit project files using the provided tools, but keep changes scoped to this task, "
                          "preserve unrelated work, never run destructive commands, and run targeted checks when useful. "
                          "Do not start other agents or background work. Finish with a concise report: changed paths, "
                          "what changed, checks run and any unresolved issue.")
        else:
            role_rules = ("You are a read-only subagent. Do not modify files or run destructive commands. "
                          "Return only actionable facts in the form path:line — fact. Do not summarize whole files; "
                          "state uncertainty when a line number is not known. Keep the report concise.")
        if browser:
            from .tools import browser as browser_tool
            role_rules = browser_tool.HELPER_RULES + (" " + browser_tool.READ_ONLY_RULES if browser == "read" else "")
        if _forked(cache_prefix):
            role_rules = ("You are now the subagent, not the parent. The messages above are the parent agent's "
                          "conversation, given to you only as context: use what was already read there instead of "
                          "reading it again. The parent's request is not yours: do only the Task below, not the rest "
                          "of that request, and do not call subagent, ask_user or todo_write. ") + role_rules
        delegation = ("<delegation>\n" + role_rules + "\n"
                      + extra_instructions + "\n</delegation>\n\nTask:\n" + str(prompt or ""))
        msgs = [*cache_prefix, {"role": "user", "content": delegation}]
        prefix_for_budget = cache_prefix
        # Same key as the parent's request. No agent-specific prompt or tool-schema variation is allowed
        # ahead of the delegation message when a shared prefix is active.
        # Effort is rendered into the system turn by the chat template: it must match the parent's.
        eb = {"top_k": top_k_for(active_model), "min_p": min_p_for(active_model),
              **cachekey.subagent_body(os.getcwd(), parent_sid), **config.effort_body(state.S.get("effort"))}
        # Pin the fork point: the child's own checkpoints must not push out the one the parent resumes from.
        cachekey.pin(eb, len(cache_prefix))
        effective_write = bool(allow_write) and not force_readonly
    else:
        # Standalone path is used by /init and legacy callers without a parent prefix. It deliberately
        # does not occupy the parent's cache slot because its system/schema prefix is different.
        tool_specs = [t for t in BUILTIN if t["function"]["name"] in SUBAGENT_READONLY]
        if d and d["tools"] and not force_readonly:
            tool_specs = [t for t in BUILTIN if t["function"]["name"] in d["tools"]]
        effective_write = False if force_readonly else (allow_write or bool(d and set(d["tools"]) & (SUBAGENT_WRITERS | {"py_run", SHELL_TOOL})))
        active_model = model or ((d or {}).get("model") or MODEL)
        base_system = SUB_SYSTEM + (f"\n\n{d['body']}" if d and d.get("body") else "")
        cache_prefix = [{"role": "system", "content": base_system}]
        prefix_for_budget = cache_prefix
        head = len(cache_prefix)
        report_limit = _report_limit(report_chars)
        msgs = [*cache_prefix, {"role": "user", "content": str(prompt or "")}]
        eb = {"top_k": top_k_for(active_model), "min_p": min_p_for(active_model),
              **cachekey.extra_body(os.getcwd(), f"standalone-{parent_sid or id(msgs)}"), **config.effort_body(state.S.get("effort"))}

    title = session_title or _child_title(session_index or 1, prompt)
    if session_id:
        _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "running", 0, "starting", index=session_index)
    client = AsyncOpenAI(base_url=config.BASE_URL, api_key=config.API_KEY, **llmretry.client_options())
    try:
        steps = max_steps or MAX_STEPS
        empty_retries = 0
        broken_retries = 0    # replies whose tool-call markup no parser could read
        unsent = False        # the last tool result has not gone to the server yet (safe to attach to)
        changed = []          # paths this child wrote: the report keeps them even when the child is cut off
        did = ""              # the last tool batch, shown while the model reads its results
        short_think = False   # the last reply's reasoning looped: this step gets a short think
        fixed = _fixed_cost(tool_specs, prefix_for_budget)
        for step in range(steps):
            last = step == steps - 1
            if step and step == steps - 3 and steps >= 6 and unsent:
                # Children ran out of steps before the checks their task asked for (live run: tests written,
                # never run). Into the freshest tool result, which the server has not seen: cache-safe.
                attach_late(msgs, [STEPS_LEFT])
            if last and step:
                # The last step is for the report: a child cut off mid-work used to return only
                # "(subagent stopped)", and the parent could not tell what was already done.
                attach_late(msgs, [WRAP_UP])
            if session_id:
                _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "running", step + 1,
                                      "model inference", index=session_index)
            _trim_history(msgs, head, tool_specs, prefix_for_budget, ctx or SUB_CTX_LIMIT)
            await _tell(activity, f"step {step + 1} · " + (f"{did} → thinking" if did else "thinking"))
            # The same thinking cap as the parent's steps (without one a high-effort step may think for
            # 10k+ tokens). It is a sampling field: the cached prompt stays as it is.
            body = dict(eb)
            budget = LOOP_RETRY_THINK if short_think else think_budget(compact.est_tokens(msgs, head, fixed),
                                                                       state.S.get("effort"))
            if budget:
                body["reasoning_budget_tokens"] = max(MIN_THINK, budget)
            short_think = False
            kw = dict(model=active_model, messages=wire(msgs), tools=tool_specs, extra_body=body,
                      **sampling_for(active_model))
            if session_id:
                LAST_REQUEST.pop(session_id, None)
                LAST_REQUEST[session_id] = {**kw, "messages": [dict(m) for m in kw["messages"]]}
                while len(LAST_REQUEST) > LAST_REQUEST_KEEP:
                    LAST_REQUEST.pop(next(iter(LAST_REQUEST)))
            try:
                async def retrying(exc, attempt, wait, step=step):
                    await _tell(activity, f"step {step + 1} · server failed ({type(exc).__name__}), "
                                          f"retry {attempt}/{llmretry.RETRIES} in {wait:g} s")
                r, looped = await llmretry.call(lambda: _complete(client, kw, _live_view(session_id)), retrying)
                unsent = False
            except llmretry.RETRYABLE as exc:
                # The server stayed down through every retry: report what was done instead of failing the
                # parent's tool call (one APIConnectionError used to lose all steps of the child).
                result = _report(_partial(f"(subagent stopped: the server did not answer: {llmretry.describe(exc)})",
                                          msgs, head, changed), report_limit)
                if session_id:
                    _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "failed", step + 1,
                                          "server error", report=result, index=session_index)
                return result
            except BadRequestError as exc:
                # Context overflow or a rejected field: report what was done instead of failing the parent's tool call.
                result = _report(_partial(f"(subagent stopped: the server rejected the request: {str(exc)[:200]})",
                                          msgs, head, changed), report_limit)
                if session_id:
                    _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "failed", step + 1,
                                          "server error", report=result, index=session_index)
                return result
            note = _cache_note(getattr(r, "usage", None))
            if session_id and note:
                state.upsert_child_session(session_id, cache=note)
            if looped:
                # The reasoning repeated itself and was cut: nothing is kept, the step goes again with a
                # short think (as in the main loop; the effort is not switched, the prompt stays cached).
                short_think = True
                if session_id:
                    state.upsert_child_session(session_id, live_think="", live_text="")
                continue
            m = r.choices[0].message
            content = m.content or ""
            calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                     for c in (m.tool_calls or [])]
            reasoning = getattr(m, "reasoning_content", None) or (getattr(m, "model_extra", None) or {}).get("reasoning_content")
            await _tell_think(activity, step + 1, reasoning or split_think(content)[0])
            if not calls and content:
                # The server left the call in the text (Qwen XML / Hermes JSON): run it, as the main loop does.
                found, content = text_tool_calls(content, f"subcall_{step}", tool_specs)
                calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in found]
            if not calls and "<tool_call>" in (reasoning or "") and not split_think(content)[1].strip():
                found = text_tool_calls(reasoning, f"subthink_{step}")[0]
                calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in found]
            for c in calls:
                c["name"] = canonical_tool_name(c["name"], tool_specs, repair_json(c.get("arguments") or "{}"))
                # A reply cut by the output limit: a half-sent file or command must not run (as in the main loop).
                c["cut"] = cut_off_error(c["name"], c.get("arguments"), getattr(r.choices[0], "finish_reason", None))
            wanted = ""
            if last and calls:
                # The wrap-up step asked for tools again: they do not run; its text (if any) is the report.
                wanted = ", ".join(_call_brief(c["name"], repair_json(c["arguments"]) or {}) for c in calls)
                calls = []
            if not calls:
                text = split_think(content)[1].strip()
                if broken_text_call(text) and broken_retries < 2 and not last:
                    # Tool-call markup no parser could read is a failed call, not the report (live run: task
                    # 3 of 4 came back "completed" with `<tool_call><2>...` as its report, nothing written).
                    # The step goes again unchanged: a feedback message would move the last user message
                    # and the server would re-read the child's whole history.
                    broken_retries += 1
                    continue
                if broken_text_call(text):
                    text = _partial("(subagent stopped: its tool calls could not be read)", msgs, head, changed)
                if not text and wanted:
                    text = _partial(f"(subagent reached its step limit before a report; its last step asked "
                                    f"to run {wanted[:300]}, which did not run: unverified)", msgs, head, changed)
                if not text and empty_retries < 1 and not last:
                    # An empty reply (cut while thinking) is not a finding: one more step, as in the main loop.
                    empty_retries += 1
                    attach_late(msgs, [THINK_CUT_NUDGE])
                    continue
                text = text or _partial("(no findings)", msgs, head, changed)
                if last and changed and not any(str(c) in text for c in changed):
                    text += "\nFiles changed: " + ", ".join(changed)
                result = _report(text, report_limit)
                done = {"role": "assistant", "content": content or text}
                msgs.append({**done, "reasoning_content": reasoning} if reasoning else done)
                if session_id:
                    _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "completed", step + 1,
                                          "completed", report=result, index=session_index)
                return result
            # finish is the parent's fast-final tool. Honor it as the child's terminal response so
            # a child never spends another full model round explaining that it has finished.
            if len(calls) == 1 and calls[0]["name"] == "finish":
                finish_args = repair_json(calls[0]["arguments"]) or {}
                if not validate_tool_arguments("finish", finish_args, tool_specs):
                    finish_text = str(finish_args.get("text") or "(child task completed)").strip()
                    msgs.append({"role": "assistant", "content": content, "tool_calls": [
                        {"id": calls[0]["id"], "type": "function", "function": {"name": "finish",
                         "arguments": calls[0]["arguments"]}}]})
                    msgs.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": "Child task completed."})
                    msgs.append({"role": "assistant", "content": finish_text})
                    result = _report(finish_text, report_limit)
                    if session_id:
                        _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "completed", step + 1,
                                              "completed", report=result, index=session_index)
                    return result
            step_msg = {"role": "assistant", "content": content, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls]}
            msgs.append({**step_msg, "reasoning_content": reasoning} if reasoning else step_msg)
            owner = msgs[-1]
            args_list = [repair_json(c["arguments"]) or {} for c in calls]
            # The file before each edit: the tab draws the real diff (it read the disk after the write: +0 -0).
            before = {c["id"]: disk_text(a["path"]) for c, a in zip(calls, args_list)
                      if c["name"] in SUBAGENT_WRITERS and isinstance(a.get("path"), str) and a["path"].strip()}
            did = ", ".join(_call_brief(c["name"], a) for c, a in zip(calls, args_list))
            await _tell(activity, f"step {step + 1} · {did}")
            if session_id:   # the tab shows the reply and its tool cards while the tools run
                _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "running", step + 1,
                                      "running: " + ", ".join(c["name"] for c in calls), index=session_index)
            # Use the exact schemas that were sent to the model for validation, but refuse unsupported
            # actions and destructive operations even when file writes are enabled for this task.
            if len(calls) > 1 and all(parallel_safe(c["name"], a) for c, a in zip(calls, args_list)):
                outs = await asyncio.gather(*(_exec_call(c, a, effective_write, tool_specs, browser)
                                              for c, a in zip(calls, args_list)))
            else:
                outs = []
                for c, a in zip(calls, args_list):
                    outs.append(await _exec_call(c, a, effective_write, tool_specs, browser))
            for c, a, out in zip(calls, args_list, outs):
                if c["name"] in SUBAGENT_WRITERS and isinstance(a, dict) and a.get("path") \
                        and not str(out).startswith(("Error", "subagent:")) and a["path"] not in changed:
                    changed.append(a["path"])
                if before.get(c["id"]) is not None:
                    _note_edit(owner, c["id"], before[c["id"]], disk_text(a["path"]))
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": tool_content(c["name"], a, out)})
                unsent = True
            if session_id:
                last_tool = ", ".join(c["name"] for c in calls)
                _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "running", step + 1,
                                      "tools: " + last_tool, index=session_index)
        result = _report(_partial(f"(subagent stopped: exceeded {steps} steps)", msgs, head, changed), report_limit)
        if session_id:
            _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "failed", steps,
                                  "step limit exceeded", report=result, index=session_index)
        return result
    except asyncio.CancelledError:
        if session_id:
            _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "cancelled", 0,
                                  "cancelled", index=session_index)
        raise
    except Exception as exc:
        if session_id:
            result = _report(f"Error: {type(exc).__name__}: {exc}", report_limit)
            _update_child_session(session_id, title, parent_sid, prompt, msgs, head, "failed", 0,
                                  "failed", report=result, index=session_index)
        raise


async def fan_out(tasks, parent_sid=None, prefix=None, tools=None, report_chars=None, model=None, agent=None,
                  allow_write=True):
    """Create a parent-controlled sequential batch and run ONLY its first child.

    Each child report returns to the parent model before the next task starts. The parent
    continues the queue with `continue_batch(batch_id, ...)`, so it can adapt or stop after
    seeing the preceding report. There is never a batch of concurrent local inference requests.
    """
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= 6:
        return "Error: tasks must be an array of 2–6 short, independent, self-contained task strings."
    normalized = []
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, str) or not task.strip():
            return f"Error: task {i} must be a non-empty string. No subagents were started."
        normalized.append(task.strip())
    if not parent_sid or prefix is None or tools is None:
        return "Error: sequential child sessions require parent_sid, the exact parent prefix, and parent tool schemas."

    batch_id = "batch-" + uuid.uuid4().hex[:10]
    batch = {
        "id": batch_id, "parent_sid": str(parent_sid), "tasks": normalized,
        "next_index": 0, "status": "pending", "report_chars": _report_limit(report_chars),
        "model": model, "agent": agent, "allow_write": bool(allow_write), "created_at": time.time(),
    }
    state.SUBAGENT_BATCHES[batch_id] = batch
    # Bound abandoned queue metadata in long-lived terminal sessions.
    if len(state.SUBAGENT_BATCHES) > 32:
        for old_id, old in list(state.SUBAGENT_BATCHES.items()):
            if old_id != batch_id and old.get("status") != "running":
                state.SUBAGENT_BATCHES.pop(old_id, None)
                if len(state.SUBAGENT_BATCHES) <= 24:
                    break
    return await continue_batch(batch_id, parent_sid=parent_sid, prefix=prefix, tools=tools,
                                report_chars=report_chars, model=model)


async def continue_batch(batch_id, parent_sid=None, prefix=None, tools=None, report_chars=None, model=None):
    """Run exactly the next queued child, then return its report to the parent model."""
    batch_id = str(batch_id or "").strip()
    batch = state.SUBAGENT_BATCHES.get(batch_id)
    if not batch:
        return f"Error: delegated batch {batch_id or '(missing id)'} was not found in this process. Start a new subagent task."
    if str(parent_sid or "") != str(batch.get("parent_sid") or ""):
        return "Error: this delegated batch belongs to a different parent session."
    if prefix is None or tools is None:
        return "Error: continuing a batch requires the exact parent prefix and tool schemas."
    if batch.get("status") == "running":
        return "Error: this delegated batch already has a running child. Wait for its report."
    tasks = batch.get("tasks") or []
    index = int(batch.get("next_index") or 0)
    if index >= len(tasks):
        batch["status"] = "completed"
        return f"<subagent_batch_complete id=\"{batch_id}\">All {len(tasks)} planned child tasks have already been attempted. Review the previous reports and verify only the reported paths/checks.</subagent_batch_complete>"

    task = tasks[index]
    limit = _report_limit(report_chars if report_chars is not None else batch.get("report_chars"))
    job = _new_queued_child(task, parent_sid, index + 1)
    batch["status"] = "running"
    batch["active_child_id"] = job["id"]
    batch["active_index"] = index
    try:
        finding = await run(task, agent=batch.get("agent"), parent_sid=parent_sid, prefix=prefix, tools=tools,
                            report_chars=limit, model=model or batch.get("model"),
                            force_readonly=not bool(batch.get("allow_write", True)),
                            allow_write=bool(batch.get("allow_write", True)), session_id=job["id"],
                            session_title=job["title"], session_index=index + 1)
        finding = _report(finding, limit)
        child_status = str((state.CHILD_SESSIONS.get(job["id"]) or {}).get("status") or "completed")
        if child_status not in {"completed", "failed", "cancelled"}:
            _set_child_status(job["id"], "completed", "completed", report=finding)
            child_status = "completed"
    except asyncio.CancelledError:
        _set_child_status(job["id"], "cancelled", "cancelled")
        batch["status"] = "cancelled"
        raise
    except Exception as exc:
        finding = _report(f"Error: {type(exc).__name__}: {exc}", limit)
        child_status = "failed"
        _set_child_status(job["id"], "failed", "failed", report=finding)

    batch["next_index"] = index + 1
    batch["last_child_id"] = job["id"]
    batch["last_report"] = finding
    batch["last_status"] = child_status
    remaining = len(tasks) - batch["next_index"]
    batch["status"] = "awaiting_parent" if remaining else "completed"
    step = (f'<subagent_step batch_id="{batch_id}" task="{index + 1}/{len(tasks)}" '
            f'child_session="{job["id"]}" status="{child_status}" remaining="{remaining}">\n'
            f'Task: {str(task)[:180]}\nReport: {finding}\n</subagent_step>')
    if remaining:
        step += (f'\n<subagent_next batch_id="{batch_id}" remaining="{remaining}">Review this report first. '
                 f'If the remaining plan is still relevant, call subagent(batch_id="{batch_id}") to run exactly the next task. '
                 f'Do not launch other model work in the background.</subagent_next>')
    else:
        step += (f'\n<subagent_batch_complete id="{batch_id}">All {len(tasks)} planned tasks have been attempted. '
                 f'Review the sequence of short reports and verify only the reported changed paths and targeted checks.</subagent_batch_complete>')
    return step


_BACKGROUND = {}
_BACKGROUND_SEQ = 0

def background_pending():
    """Return true while any background subagent is still running."""
    return any(not task.done() for task in _BACKGROUND.values())

def background_count():
    return sum(not task.done() for task in _BACKGROUND.values())

def take_background_results():
    """Drain completed background results in completion order."""
    results = []
    while state.SUBAGENT_RESULTS:
        results.append(state.SUBAGENT_RESULTS.popleft())
    return results

async def _background_worker(task_id, prompt, agent=None, parent_session=None, prefix=None, tools=None, tasks=None, report_chars=None, model=None):
    result = ""
    try:
        if tasks is not None:
            result = await fan_out(tasks, parent_sid=parent_session, prefix=prefix, tools=tools, report_chars=report_chars, model=model, agent=agent)
        else:
            result = await run(prompt, agent=agent, allow_write=False, force_readonly=True, parent_sid=parent_session, prefix=prefix, tools=tools, report_chars=report_chars, model=model)
    except asyncio.CancelledError:
        result = "(background subagent cancelled)"
        raise
    except Exception as exc:
        result = f"Error: background subagent {type(exc).__name__}: {exc}"
    finally:
        h = await hooks.run("SubagentStop", {"cwd": os.getcwd(), "event": "SubagentStop",
                                             "agent": agent or "general", "task_id": task_id,
                                             "tool_response": result})
        if h.get("context"):
            result += "\n[hook]\n" + h["context"].strip()
        result = limit_report(result, report_chars)
        state.SUBAGENT_RESULTS.append(
            f"<subagent_result id=\"{task_id}\" agent=\"{agent or 'general'}\">\n{result}\n</subagent_result>")
        _BACKGROUND.pop(task_id, None)

def start_background(prompt, agent=None, parent_session=None, prefix=None, tools=None, tasks=None, report_chars=None, model=None):
    """Background model inference is intentionally disabled for local single-GPU use."""
    return ("Error: background subagent inference is disabled to protect local GPU/RAM. "
            "Use subagent(tasks=[...]) and continue each task with the returned batch_id; "
            "the parent will receive a report between child runs.")


async def wait_for_background(timeout=60):
    """Wait briefly for currently active subagents; return false if any remain pending."""
    tasks = [t for t in _BACKGROUND.values() if not t.done()]
    if not tasks:
        return True
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return not background_pending()
