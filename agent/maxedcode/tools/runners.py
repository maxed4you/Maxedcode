"""Running PowerShell and Python with timeouts and killing the process tree.

Three things that weren't there before:
  * a live shell (tools/shell.py) instead of spawning PowerShell on every call;
  * telling "process failed to start" (OSError) from "command returned a non-zero code" — the former
    used to be an unhandled exception;
  * cleanup: background tasks and the live shell are killed when the app exits, and a removed
    running task gets the `killed` status, not "still running" (like the exit event in opencode
    shell.ts) — otherwise the reader waits forever and reads a partial file.
"""
import asyncio
import atexit
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from ..config import HOME
from ..shelltool import NAME as SHELL_TOOL, POSIX, posix_env
from ..util import clip, strip_ansi, tool_output_dir
from . import psprep, shell

# Linux/macOS: every child leads its own process group, so a timeout or bg_kill stops its children too.
_SESSION = {"start_new_session": True} if POSIX else {}

# Output is limited per tool, not globally: for commands the head and tail matter, the middle
# is almost never needed (head+tail buffer). That's wrong for read_file, so it has its own limit.
PS_OUT_MAX = int(os.environ.get("AGENT_PS_OUT_MAX", "4000"))
PY_OUT_MAX = int(os.environ.get("AGENT_PY_OUT_MAX", "4000"))
BG_OUT_MAX = int(os.environ.get("AGENT_BG_OUT_MAX", "6000"))


def _kill(p):
    """Kill the process tree; if the tree kill is unavailable, fall back to killing the process itself.

    Earlier only taskkill was tried on Windows: when it failed (pid not ours, already gone, or the
    binary unavailable) the process kept running, so a task reported as "killed" was still alive and
    poll() never returned a code.
    """
    if sys.platform == "win32":
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            if r.returncode == 0:
                return
        except Exception:
            pass
    elif shell.killpg(p.pid):
        return
    with suppress(Exception):
        p.kill()


def _desktops():
    home = Path.home()
    return [d for d in (home / "Desktop", Path(os.environ.get("OneDrive") or home) / "Desktop") if d.is_dir()]


def system_state():
    """User PATH/variables (HKCU\\Environment, raw values) and desktop file names. A test or a check that
    changes them by accident changes the user's real system; a snapshot costs well under a millisecond."""
    if sys.platform != "win32":
        return None
    import winreg
    env = {}
    with suppress(OSError), winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        for i in range(winreg.QueryInfoKey(k)[1]):
            name, value, kind = winreg.EnumValue(k, i)
            env[name.lower()] = (value, kind)
    desk = set()
    for d in _desktops():
        with suppress(OSError):
            desk.update(str(d / n) for n in os.listdir(d))
    return env, desk


def system_note(before):
    """One line for the tool result when the command changed the user environment or the desktop."""
    after = system_state() if before is not None else None
    if after is None or after == before:
        return ""
    parts = []
    (env0, desk0), (env1, desk1) = before, after
    for name in sorted(env0.keys() | env1.keys()):
        old, new = env0.get(name, ("", 0)), env1.get(name, ("", 0))
        if old == new:
            continue
        if name == "path":
            a, b = [x for x in str(old[0]).split(";") if x], [x for x in str(new[0]).split(";") if x]
            diff = [f"+{x}" for x in b if x not in a] + [f"-{x}" for x in a if x not in b]
            parts.append("user PATH " + (" ".join(diff)[:300] if diff else
                                         "rewritten (same entries, other order or value type: %VARS% may be expanded)"))
        else:
            parts.append(f"user variable {name}")
    parts += [f"desktop +{Path(x).name}" for x in sorted(desk1 - desk0)][:5]
    parts += [f"desktop -{Path(x).name}" for x in sorted(desk0 - desk1)][:5]
    return ("\n[system change] this command changed the real system: " + "; ".join(parts) +
            ". If that was not its goal (a test, a check), restore it and keep the code under test off the real "
            "system (mock it or use a temp dir).")


def _head_tail(text, limit):
    """Head+tail: for command output the beginning and the end matter, the middle almost never does.

    The full text is saved where read_file may open it. The marker used to say "full text saved by clip()",
    but clip() then got the already cut text and saved nothing: a failed pytest with its traceback in the
    middle was run again with a filter, one more step plus the test time."""
    if len(text) <= limit:
        return text
    saved = _save_output(text)
    where = (f"full output saved to {saved}; read_file it with start_line/end_line or grep it" if saved
             else "the full output could not be saved")
    marker = f"\n…[middle cut: {len(text)} chars total; {where}]\n"
    head = min(1500, limit // 3)
    tail = max(0, limit - head - len(marker))
    return text[:head] + marker + (text[-tail:] if tail else "")


def _save_output(text):
    """Write text to the tool-output folder under the name read_file trusts; the path, or None."""
    with suppress(OSError):
        d = tool_output_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp, n = int(time.time() * 1000), 0
        path = d / f"tool-output-{stamp}-{os.getpid()}.txt"
        while path.exists():
            n += 1
            path = d / f"tool-output-{stamp}-{os.getpid()}-{n}.txt"
        path.write_text(text, encoding="utf-8", errors="replace")
        return path
    return None



async def run_py(code, promote=False):
    """promote: on timeout the snippet keeps running as a background task (the main loop has bg_output and
    bg_kill); without it (subagents) it is killed as before."""
    before = system_state()
    return await _run_py(code, promote) + system_note(before)


PY_TIMEOUT = 60
# A snippet moved to the background is stopped after this many more seconds: a forgotten brute-force check
# must not keep a core busy next to the local model.
PY_BG_MAX = int(os.environ.get("AGENT_PY_BG_MAX", "300"))


async def _drain(stream, sink):
    """Collect output as it arrives (sink["buf"]), so a stopped snippet still returns what it printed; after
    the snippet moved to the background (sink["file"] set) the rest goes to its task log."""
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            if sink.get("file") is not None:
                sink["file"].write(chunk)
                sink["file"].flush()
            else:
                sink["buf"] += chunk
    finally:
        if sink.get("file") is not None:
            with suppress(OSError):
                sink["file"].close()


class _AsyncProc:
    """An asyncio child seen through the Popen surface the background-task table uses (poll, kill, pid)."""

    def __init__(self, proc):
        self.proc, self.pid = proc, proc.pid

    def poll(self):
        return self.proc.returncode

    def kill(self):
        with suppress(ProcessLookupError, OSError):
            self.proc.kill()


def _promote_py(p, reader, sink):
    """Keep a timed-out snippet running as a background task: its output so far starts the task log, the
    reader goes on appending to it, completion is reported like any background task."""
    d = HOME / "bg"
    d.mkdir(parents=True, exist_ok=True)
    hid = str(int(time.time() * 1000) % 10000000)
    log = d / f"{hid}.log"
    handle = open(log, "wb")
    handle.write(bytes(sink["buf"]))
    handle.flush()
    sink["file"] = handle
    BG[hid] = (_AsyncProc(p), log)
    BG_STARTED[hid] = time.monotonic()
    BG_LIMIT[hid] = PY_BG_MAX
    BG_READERS[hid] = reader
    asyncio.get_running_loop().create_task(watch_background(hid))
    body = strip_ansi(bytes(sink["buf"]).decode("utf-8", "replace"))
    return (f"[py_run still running after {PY_TIMEOUT} s; NOT stopped, it continues as background id {hid}]\n"
            f"{_head_tail(body, PY_OUT_MAX) if body.strip() else '(no output yet)'}\n"
            f"If the check is only too big, stop it with bg_kill(\"{hid}\") and run a smaller one; otherwise wait "
            f"with bg_output(\"{hid}\", wait=N) (returns the moment it ends). It is stopped after {PY_BG_MAX} s more.")


def _py_timeout(body):
    """Timeout result with the partial output and what to do next. A bare "timeout" made the model rerun the
    same brute-force check on the same large input (LCB 3748, abc393_e: 60-120 s lost per task)."""
    head = f"Error: timeout {PY_TIMEOUT} s, code killed."
    if body.strip():
        return (f"{head}\n{_head_tail(body, PY_OUT_MAX)}\n[hint] It was still running (partial output above). "
                "Do not rerun it as is: shrink the input or the number of cases, or start a long check with "
                f"{SHELL_TOOL} run_in_background and wait with bg_output(id, wait=N).")
    return (f"{head}\n[hint] Nothing was printed in {PY_TIMEOUT} s: the code is stuck (waiting for input, an endless "
            "loop) or the work is too big (brute force on a large input). Do not rerun it as is: shrink the input, "
            f"print progress, or start a long check with {SHELL_TOOL} run_in_background and wait with bg_output(id, wait=N).")


async def _run_py(code, promote=False):
    """Run each snippet in a unique file; parallel calls must not overwrite one scratch.py."""
    HOME.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="scratch_", suffix=".py", dir=str(HOME))
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(code)
        try:
            p = await asyncio.create_subprocess_exec(
                sys.executable, str(path), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=shell.NO_WINDOW, **_SESSION,
            )
        except OSError as e:
            # A process that never started is not a command that returned a nonzero code.
            return f"Error: could not start Python: {type(e).__name__}: {e}"
        sink = {"buf": bytearray(), "file": None}
        buf = sink["buf"]
        reader = asyncio.ensure_future(_drain(p.stdout, sink))

        async def finished():
            # Both under the one deadline: a grandchild that keeps stdout open must not make the read wait forever.
            await p.wait()
            await asyncio.shield(reader)

        try:
            await asyncio.wait_for(finished(), PY_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            exited = p.returncode is not None
            if promote and not exited and isinstance(e, asyncio.TimeoutError) and bytes(buf).strip():
                # Like a long powershell command (_run_long): a rerun from scratch cost another full minute.
                # Only a snippet that printed something: a silent one is stuck or a brute force that never ends
                # (LCB abc395_e: three silent snippets moved here, ~480 s of bg_output waits, all killed after).
                with suppress(OSError):          # no task log (disk): stop it as before
                    return _promote_py(p, reader, sink)
                sink["file"] = None
            _kill(p)
            if isinstance(e, asyncio.CancelledError):
                reader.cancel()
                raise
            if not exited:
                with suppress(Exception):
                    await asyncio.wait_for(reader, 2)
            reader.cancel()
            body = strip_ansi(bytes(buf).decode("utf-8", "replace"))
            if exited:
                # The snippet itself ended; a process it started still holds the output pipe open.
                return (f"{_head_tail(body, PY_OUT_MAX) or '(no output)'}\n[note] The code finished (exit code "
                        f"{p.returncode}), but a process it started is still running and holding its output.")
            return _py_timeout(body)
        body = strip_ansi(bytes(buf).decode("utf-8", "replace"))
        if p.returncode != 0:
            return f"Error: Python process exited with code {p.returncode}.\n{_head_tail(body, PY_OUT_MAX)}" if body else f"Error: Python process exited with code {p.returncode} (no output)."
        return body or f"(no output, code {p.returncode})"
    finally:
        with suppress(OSError):
            path.unlink()


# Stdout is a pipe, so PowerShell formats tables for 120 columns and cuts long paths to "...\san...":
# the model cannot use them and lists the folder again. Lines are padded to that width, so trailing
# spaces are stripped from the result.
PS_WIDE = "try { $__s = $Host.UI.RawUI.BufferSize; $__s.Width = 1024; $Host.UI.RawUI.BufferSize = $__s } catch {}; "


# A script error prints "At line:1 char:254" — the caller's position, not where the script failed
# (a model then bisects the script blindly). After the command, every new error raised inside a
# script file is reported as "file:line: source line" from its ScriptStackTrace.
PS_ERR_MARK = "if ($global:Error.Count -gt 200) { $global:Error.Clear() }; $__maxedcode_err0 = $global:Error.Count; "
# PowerShell 5.1: `native 2>&1` turns every stderr line of a native program (unittest, git, pip print
# there) into a NativeCommandError and sets $? to false, so a passing `python -m unittest 2>&1` came back
# as "exited with code 1". With exit code 0 and only such records, the command succeeded.
PS_NATIVE_STDERR = (
    r'''if (-not $__maxedcode_ok -and ($null -eq $__maxedcode_native_exit -or $__maxedcode_native_exit -eq 0)) { '''
    r'''$__maxedcode_new = @($global:Error | Select-Object -First ([Math]::Max(0, $global:Error.Count - $__maxedcode_err0))); '''
    r'''if ($__maxedcode_new.Count -and -not @($__maxedcode_new | Where-Object { "$($_.FullyQualifiedErrorId)" -notlike 'NativeCommandError*' }).Count) '''
    r'''{ $__maxedcode_ok = $true } }; '''
)
# One line on purpose: the persistent shell reads commands line by line from stdin. Only for a command
# that failed: -ErrorAction SilentlyContinue still records its error in $Error, and reporting a
# suppressed error sends the model after a problem that does not exist.
PS_ORIGIN = (
    r'''if (-not $__maxedcode_ok -or ($null -ne $__maxedcode_native_exit -and $__maxedcode_native_exit -ne 0)) { '''
    r'''try { $__maxedcode_seen = @{}; '''
    r'''foreach ($__maxedcode_e in @($global:Error | Select-Object -First ([Math]::Max(0, $global:Error.Count - $__maxedcode_err0)))) { '''
    r'''if ("$($__maxedcode_e.ScriptStackTrace)" -match 'at .*?, (.+?): line (\d+)') { '''
    r'''$__maxedcode_f = $Matches[1]; $__maxedcode_n = [int]$Matches[2]; $__maxedcode_k = "$__maxedcode_f|$__maxedcode_n"; '''
    r'''if ($__maxedcode_f -ne '<No file>' -and -not $__maxedcode_seen[$__maxedcode_k] -and (Test-Path -LiteralPath $__maxedcode_f)) { '''
    r'''$__maxedcode_seen[$__maxedcode_k] = 1; '''
    r'''$__maxedcode_src = "$(@(Get-Content -LiteralPath $__maxedcode_f -TotalCount $__maxedcode_n)[-1])".Trim(); '''
    r'''[Console]::Out.WriteLine("[error origin] ${__maxedcode_f}:${__maxedcode_n}: $__maxedcode_src") } } } } catch {} }; '''
)


def _rstrip_lines(text):
    return "\n".join(line.rstrip() for line in strip_ansi(text or "").split("\n"))


# Downloads, clones and installs routinely need more than two minutes. With the 120 s default the
# shell was killed mid-clone, the model retried into a half-written folder and lost several steps.
LONG_CMD = re.compile(
    r"(?i)(?<![\w-])(?:git\s+(?:clone|fetch|pull|submodule|lfs)|(?:pip3?|uv\s+pip|python\s+-m\s+pip)\s+(?:install|download)"
    r"|uv\s+(?:sync|add)|(?:npm|pnpm|yarn|bun)\s+(?:install|i|ci|add)\b|(?:winget|choco|scoop)\s+install"
    r"|Invoke-WebRequest|\biwr\b|Start-BitsTransfer|curl(?:\.exe)?\s|wget\s|Expand-Archive|docker\s+(?:pull|build)"
    r"|cargo\s+(?:build|install)|dotnet\s+(?:build|restore|publish)|ollama\s+pull|(?:huggingface-cli|hf)\s+download"
    r"|conda\s+(?:install|create))")
if POSIX:   # the package managers of Linux/macOS
    LONG_CMD = re.compile(LONG_CMD.pattern[:-1] + r"|(?:apt(?:-get)?|dnf|yum|zypper|apk|brew|port|snap)\s+(?:install|upgrade|add)"
                          r"|pacman\s+-S)")
PS_TIMEOUT = 120
PS_TIMEOUT_LONG = 600
PS_TIMEOUT_MAX = 600


def ps_timeout(cmd, timeout=None):
    """Seconds for one command: the model's value (capped), else 600 for downloads/installs, else 120."""
    with suppress(TypeError, ValueError):
        if timeout is not None and float(timeout) > 0:
            return int(min(float(timeout), PS_TIMEOUT_MAX))
    return PS_TIMEOUT_LONG if LONG_CMD.search(cmd or "") else PS_TIMEOUT


# "Start-Sleep 100" to wait for a background job wasted the whole interval even when the job ended a
# second later. A leading sleep returns as soon as a running background task finishes.
SLEEP = re.compile(r"(?i)^\s*(?:Start-Sleep|sleep)\s+(?:-s(?:econds)?\s+)?(\d+(?:\.\d+)?)\s*(?:;|\n|$)")


def running_bg():
    return [hid for hid, (p, _) in BG.items() if p.poll() is None and hid not in KILLED]


async def _smart_sleep(secs):
    """Sleep up to secs; stop early when a background task that was running finishes. Returns its id."""
    watch = running_bg()
    end = time.monotonic() + secs
    while time.monotonic() < end:
        done = [hid for hid in watch if BG[hid][0].poll() is not None or hid in KILLED]
        if done:
            return done[0]
        await asyncio.sleep(min(0.25, max(0.01, end - time.monotonic())))
    return None


async def run_ps(cmd, timeout=None, promote=False):
    """promote: a command that outlives its timeout keeps running as a background task (main loop only: it has
    bg_output and bg_kill); otherwise it is stopped as before."""
    before = system_state()
    m = SLEEP.match(cmd or "")
    if m and running_bg():
        hid = await _smart_sleep(min(float(m.group(1)), PS_TIMEOUT_MAX))
        rest = cmd[m.end():].strip()
        head = (f"[sleep cut short: background id {hid} finished]\n{bg_output(hid)}" if hid else
                f"[slept {m.group(1)} s; background tasks still running: {', '.join(running_bg()) or 'none'}. "
                "Next time use bg_output(id, wait=N): it returns the moment the task ends]")
        if not rest:
            return head
        return head + "\n" + await _run_ps(rest, timeout, promote) + system_note(before)
    return await _run_ps(cmd, timeout, promote) + system_note(before)


def _shell_location():
    """Current folder of the live shell (a `cd` in an earlier call), or None."""
    if not (shell.ENABLED and shell.SHELL.alive):
        return None
    with suppress(Exception):
        r = shell.SHELL.run("printf '__loc=%s\\n' \"$PWD\"" if POSIX else
                            "Write-Output ('__loc=' + (Get-Location).ProviderPath)", 5)
        if r and r[0] == 0:
            return next((x[6:] for x in r[1].splitlines() if x.startswith("__loc=")), None)
    return None


async def _run_long(cmd):
    """A clone/install/download runs as a background task that the call waits on (codex-style yield).

    It ends like a normal command when it finishes within PS_TIMEOUT_LONG. When it does not, it is NOT
    killed: the result hands over the task id. Killing it left a half-written folder, and the retry
    collided with it (the openclaw clone took 4 steps and still failed)."""
    loc = await asyncio.to_thread(_shell_location)
    if loc and POSIX:
        cmd = "cd -- '" + loc.replace("'", "'\\''") + "' || exit 1\n" + cmd
    elif loc:
        cmd = "Set-Location -LiteralPath '" + loc.replace("'", "''") + "'\n" + cmd
    started = bg_start(cmd)
    m = re.search(r"Background id (\d+): started", started)
    if not m:
        return started          # the pre-check refused it: nothing ran
    hid = m.group(1)
    try:
        await bg_wait(hid, PS_TIMEOUT_LONG)
    except asyncio.CancelledError:
        bg_kill(hid)
        raise
    p, log = BG[hid]
    body = _rstrip_lines(log.read_text(encoding="utf-8", errors="replace"))
    if p.poll() is None:
        asyncio.get_running_loop().create_task(watch_background(hid))
        REPORTED.discard(hid)
        return (f"[still running after {PS_TIMEOUT_LONG} s; NOT stopped, it continues as background id {hid}]\n"
                f"{_head_tail(body, PS_OUT_MAX)}\n"
                f'Wait for it with bg_output("{hid}", wait=600) (returns the moment it ends) or stop it with bg_kill("{hid}").')
    return _ps_result(p.returncode, body)


async def _run_ps(cmd, timeout=None, promote=False):
    if timeout is None and LONG_CMD.search(cmd or "") and not EXIT_STMT.search(cmd or ""):
        return await _run_long(cmd)
    if POSIX:
        return await _run_sh(cmd, timeout, promote)
    limit = ps_timeout(cmd, timeout)
    # Capture native exit codes and PowerShell failures, but never exit the shared shell process.
    # `exit` is only valid for the fresh one-shot process below: using it in the persistent shell
    # kills the shell before its completion marker, causing a 120 s wait and duplicate execution.
    # psprep: the command goes as one line (base64), is parsed before running, and command and
    # parameter names are checked — a wrong command doesn't run halfway.
    cmd, fixes = psprep.autofix(cmd)
    note = "[auto-fixed: " + "; ".join(fixes) + "]\n" if fixes else ""
    # A protected path must not be executed inside a shell that may have escaped cwd, so protected
    # commands always go through a fresh one-shot spawn.
    if shell.ENABLED and not shell.protected(cmd) and not shell.nested_shell(cmd) and not EXIT_STMT.search(cmd):
        # A recursive listing that stalls loops forever on a folder named with spaces/NBSP (see RECURSE): stop it.
        hid = _new_bg_id() if promote and not RECURSE.search(cmd or "") else None
        log = None
        if hid:
            (HOME / "bg").mkdir(parents=True, exist_ok=True)
            log = HOME / "bg" / f"{hid}.log"
        try:
            r = await asyncio.to_thread(shell.SHELL.run, psprep.wrap(cmd), limit, str(log) if log else None)
        except asyncio.CancelledError:
            # Cancelling asyncio.to_thread does not stop its worker. Retire the persistent process
            # explicitly so the command cannot keep running after the turn was cancelled.
            shell.SHELL.stop()
            raise
        if r is not None and r[0] == shell.Shell.RUN_DETACHED:
            return note + _detached_ps(hid, r[1], log, limit, _rstrip_lines(r[2]))
        if r is not None:
            code, out, err = r
            body = _rstrip_lines("\n".join(x for x in (out, err) if x))
            if code == shell.SHELL.RUN_TIMEOUT:
                if body == "persistent PowerShell timed out":
                    body = ""   # the shell's own placeholder for "no output", not command output
                detail = (f"persistent PowerShell timed out after {limit} s; the command and its child processes were "
                          "stopped and not automatically retried")
                return f"Error: {detail}.\n{_head_tail(body, PS_OUT_MAX)}{_timeout_hint(cmd, body)}"
            if code == shell.SHELL.RUN_LOST:
                detail = "persistent PowerShell ended before completion; command may have run and was not automatically retried"
                return f"Error: {detail}.\n{_head_tail(body, PS_OUT_MAX)}{_timeout_hint(cmd, body)}"
            return note + _ps_result(code, body)
    # One-off run: the script goes to a temporary .ps1 (BOM — for Cyrillic in PS 5.1), no -Command length limit.
    fd, script = tempfile.mkstemp(prefix="maxedcode_ps_", suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(psprep.wrap(cmd, one_shot=True))
    try:
        try:
            p = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=shell.NO_WINDOW,
            )
        except OSError as e:
            return f"Error: could not start PowerShell: {type(e).__name__}: {e}"
        try:
            out, err = await asyncio.wait_for(p.communicate(), limit)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            _kill(p)
            if isinstance(e, asyncio.CancelledError):
                raise
            return f"Error: {limit} s timeout, the command was interrupted." + _timeout_hint(cmd, "")
    finally:
        with suppress(OSError):
            os.unlink(script)
    body = _rstrip_lines(out.decode("utf-8", "replace") + err.decode("utf-8", "replace"))
    return note + _ps_result(p.returncode, body)


async def _run_sh(cmd, timeout=None, promote=False):
    """Linux/macOS: _run_ps for the bash tool. The same live-shell / one-shot split and the same timeout,
    background and result rules; no PowerShell wrapper, a `bash -n` syntax pre-check instead of psprep."""
    cmd = shell.posix_text(cmd)
    limit = ps_timeout(cmd, timeout)
    pre = await asyncio.to_thread(shell.syntax_error, cmd)
    if pre:
        return pre
    if shell.ENABLED and not shell.protected(cmd) and not shell.one_shot(cmd) and not EXIT_STMT.search(cmd):
        hid = _new_bg_id() if promote else None
        log = None
        if hid:
            (HOME / "bg").mkdir(parents=True, exist_ok=True)
            log = HOME / "bg" / f"{hid}.log"
        try:
            r = await asyncio.to_thread(shell.SHELL.run, cmd, limit, str(log) if log else None)
        except asyncio.CancelledError:
            shell.SHELL.stop()
            raise
        if r is not None and r[0] == shell.Shell.RUN_DETACHED:
            return _detached_ps(hid, r[1], log, limit, _rstrip_lines(r[2]))
        if r is not None:
            code, out, err = r
            body = _rstrip_lines("\n".join(x for x in (out, err) if x))
            if code == shell.SHELL.RUN_TIMEOUT:
                if body == f"{shell.LABEL} timed out":
                    body = ""
                return (f"Error: {shell.LABEL} timed out after {limit} s; the command and its child processes were "
                        f"stopped and not automatically retried.\n{_head_tail(body, PS_OUT_MAX)}{_sh_timeout_hint(body)}")
            if code == shell.SHELL.RUN_LOST:
                return (f"Error: {shell.LABEL} ended before completion; command may have run and was not automatically "
                        f"retried.\n{_head_tail(body, PS_OUT_MAX)}{_sh_timeout_hint(body)}")
            return _sh_result(code, body)
    # One-shot run: protected paths, exit/exec/set -e, or no live bash.
    try:
        p = await asyncio.create_subprocess_exec(
            shell.BASH, "-c", cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=posix_env(), **_SESSION)
    except OSError as e:
        return f"Error: could not start {shell.BASH}: {type(e).__name__}: {e}"
    try:
        out, _ = await asyncio.wait_for(p.communicate(), limit)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        _kill(p)
        if isinstance(e, asyncio.CancelledError):
            raise
        return f"Error: {limit} s timeout, the command was interrupted." + _sh_timeout_hint("")
    return _sh_result(p.returncode, _rstrip_lines(out.decode("utf-8", "replace")))


def _sh_result(code, body):
    if code != 0:
        return (f"Error: bash command exited with code {code}.\n{_head_tail(body, PS_OUT_MAX)}" if body
                else f"Error: bash command exited with code {code} (no output).")
    return body or f"(no output, exit code {code})"


def _sh_timeout_hint(body):
    if body.strip():
        return ("\n[hint] It was still working (partial output above); a partial result may be on disk. Remove"
                " it, then rerun with run_in_background and wait with bg_output(id, wait=N).")
    return ("\n[hint] Nothing was printed before the timeout: the command stalled in its first step (waiting for input"
            " or a prompt, a lock, the network). Do not rerun it as is. Split it so partial output arrives, pass a"
            " larger timeout (max 600), or start it with run_in_background and wait with bg_output(id, wait=N).")


def _bg_start_sh(cmd):
    """bg_start for bash: syntax pre-check, then `bash -c` in its own process group, output to the task log."""
    cmd = shell.posix_text(cmd)
    pre = shell.syntax_error(cmd)
    if pre:
        return pre
    d = HOME / "bg"
    d.mkdir(parents=True, exist_ok=True)
    hid = _new_bg_id()
    log = d / f"{hid}.log"
    handle = open(log, "w", encoding="utf-8")
    try:
        try:
            p = subprocess.Popen([shell.BASH, "-c", cmd], stdin=subprocess.DEVNULL, stdout=handle,
                                 stderr=subprocess.STDOUT, env=posix_env(), **_SESSION)
        except OSError as e:
            return f"Error: could not start background command: {type(e).__name__}: {e}"
    finally:
        handle.close()
    BG[hid] = (p, log)
    BG_STARTED[hid] = time.monotonic()
    return f'Background id {hid}: started. Check output with bg_output("{hid}"), stop with bg_kill("{hid}").'


PS_BG_MAX = int(os.environ.get("AGENT_PS_BG_MAX", "600"))


def _new_bg_id():
    hid = str(int(time.time() * 1000) % 10000000)
    while hid in BG:
        hid = str(int(hid) + 1)
    return hid


def _detached_ps(hid, task, log, limit, body):
    """A persistent-shell command that outlived its timeout, registered as a background task (DeepSeek Harness
    moves such commands to a job instead of killing them: the model then waits or stops it, no rerun)."""
    BG[hid] = (task, log)
    BG_STARTED[hid] = time.monotonic()
    BG_LIMIT[hid] = PS_BG_MAX
    asyncio.get_running_loop().create_task(watch_background(hid))
    return (f"[still running after {limit} s; NOT stopped, it continues as background id {hid}]\n"
            f"{_head_tail(body, PS_OUT_MAX) if body.strip() else '(no output yet)'}\n"
            f"Wait for it with bg_output(\"{hid}\", wait=N) (returns the moment it ends) or stop it with "
            f"bg_kill(\"{hid}\") if it is stuck or no longer needed. It is stopped after {PS_BG_MAX} s more.")


# A command that printed nothing in 120 s stalled in its first step. Without a hint the model decided
# "the shell is broken" and retried the same listing (two lost timeouts). Windows PowerShell 5.1
# Get-ChildItem -Recurse never ends in a tree with a folder named only with spaces/NBSP: it trims the
# name and lists the parent again (60 → 220 → 710 folders per depth level on a 73-folder Desktop;
# -Depth does not help: it returns the same files several times).
RECURSE = re.compile(r"(?i)(?<![\w$-])(?:Get-ChildItem|gci|ls|dir)\b[^|;]*\s-r(?:ecurse)?\b")


def _timeout_hint(cmd, body):
    # On a smaller tree the same loop ends with StackOverflowException instead of the timeout.
    overflow = "StackOverflowException" in body
    if body.strip() and not overflow:
        # A clone/download that was still printing progress: its half-written result is on disk.
        return ("\n[hint] It was still working (partial output above); a partial result may be on disk. Remove"
                " it, then rerun with run_in_background and wait with bg_output(id, wait=N).")
    hint = ("\n[hint] PowerShell crashed on endless recursion. Do not rerun it as is." if overflow else
            "\n[hint] Nothing was printed before the timeout: the command stalled in its first step. Do not rerun it as is.")
    if RECURSE.search(cmd or ""):
        return hint + (" Get-ChildItem -Recurse in Windows PowerShell 5.1 never ends when the tree has a folder whose"
                       " name is only spaces/NBSP (it lists the parent folder again; with -Depth it returns"
                       " duplicates). Walk the tree with py_run (os.walk) instead.")
    return hint + (" Split it so partial output arrives, pass a larger timeout (max 600), or start it with"
                   " run_in_background and wait with bg_output(id, wait=N).")


# `exit` in the live shell would kill the process itself: such commands go through a one-off run.
EXIT_STMT = re.compile(r"(?im)(?:^|[;{}])\s*exit(?:\s+-?\$?\w+)?\s*(?:$|[;}#])")


# PowerShell 5.1 prints the first stderr line of `native 2>&1` as an error record: "python : <line>" and
# five more lines of position, code echo, CategoryInfo and NativeCommandError. Only <line> is output;
# the rest was noise the model read after every failing test run.
NATIVE_RECORD = re.compile(
    r"(?m)^[^\n]*? : (?P<line>[^\n]*)\nAt (?:line:\d+|[^\n]*:\d+) char:\d+\n(?:\+[^\n]*\n){1,8}"
    r"[ \t]+\+ CategoryInfo[^\n]*\n(?:[ \t]+[^+\s][^\n]*\n){0,3}"
    r"[ \t]+\+ FullyQualifiedErrorId : NativeCommandError[^\n]*(?:\n|$)")


def strip_native_records(body):
    body = NATIVE_RECORD.sub(lambda m: m.group("line") + "\n", body or "")
    # The wrapper's own "| Out-Default" (psprep.RUN) must not show up in echoed error lines
    return psprep.unmark(body)


def _ps_result(code, body):
    if POSIX:
        return _sh_result(code, body)
    body = strip_native_records(body)
    pre = psprep.explain(body)
    if pre:
        return pre
    if code != 0:
        return f"Error: PowerShell command exited with code {code}.\n{_head_tail(body, PS_OUT_MAX)}" if body else f"Error: PowerShell command exited with code {code} (no output)."
    return body or f"(no output, exit code {code})"


BG = {}
BG_NOTIFICATIONS = []
KILLED = set()
BG_STARTED = {}
BG_LIMIT = {}     # per-task lifetime in seconds (a py_run moved to the background); others use BG_TIMEOUT
BG_READERS = {}   # output readers of promoted py_run tasks: the log is complete only when they end
# Tasks whose final output the model already got from bg_output: their completion notice is dropped,
# otherwise the same log arrived twice and the model reacted to it again.
REPORTED = set()
try:
    BG_TIMEOUT = max(1, int(os.environ.get("AGENT_BG_TIMEOUT", "3600")))
except (TypeError, ValueError):
    BG_TIMEOUT = 3600


def bg_start(cmd):
    """Start a background command and return its task id."""
    if POSIX:
        return _bg_start_sh(cmd)
    # The same auto-fix and pre-check as a regular command: otherwise the error shows only in the task log.
    cmd, _ = psprep.autofix(cmd)
    if shell.ENABLED:
        with suppress(Exception):
            r = shell.SHELL.run(psprep.wrap(cmd, check_only=True), 30)
            pre = r and psprep.explain(r[1])
            if pre:
                return pre
    cmd = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
           "try { [Console]::InputEncoding = $OutputEncoding } catch {}\n" + cmd)   # no BOM in piped stdin (psprep.RUN)
    d = HOME / "bg"
    d.mkdir(parents=True, exist_ok=True)
    hid = str(int(time.time() * 1000) % 10000000)
    log = d / f"{hid}.log"
    handle = open(log, "w", encoding="utf-8")
    try:
        try:
            p = subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], stdin=subprocess.DEVNULL,
                                 stdout=handle, stderr=subprocess.STDOUT, creationflags=shell.NO_WINDOW)
        except OSError as e:
            return f"Error: could not start background command: {type(e).__name__}: {e}"
    finally:
        handle.close()
    BG[hid] = (p, log)
    BG_STARTED[hid] = time.monotonic()
    return f'Background id {hid}: started. Check output with bg_output("{hid}"), stop with bg_kill("{hid}").'


def bg_output(hid):
    if hid not in BG:
        return f"Error: no background task {hid}"
    p, log = BG[hid]
    started = BG_STARTED.get(hid, time.monotonic())
    if p.poll() is None and time.monotonic() - started > BG_LIMIT.get(hid, BG_TIMEOUT):
        KILLED.add(hid)
        _kill(p)
    code = p.poll()
    # A task that was removed while still running is reported as killed, not as still running:
    # otherwise a wait on it never ends and the log file is read while still being written.
    if hid in KILLED:
        status = "killed"
    elif code is not None:
        status = f"finished, exit code {code}"
    else:
        status = "still running"
    if code is not None or hid in KILLED:
        REPORTED.add(hid)
    return f'[id {hid}: {status}]\n' + (_head_tail(strip_ansi(log.read_text(encoding="utf-8", errors="replace")), BG_OUT_MAX) or "(no output yet)")


async def _reader_done(hid, limit=2.0):
    """A promoted py_run writes its log through a reader task: let it flush the last output first."""
    reader = BG_READERS.get(hid)
    if reader is not None and not reader.done():
        with suppress(Exception):
            await asyncio.wait_for(asyncio.shield(reader), limit)


POLLED = set()   # tasks the model already saw as "still running"
POLL_WAIT = 30   # a repeated poll without wait blocks this long instead of spinning the model


async def bg_wait(hid, wait=None):
    """bg_output that first waits up to `wait` seconds for the task to end (returns the moment it does).

    The first look without wait returns at once (a server's startup line); a second poll of a task that
    is still running waits POLL_WAIT s, so polling cannot turn into a fast loop of model calls."""
    try:
        wait = min(float(POLL_WAIT if wait is None and hid in POLLED else wait or 0), PS_TIMEOUT_MAX)
    except (TypeError, ValueError):
        wait = 0
    if hid in BG and wait > 0:
        end = time.monotonic() + wait
        while BG[hid][0].poll() is None and hid not in KILLED and time.monotonic() < end:
            await asyncio.sleep(min(0.25, max(0.01, end - time.monotonic())))
    if hid in BG and BG[hid][0].poll() is not None:
        await _reader_done(hid)
    out = bg_output(hid)
    if hid in BG and hid not in REPORTED:
        POLLED.add(hid)
    return out


def bg_kill(hid):
    if hid not in BG:
        return f"Error: no background task {hid}"
    p, _ = BG[hid]
    if p.poll() is None:
        KILLED.add(hid)
        _kill(p)
        return f"Stopped background id {hid}."
    return f"id {hid} had already finished."


def cleanup():
    """Kill live processes on exit: background tasks and the persistent shell (nothing did this before)."""
    for hid, (p, _) in list(BG.items()):
        if p.poll() is None:
            KILLED.add(hid)
            _kill(p)
    shell.SHELL.stop()


atexit.register(cleanup)


async def watch_background(hid, callback=None):
    """Wait for a background task and report its output without a bg_output tool call."""
    while hid in BG:
        p, _ = BG[hid]
        if p.poll() is not None:
            break
        if time.monotonic() - BG_STARTED.get(hid, time.monotonic()) > BG_LIMIT.get(hid, BG_TIMEOUT):
            KILLED.add(hid)
            _kill(p)
            break
        await asyncio.sleep(0.5)
    await _reader_done(hid)
    # A bg_output(wait=N) in progress polls every 0.25 s: let it take the result first, otherwise the same
    # log arrived twice (its answer and this notice).
    await asyncio.sleep(0.5)
    if hid not in BG or hid in REPORTED:
        return
    message = f"<background_task_completed id=\"{hid}\">\n{bg_output(hid)}\n</background_task_completed>"
    if callback is not None:
        try:
            # A callback that returns False declines: the notice then waits in the queue.
            if await callback(message) is not False:
                return
        except Exception:
            pass
    BG_NOTIFICATIONS.append(message)


def take_bg_notifications():
    """Drain task completion notices once, in completion order."""
    out = list(BG_NOTIFICATIONS)
    BG_NOTIFICATIONS.clear()
    return out
