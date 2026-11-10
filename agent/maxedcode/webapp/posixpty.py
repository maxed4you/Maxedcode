"""The side panel's terminal on macOS / Linux: the user's shell on a pseudo terminal (pty.openpty), the same
interface as the Windows ConPTY one (conpty.py). No fork in this (threaded) server: subprocess starts the shell
with the pty's slave end as its controlling terminal."""
import codecs
import errno
import fcntl
import os
import signal
import struct
import subprocess
import termios
import threading

from .. import plat

KEEP = 2_000_000            # characters of output kept for a reattaching page


def shell():
    """The user's login shell ($SHELL), else bash, else sh."""
    return plat.user_shell()


def _set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Pty:
    """One shell on a pseudo terminal. Output accumulates in `text` (offset `base` is where it starts after
    trimming); readers wait on `changed`."""

    def __init__(self, cwd, cols=100, rows=30, command=None):
        import pty as _pty
        self.cwd, self.cols, self.rows = cwd, max(2, cols), max(2, rows)
        self.text, self.base, self.exit = "", 0, None
        self.changed = threading.Condition()
        master, slave = _pty.openpty()
        _set_size(slave, self.cols, self.rows)
        env = {**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color", "COLORTERM": "truecolor"}
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        argv = command if isinstance(command, list) else [command] if command else [shell(), "-l"]

        def child_setup():
            # runs after setsid (start_new_session): the pty becomes the new session's controlling terminal, so
            # job control, Ctrl+C and the shell's prompt work, and close() ends the whole group. One syscall only:
            # the server has threads, and nothing else may run between fork and exec.
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                         close_fds=True, start_new_session=True, preexec_fn=child_setup)
        finally:
            os.close(slave)
        self.fd = master
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        """The shell ended (exit): readers learn the code."""
        code = self.proc.wait()
        with self.changed:
            if self.exit is None:
                self.exit = code if code >= 0 else 128 - code
            self.changed.notify_all()

    def _read(self):
        """Drains the pty into `text`; a UTF-8 character split between two reads waits for its tail."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as e:          # EIO: the shell (the slave end) is gone
                if e.errno == errno.EINTR:
                    continue
                break
            except TypeError:             # close() took the descriptor
                break
            if not data:
                break
            self._add(decoder.decode(data))

    def _add(self, text):
        if not text:
            return
        with self.changed:
            self.text += text
            if len(self.text) > KEEP:
                drop = len(self.text) - KEEP // 2
                self.text, self.base = self.text[drop:], self.base + drop
            self.changed.notify_all()

    def since(self, offset, timeout=25):
        """(new offset, output after `offset`, exit code or None); waits up to `timeout` s for something new."""
        with self.changed:
            if self.base + len(self.text) <= offset and self.exit is None:
                self.changed.wait(timeout)
            start = max(0, offset - self.base)
            return self.base + len(self.text), self.text[start:], self.exit

    def write(self, data):
        raw = data.encode("utf-8")
        while raw and self.fd is not None:
            try:
                raw = raw[os.write(self.fd, raw):]
            except OSError:
                return

    def resize(self, cols, rows):
        self.cols, self.rows = max(2, int(cols)), max(2, int(rows))
        try:
            # the kernel sends SIGWINCH to the terminal's foreground job itself (the shell, or vim running in it)
            _set_size(self.fd, self.cols, self.rows)
        except (OSError, TypeError):
            return

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        """Ends the shell and everything it started (its process group), then the pty."""
        if self.alive():
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    break
                try:
                    self.proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        with self.changed:
            if self.exit is None:
                self.exit = 0
            self.changed.notify_all()
