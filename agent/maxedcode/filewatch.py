"""Files changed on disk by a tool that is not an edit tool (powershell, py_run, subagent, MCP).

Edit tools draw their own diff from the call. Anything else that writes (Set-Content, a redirect,
New-Item, a Python script, a subagent) used to change files without the user seeing it. The UI takes
a snapshot of the working folder (and of the absolute file paths the call names) before such a tool
and diffs it after. The model gets nothing from this: it is display only, so the prompt stays the same.

Cost: one stat walk per call; file text is read once and re-read only when its mtime changes.
"""
import os
import re
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vs", ".next", ".cache"}
MAX_FILES = 5000           # a bigger folder (the home folder, a drive root) is not watched recursively
MAX_TEXT = 256_000         # bytes; a larger file is reported by size only
MAX_CACHE = 32_000_000     # total bytes of remembered text
_ABS_PATH = re.compile(r"""(?:"|')([A-Za-z]:[\\/][^"'\r\n]+)(?:"|')|([A-Za-z]:[\\/][^\s"'<>|*?;,()]+)""")


def named_files(text):
    """Absolute file paths named in a command or script (outside the working folder they are watched one by one)."""
    out = []
    for m in _ABS_PATH.finditer(text or ""):
        p = (m.group(1) or m.group(2)).rstrip(".\\/")
        if p and p not in out:
            out.append(p)
    return out[:20]


def watched_files(name, args):
    """None when the tool cannot change files or draws its own diff; else the absolute files it names."""
    from .tools import ps_readonly
    if name == "powershell" and not args.get("run_in_background") and not ps_readonly(args.get("command") or ""):
        return named_files(args.get("command"))
    if name == "py_run":
        return named_files(args.get("code"))
    if name == "subagent" or name.startswith("mcp__"):
        return []
    return None


def scratch_note(changes, root, asked=""):
    """A shell command or script that left new files in the project: ask once to remove check-only ones.
    Live T3 run: three probe .bat files were left in the user's folder after the fix. A file the user's
    request names (asked) is the deliverable, not a probe."""
    root = os.path.normcase(os.path.abspath(root)) + os.sep
    asked = (asked or "").lower()
    made = [os.path.relpath(p, root) for kind, p, _old, _new in changes
            if kind == "new" and os.path.normcase(p).startswith(root) and not os.path.basename(p).startswith(".")
            and os.path.basename(p).lower() not in asked]
    if not made:
        return ""
    names = ", ".join(made[:5]) + (f" and {len(made) - 5} more" if len(made) > 5 else "")
    return (f"\n[CLI] New file(s) in the project from this call: {names}. If they are only for a check, "
            "delete them before you answer; put further check files in %TEMP%.")


KEEP_TEXT = 64_000   # chars of old/new text a saved session keeps per file for /history


def saved_changes(changes):
    """Changes as a session keeps them (JSON lists); a long text is dropped and shown by name only."""
    keep = lambda t: t if t is None or len(t) <= KEEP_TEXT else None
    return [[kind, path, keep(old), keep(new)] for kind, path, old, new in changes[:20]]


def decode(data):
    """Text of a file for the diff, or None for a binary file."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace").replace("\r\n", "\n")
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")


class FileWatch:
    """Remembers file stats and text between tool calls; diff() reports what a call changed."""

    def __init__(self, texts=True):
        self.texts = texts      # False: stats only (enough to see which files a call created)
        self.text = {}          # path -> (mtime_ns, size, raw bytes of a text file, or None)
        self.cached = 0
        self.too_big = set()    # roots that had more than MAX_FILES files
        self.old_raw = {}       # path -> bytes before the last diffed call (changed/deleted text files)

    def _walk(self, root):
        stats, stack = {}, [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.name not in SKIP_DIRS:
                                    stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat()
                                stats[os.path.normcase(e.path)] = (e.path, st.st_mtime_ns, st.st_size)
                                if len(stats) > MAX_FILES:
                                    return None
                        except OSError:
                            continue
            except OSError:
                continue
        return stats

    def scan(self, root, files=()):
        """{key: (path, mtime_ns, size)} for the root folder tree and the named files."""
        stats = {}
        root = os.path.abspath(root)
        if root not in self.too_big:
            walked = self._walk(root)
            if walked is None:
                self.too_big.add(root)
            else:
                stats = walked
        for f in files:
            try:
                st = os.stat(f)
            except OSError:
                continue
            if os.path.isfile(f):
                stats[os.path.normcase(os.path.abspath(f))] = (os.path.abspath(f), st.st_mtime_ns, st.st_size)
        return stats

    def _load(self, key, path, mtime, size):
        """Raw bytes of the text file at this stat, from the cache when it has not changed since.

        Bytes, not decoded text: /undo must put back the file exactly (CRLF, encoding) when a shell command
        or a script changed it; only edit tools made checkpoints before."""
        hit = self.text.get(key)
        if hit and hit[0] == mtime and hit[1] == size:
            return hit[2]
        raw = None
        if size <= MAX_TEXT and (self.cached + size <= MAX_CACHE or hit):
            try:
                raw = Path(path).read_bytes()
            except OSError:
                raw = None
            if raw is not None and decode(raw) is None:
                raw = None          # binary: not shown, not kept
        if hit and hit[2] is not None:
            self.cached -= len(hit[2])
        if raw is not None:
            self.cached += len(raw)
        self.text[key] = (mtime, size, raw)
        return raw

    def _read(self, key, path, mtime, size):
        """Text of the file at this stat (for the diff), or None."""
        raw = self._load(key, path, mtime, size)
        return decode(raw) if raw is not None else None

    def before(self, root, files=()):
        """Snapshot before a call: stats, plus the bytes of every file that changed since the last one."""
        stats = self.scan(root, files)
        if self.texts:
            for key, (path, mtime, size) in stats.items():
                self._load(key, path, mtime, size)
        return stats

    def diff(self, root, files, before):
        """[(kind, path, old, new)] with kind new|changed|deleted; old/new are None when not text."""
        after = self.scan(root, files)
        out = []
        self.old_raw = {}
        for key, (path, mtime, size) in after.items():
            was = before.get(key)
            if was is None:
                out.append(("new", path, "", self._read(key, path, mtime, size) if self.texts else None))
            elif not self.texts:
                continue
            elif was[1:] != (mtime, size):
                hit = self.text.get(key)
                old_raw = hit[2] if hit and hit[:2] == was[1:] else None
                old = decode(old_raw) if old_raw is not None else None
                new = self._read(key, path, mtime, size)
                if old is None or new is None or old != new:
                    out.append(("changed", path, old, new))
                    if old_raw is not None:
                        self.old_raw[path] = old_raw
        for key, (path, _mtime, _size) in before.items():
            if key not in after and not os.path.exists(path):
                hit = self.text.pop(key, None)
                if hit and hit[2] is not None:
                    self.cached -= len(hit[2])
                    self.old_raw[path] = hit[2]
                out.append(("deleted", path, decode(hit[2]) if hit and hit[2] is not None else None, ""))
        return sorted(out, key=lambda c: c[1].lower())
