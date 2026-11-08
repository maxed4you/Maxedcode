"""Persistent PowerShell for a session, with a serialized marker-based protocol.

A shared shell avoids process startup overhead and preserves cwd/variables between calls. Tool
commands report their exit status through a marker instead of exiting the shell. Calls are serialized
because they share stdin/stdout. If the process dies or the marker never arrives, return immediately
on EOF or kill it at the deadline; do not automatically retry a command that may already have run.
"""
import contextlib
import os
import re
import subprocess
import threading
import time
from queue import Empty, Queue

ENABLED = str(os.environ.get("AGENT_PERSISTENT_SHELL", "1")).lower() in ("1", "true", "yes")
IDLE_EXIT = int(os.environ.get("AGENT_SHELL_IDLE", "300"))       # seconds before the shell is retired
MARK_PREFIX = "__maxedcode_exit_"
# The child process gets its own hidden console: otherwise PowerShell changes the TUI console buffer
# (BufferSize, Write-Progress, Clear-Host) and Textual draws askew — the screen shifts up.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A live shell can leave cwd, so protected paths get their own one-shot execution path instead.
PROTECTED_MARK = ("protected", "forbidden", "refused", "denied")


def protected(cmd):
    """True when this command must not run inside a live shell that may have escaped cwd."""
    from .. import permissions
    try:
        return bool(permissions.protected(str(cmd or "")))
    except Exception:
        return False


# A nested powershell/pwsh inherits the live shell's stdin pipe, reads it and swallows the completion
# marker: the command then hangs until the 120 s timeout. cmd and batch files do the same: `set /p` or
# `pause` waits on that pipe (real T3 run: two 120 s / 60 s timeouts on a menu .bat). Such commands go
# through a one-shot spawn (stdin = DEVNULL) instead.
NESTED_PS = re.compile(r"(?:^|[\s;&|(])(?:powershell|pwsh|cmd)(?:\.exe)?(?=\s|$)"
                       r"|[\w\\/.:-]+\.(?:bat|cmd)\b", re.I)


def nested_shell(cmd):
    return bool(NESTED_PS.search(str(cmd or "")))


# Any native program started without piped input (python, node, a test binary) inherits the same pipe:
# `"5 2" | Measure-Command { python sol.py }` read the following protocol lines as its input, the
# shell hung until the 120 s timeout and later commands were swallowed (LCB abc393_e). PowerShell
# keeps reading commands through its already open reader, so the process standard input handle is
# swapped for an inheritable NUL once at start: such a child now reads EOF at once. Explicitly piped
# input (`"x" | python`) is unaffected. Costs ~0.3 s per shell start (one Add-Type).
STDIN_NUL = (
    "try { $__maxedcode_k = Add-Type -Namespace MaxedCode -Name StdIn -PassThru -MemberDefinition "
    "'[DllImport(\"kernel32.dll\")] public static extern bool SetStdHandle(int n, IntPtr h); "
    "[DllImport(\"kernel32.dll\")] public static extern bool SetHandleInformation(IntPtr h, int m, int f); "
    "[DllImport(\"kernel32.dll\", CharSet=CharSet.Unicode)] public static extern IntPtr CreateFileW("
    "string f, int a, int s, IntPtr sa, int c, int fl, IntPtr t);'; "
    "$__maxedcode_h = $__maxedcode_k::CreateFileW('NUL', -2147483648, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero); "
    "if ($__maxedcode_h -ne [IntPtr](-1)) { [void]$__maxedcode_k::SetHandleInformation($__maxedcode_h, 1, 1); "
    "[void]$__maxedcode_k::SetStdHandle(-10, $__maxedcode_h) } } catch {}"
)


class Detached:
    """A persistent shell whose command outlived its timeout, kept running as a background task: the rest of its
    output goes to the task log, and the shell is closed when the command's completion marker arrives. The live
    shell slot is free at once; the next command starts a new shell (as after a timeout before)."""

    def __init__(self, proc, out, err, mark, lines, log):
        self.proc, self.pid, self.code, self.mark, self.out, self.err = proc, proc.pid, None, mark, out, err
        self.log = open(log, "w", encoding="utf-8", errors="replace")
        for line in lines:
            self.log.write(line + "\n")
        self.log.flush()
        threading.Thread(target=self._follow, daemon=True).start()

    def _drain_err(self):
        while True:
            try:
                line = self.err.get_nowait()
            except Empty:
                return
            if line is not None:
                self.log.write(line + "\n")

    def _follow(self):
        code = None
        try:
            while True:
                try:
                    line = self.out.get(timeout=0.5)
                except Empty:
                    self._drain_err()
                    if self.proc.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if line.startswith(self.mark):
                    try:
                        code = int(line[len(self.mark):])
                    except ValueError:
                        code = 0
                    break
                self.log.write(line + "\n")
                self.log.flush()
            self._drain_err()
        finally:
            with contextlib.suppress(Exception):
                self.log.close()
            with contextlib.suppress(Exception):
                self.proc.stdin.close()          # the shell exits on EOF of its command pipe
            with contextlib.suppress(Exception):
                self.proc.wait(5)
            if self.proc.poll() is None:
                self.kill()
            done = self.proc.poll()
            self.code = code if code is not None else (done if done is not None else -1)

    def poll(self):
        return self.code

    def kill(self):
        if os.name == "nt":
            with contextlib.suppress(Exception):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)], capture_output=True,
                               creationflags=NO_WINDOW, timeout=10)
        with contextlib.suppress(Exception):
            self.proc.kill()


class Shell:
    def __init__(self):
        self.proc = None
        self.out = Queue()
        self.err = Queue()
        self.alive = False
        self.last = time.monotonic()
        # All calls share one stdin/stdout pair; concurrent readers corrupt the protocol.
        self.lock = threading.Lock()

    def start(self):
        if self.alive:
            return True
        self.out, self.err = Queue(), Queue()
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)
        except Exception:
            return False
        self.alive = True
        for stream, q, is_stdout in ((self.proc.stdout, self.out, True), (self.proc.stderr, self.err, False)):
            threading.Thread(target=self._pump, args=(stream, q, is_stdout, self.proc), daemon=True).start()
        try:
            # Prints nothing; runs before the first command, so no extra round trip is awaited.
            self.proc.stdin.write(STDIN_NUL + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        return True

    def _pump(self, stream, q, is_stdout=False, proc=None):
        try:
            for line in stream:
                q.put(line.rstrip("\n"))
        finally:
            # Wake a waiting caller immediately when the process closes its pipe.
            q.put(None)
            # Only for the current shell: a detached one closing later must not mark its successor dead.
            if is_stdout and (proc is None or proc is self.proc):
                self.alive = False

    def stop(self):
        """Kill the shell with its whole process tree: after a timeout a plain kill left the child
        (git clone, pip) running, holding files open, and the next command collided with it."""
        self.alive = False
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                if subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                                  creationflags=NO_WINDOW, timeout=10).returncode == 0:
                    return
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    # Negative codes are internal transport outcomes, not PowerShell exit codes.
    RUN_TIMEOUT = -124
    RUN_LOST = -125
    RUN_DETACHED = -126

    def run(self, command, timeout=120, detach_log=None):
        """Run one command; never report a timed-out/lost execution as safe to retry. With detach_log a command
        that times out keeps running: (RUN_DETACHED, Detached, output so far) instead of a killed shell."""
        with self.lock:
            return self._run_locked(command, timeout, detach_log)

    def _run_locked(self, command, timeout=120, detach_log=None):
        if not self.alive and not self.start():
            return None  # No command was submitted; a one-shot fallback is safe.
        proc = self.proc
        nonce = str(int(time.monotonic() * 1000) % 1_000_000)
        mark = MARK_PREFIX + nonce + "__"
        try:
            proc.stdin.write(command + "\n")
            # The command wrapper sets __maxedcode_run_exit; this separate line reports it.
            # A unique marker prevents ordinary command output being mistaken for completion.
            proc.stdin.write(f'Write-Output ("{mark}" + [string]$global:__maxedcode_run_exit)\n')
            proc.stdin.flush()
        except Exception:
            self.stop()
            return self.RUN_LOST, "", "Could not submit command to the persistent PowerShell; it was not retried."
        self.last = time.monotonic()
        out, code = [], None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = self.out.get(timeout=min(0.1, max(0.01, end - time.monotonic())))
            except Empty:
                # Do not sleep until the 120-second deadline after PowerShell has already died.
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            if line.startswith(mark):
                try:
                    code = int(line[len(mark):])
                except ValueError:
                    code = 0
                break
            out.append(line)

        err = []
        while True:
            try:
                line = self.err.get_nowait()
                if line is not None:
                    err.append(line)
            except Empty:
                break

        if code is None:
            # The command was submitted. It may already have had side effects, so do not rerun it.
            timed_out = time.monotonic() >= end and proc.poll() is None
            if timed_out and detach_log:
                try:
                    task = Detached(proc, self.out, self.err, mark, out + err, detach_log)
                except OSError:
                    task = None
                if task is not None:
                    self.proc, self.alive = None, False      # the next command starts a fresh shell
                    return self.RUN_DETACHED, task, "\n".join(out + err)
            self.stop()
            detail = "persistent PowerShell timed out" if timed_out else "persistent PowerShell ended before its completion marker"
            body = "\n".join(out)
            if err:
                body = (body + "\n" + "\n".join(err)).strip()
            return (self.RUN_TIMEOUT if timed_out else self.RUN_LOST), body or detail, ""
        return code, "\n".join(out), "\n".join(err)

    def idle_too_long(self):
        return self.alive and (time.monotonic() - self.last) > IDLE_EXIT


SHELL = Shell()
