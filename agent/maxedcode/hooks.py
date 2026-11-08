"""Hooks. Events from settings.json: PreToolUse, PostToolUse, UserPromptSubmit, SessionStart,
Stop, SubagentStop, PreCompact.

Hook = a command. It gets JSON on stdin (session_id, cwd, event, tool_name, tool_input; for
PostToolUse also tool_response), answers with JSON (permissionDecision allow/deny/ask, updatedInput,
systemMessage, additionalContext) or exit code 2 = block (stderr is the reason).
Format: {"hooks": {"PreToolUse": [{"matcher": "powershell", "hooks": [{"type": "command", "command": "..."}]}]}}
"""
import asyncio
import json
import os
import re
import subprocess
from contextlib import suppress
from .config import SETTINGS

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop", "SubagentStop", "PreCompact")
TIMEOUT = int(os.environ.get("AGENT_HOOK_TIMEOUT", "30"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # a hook must not touch the TUI console


TOOL_EVENTS = ("PreToolUse", "PostToolUse")   # only these events have a tool name to match


def _match(m, tool):
    if not m or m == "*":
        return True
    try:
        return bool(re.search(m, tool or "", re.I))
    except re.error:
        return m.lower() == (tool or "").lower()


def hooks_for(event, tool=""):
    out = []
    for h in (SETTINGS.get("hooks") or {}).get(event) or []:
        if event not in TOOL_EVENTS or _match(h.get("matcher"), tool):
            out.append(h)
    return out


def _test_command():
    command = SETTINGS.get("test_command")
    if isinstance(command, dict):
        command = command.get("command")
    return command.strip() if isinstance(command, str) and command.strip() else ""


# Full test suites are queued after successful mutations and executed only when the
# model attempts to finish the current task. This avoids running pytest after every edit.
_PENDING_AUTO_TESTS = set()
_AUTO_TEST_TOOLS = {"edit_file", "write_file", "patch", "apply_diff"}


def _test_key(session_id=None, cwd=None):
    return (str(session_id or "default"), os.path.abspath(cwd or os.getcwd()))


def _should_queue_auto_test(event, payload):
    return (event == "PostToolUse"
            and payload.get("tool_name") in _AUTO_TEST_TOOLS
            and not str(payload.get("tool_response") or "").startswith(("Error", "Tool error"))
            and bool(_test_command()))


def _test_timeout():
    try:
        return max(1, min(600, int(SETTINGS.get("test_timeout", 120))))
    except (TypeError, ValueError):
        return 120


def clear_pending_auto_tests():
    """Clear queued test runs; exposed for isolated tests and session cleanup."""
    _PENDING_AUTO_TESTS.clear()


def _queue_auto_test(payload):
    _PENDING_AUTO_TESTS.add(_test_key(payload.get("session_id"), payload.get("cwd")))


def automatic_tests_passed(result):
    return str(result or "").startswith("[automatic tests PASS]")


async def run_pending_auto_tests(session_id=None, cwd=None):
    """Run one configured suite for a session after its edits, not after each tool call."""
    key = _test_key(session_id, cwd)
    if key not in _PENDING_AUTO_TESTS:
        return ""
    _PENDING_AUTO_TESTS.discard(key)
    command = _test_command()
    if not command:
        return ""
    target_cwd = key[1]
    p = None
    try:
        p = await asyncio.create_subprocess_shell(command, cwd=target_cwd, stdin=asyncio.subprocess.DEVNULL,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                  creationflags=NO_WINDOW)
        output, _ = await asyncio.wait_for(p.communicate(), _test_timeout())
        text = output.decode("utf-8", "replace").strip()
        status = "PASS" if p.returncode == 0 else f"FAIL (exit {p.returncode})"
        return f"[automatic tests {status}] command: {command}\n{text[:5000] or '(no output)'}"
    except asyncio.TimeoutError:
        if p is not None and p.returncode is None:
            p.kill()
            with suppress(Exception):
                await p.wait()
        return f"[automatic tests TIMEOUT] command exceeded {_test_timeout()} s: {command}"
    except Exception as e:
        return f"[automatic tests ERROR] {type(e).__name__}: {e}"


async def run(event, payload):
    """Run all hooks of the event. Returns {decision, input, context, message, stop}."""
    r = {"event": event, "decision": None, "input": None, "context": "", "message": "", "stop": False}
    for h in hooks_for(event, payload.get("tool_name") or ""):
        for hk in h.get("hooks") or []:
            if hk.get("type") != "command" or not hk.get("command"):
                continue
            try:
                p = await asyncio.create_subprocess_shell(hk["command"], stdin=subprocess.PIPE,
                                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                          env={**os.environ, "MAXEDCODE_EVENT": event},
                                                          creationflags=NO_WINDOW)
                raw, err = await asyncio.wait_for(p.communicate(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")), TIMEOUT)
            except Exception as e:
                r["message"] += f"hook failed: {type(e).__name__}: {e}\n"
                continue
            txt = raw.decode("utf-8", "replace").strip()
            if p.returncode == 2:
                r["decision"] = "deny"
                r["message"] += (err.decode("utf-8", "replace").strip() or txt or "blocked by hook")
                return r
            d = None
            with suppress(Exception):
                d = json.loads(txt)
            if isinstance(d, dict):
                if d.get("permissionDecision") in ("allow", "deny", "ask"):
                    r["decision"] = d["permissionDecision"]
                if isinstance(d.get("updatedInput"), dict):
                    r["input"] = d["updatedInput"]
                for k in ("systemMessage", "additionalContext"):
                    if d.get(k):
                        r["message"] += str(d[k]) + "\n"
                if d.get("continue") is False:
                    r["stop"] = True
            elif txt:
                r["context"] += txt + "\n"
            if r["decision"] == "deny":
                return r
    if _should_queue_auto_test(event, payload):
        _queue_auto_test(payload)
    return r


def sync(event, payload):
    """Same for code paths without an event loop (headless uses run())."""
    import subprocess
    r = {"event": event, "decision": None, "input": None, "context": "", "message": "", "stop": False}
    for h in hooks_for(event, payload.get("tool_name") or ""):
        for hk in h.get("hooks") or []:
            if hk.get("type") != "command" or not hk.get("command"):
                continue
            try:
                p = subprocess.run(hk["command"], input=json.dumps(payload, ensure_ascii=False),
                                   capture_output=True, text=True, timeout=TIMEOUT,
                                   env={**os.environ, "MAXEDCODE_EVENT": event}, creationflags=NO_WINDOW)
            except Exception as e:
                r["message"] += f"hook failed: {type(e).__name__}: {e}\n"
                continue
            if p.returncode == 2:
                r["decision"] = "deny"
                r["message"] += (p.stderr.strip() or p.stdout.strip() or "blocked by hook")
                return r
            d = None
            with suppress(Exception):
                d = json.loads(p.stdout)
            if isinstance(d, dict):
                if d.get("permissionDecision") in ("allow", "deny", "ask"):
                    r["decision"] = d["permissionDecision"]
                if isinstance(d.get("updatedInput"), dict):
                    r["input"] = d["updatedInput"]
                for k in ("systemMessage", "additionalContext"):
                    if d.get(k):
                        r["message"] += str(d[k]) + "\n"
                if d.get("continue") is False:
                    r["stop"] = True
            elif p.stdout.strip():
                r["context"] += p.stdout.strip() + "\n"
            if r["decision"] == "deny":
                return r
    if _should_queue_auto_test(event, payload):
        _queue_auto_test(payload)
    return r
