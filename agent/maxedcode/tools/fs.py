"""File tools: read, write, edit, diff, grep."""
from pathlib import Path
from collections import OrderedDict
from contextlib import suppress
import ast
import importlib.util
import sys
from rich.text import Text
import difflib
import functools
import fnmatch
import os
import re
import shutil
import subprocess
import time
from . import undo
from .. import config, permissions
from ..config import MAX_FILES, NO_VISION, SETTINGS
from ..util import IMAGE_EXTS, IMAGE_NOTE, SKIP_DIRS, TEXT_EXT, clip, tool_output_dir

DECODE_WHOLE_MAX = 8_000_000   # read_file decodes files up to this size at once (encoding check); bigger ones stream
READ_LINE_MAX = 2000           # one shown line at most, like DeepSeek Harness read-render
GREP_FILES_MAX = 20000         # python grep fallback: files walked at most
GREP_TIME_MAX = 30             # ... and seconds (ripgrep has the same 30 s timeout above)
_TOOL_OUTPUT_RE = re.compile(r"^tool-output-\d+-\d+(?:-\d+)?\.txt$")


def _trusted_tool_output_file(p):
    """Allow targeted reads of files created by normalize_tool_output, not writes or directory walks."""
    try:
        candidate = Path(p).resolve(strict=False)
        trusted_dir = tool_output_dir().resolve(strict=False)
        if candidate.parent != trusted_dir or not _TOOL_OUTPUT_RE.fullmatch(candidate.name):
            return False
        # Resolve before comparing to reject symlinks escaping the private directory.
        return candidate.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def blocked(p, *, allow_tool_output_read=False, read=False):
    """Enforce protected paths; only an exact generated spill file may bypass read protection.
    read=True for read_file/grep/glob/folder listings: installed library source may be read."""
    if allow_tool_output_read and _trusted_tool_output_file(p):
        return None
    return permissions.protected(str(p), read=read)


def deny(p):
    return f"Error: protected path ({blocked(p)}): access denied"


def shown(path):
    """A path as a result shows it: relative to the working directory when it is inside it.

    Every new token of a tool result costs prefill time on a local server; the absolute path in each
    header was 13% of all result text. A relative path still works as an argument: tool_validation
    resolves it against the same working directory."""
    try:
        rel = Path(path).resolve(strict=False).relative_to(Path.cwd().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(path)
    return str(rel) if rel.parts else str(path)
# rg globs for the DEFAULT protected set; with custom protected_paths we use the python walker instead
RG_EXCLUDE = ["!**/.env", "!**/.env.*", "!**/.ssh/**", "!**/AppData/**"]
DATA_EXTS = {".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson"}
READ_CACHE = OrderedDict()
try:
    READ_CACHE_LIMIT = max(0, min(200, int(SETTINGS.get("read_cache_size", 24))))
except (TypeError, ValueError):
    READ_CACHE_LIMIT = 24
EDIT_FAILURES = {}
# Files whose current content the model knows (it read or wrote them), with the (mtime, size) it saw.
# write_file over an existing file needs this: a blind overwrite from memory loses code silently.
READ_SEEN = {}
WRITE_NEEDS_READ = str(SETTINGS.get("write_requires_read", True)).lower() not in ("0", "false", "no")
# "# ... rest of the code unchanged": a whole-file rewrite where the model skipped real code.
LAZY_RE = re.compile(
    r"^[ \t]*(?:#|//|/\*|<!--|--|;|')?[ \t]*(?:"
    r"(?:\.\.\.|…)[ \t(\[]*(?:rest|remainder|existing|other|previous|same|unchanged|omitted|остальн|прежн|существующ|без изменений)"
    r"|(?:rest|remainder|existing|other|остальн)\S*[^\n]{0,40}?(?:unchanged|the same|as before|omitted|remains|без изменений|как было|прежн)"
    r")[^\n]{0,80}$"
    # Any language: a comment that opens with an ellipsis and words ("// … resto sin cambios", "# ... 其余代码不变").
    r"|^[ \t]*(?:#|//|/\*|<!--|--|;|')[ \t]*(?:\.\.\.|…)[ \t(\[]*[^\W\d_][^\n]{0,60}$", re.I | re.M)
CONFLICT_RE =re.compile(r"^(?:<<<<<<< .+|=======\s*$|>>>>>>> .+|\|\|\|\|\|\|\| .+)$", re.M)


def _conflict_error(text):
    matches = [(i, line) for i, line in enumerate(str(text).splitlines(), 1) if CONFLICT_RE.match(line)]
    if not matches:
        return ""
    sample = "; ".join(f"L{n}: {line[:60]}" for n, line in matches[:3])
    return f"Error: conflict markers detected; file was not written ({sample}). Resolve the markers before retrying."


def clear_read_cache(path=None):
    """Invalidate cached reads globally, or only the changed path and its ancestors.

    Directory reads include descendant file contents, so writing a nested file must also
    invalidate cached reads of every parent directory. Unrelated file reads stay cached.
    """
    if path is None:
        READ_CACHE.clear()
        return
    try:
        target = Path(path).expanduser().resolve(strict=False)
        invalid_paths = {os.path.normcase(str(target))}
        invalid_paths.update(os.path.normcase(str(parent)) for parent in target.parents)
    except (OSError, RuntimeError, TypeError, ValueError):
        # If the target cannot be normalized safely, correctness beats cache retention.
        READ_CACHE.clear()
        return
    for key in list(READ_CACHE):
        if key and key[0] in invalid_paths:
            READ_CACHE.pop(key, None)


def _stamp(path):
    try:
        st = Path(path).stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _seen_key(path):
    try:
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return os.path.normcase(str(path))


def mark_seen(path, only_known=False):
    """Remember that the model knows this file as it is now (after a read or its own write)."""
    key = _seen_key(path)
    if not only_known or key in READ_SEEN:
        READ_SEEN[key] = _stamp(path)


# An unread file this small is a stub or a scratch file. Refusing it cost a whole second generation of
# the new file (refused write, read, the same write again); it is replaced instead, and its old text is
# shown in the result and kept in a checkpoint (/undo).
BLIND_WRITE_MAX = 2048


def _stale_note(path):
    """A line for a failed edit when the file changed on disk after the model last read it, else ""."""
    known = READ_SEEN.get(_seen_key(path), False)
    if known is False or known == _stamp(path):
        return ""
    return ("\n[the file changed on disk after you last read it (the user, a command or a tool changed it): "
            "read_file it again before the next edit]")


def _unread(p, previous):
    """The file exists with content the model has never seen in this session."""
    return WRITE_NEEDS_READ and bool(previous.strip()) and READ_SEEN.get(_seen_key(p), False) is False


def _overwrite_error(p, previous, content):
    """Why write_file must not replace an existing file, or ""."""
    if not previous.strip():
        return ""
    if WRITE_NEEDS_READ:
        known = READ_SEEN.get(_seen_key(p), False)
        if known is False and len(previous.encode("utf-8")) > BLIND_WRITE_MAX:
            return (f"Error: {p} already exists and was not read in this session, so write_file would replace it blindly. "
                    "Change it with patch/edit_file, or read_file it first and then write the complete file.")
        if known is not False and known != _stamp(p):
            return (f"Error: {p} changed on disk after you last read it (the user, a command or a tool changed it). "
                    "read_file it again before rewriting it, or use patch/edit_file.")
    lazy = [m.group().strip() for m in LAZY_RE.finditer(content) if m.group().strip() not in previous]
    if lazy:
        return (f"Error: write_file refused, {p} was not changed: the content has a placeholder instead of real code "
                f"({lazy[0][:80]!r}). Writing it would delete that code. Send the complete file, or change only "
                "the needed parts with patch/edit_file.")
    return ""


def _cache_key(path, start, end, offset=None, limit=None):
    try:
        full = str(Path(path).resolve())
        st = Path(full).stat()
        fingerprint = (st.st_mtime_ns, st.st_size, Path(full).is_dir())
    except OSError:
        full, fingerprint = str(Path(path).absolute()), None
    return (os.path.normcase(full), start, end, offset, limit), fingerprint


def _edit_failure(path, message):
    key = os.path.normcase(str(Path(path).absolute()))
    EDIT_FAILURES[key] = EDIT_FAILURES.get(key, 0) + 1
    if EDIT_FAILURES[key] >= 2:
        message += ("\n\n[Edit fallback after repeated exact-match failures] Stop retrying the same old_text. "
                    "Option A: read_file, then write_file with the complete intended file (whole-file format). "
                    "Option B: call apply_diff with a standard unified diff (---/+++/@@ hunks). "
                    "Preserve unrelated code and change only the requested blocks.")
    return message


def _edit_success(path):
    EDIT_FAILURES.pop(os.path.normcase(str(Path(path).absolute())), None)


def _json_check(path):
    """A .json file that no longer parses, said right after the write (free: json.loads).

    A config broken by an edit used to show up only when a program read it, steps later. A file with
    comments (JSONC: tsconfig, VS Code settings) is not reported."""
    p = Path(path)
    if p.suffix.lower() != ".json":
        return ""
    import json
    try:
        text = decode_text(p.read_bytes())[0]
        json.loads(text.lstrip("﻿"))
    except json.JSONDecodeError as e:
        if re.search(r"(?m)^\s*//|/\*", text):
            return ""
        return f"\n[JSON validation FAILED] line {e.lineno}, column {e.colno}: {e.msg}. Fix it before using the file."
    except (OSError, ValueError):
        return ""
    return ""


def _python_check(path):
    """Run syntax validation plus the first available local linter after a .py write.

    Only problems are reported: "ast.parse: OK" and "lint: skipped" after every edit were tokens the
    model read on each step and never acted on."""
    p = Path(path)
    if p.suffix.lower() != ".py":
        return ""
    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"\n[Python validation] Could not read the written file: {e}"
    try:
        ast.parse(source, filename=str(p))
    except SyntaxError as e:
        where = f"line {e.lineno}" if e.lineno else "unknown line"
        return f"\n[Python validation FAILED] ast.parse: {where}: {e.msg}. Fix this syntax error before proceeding."
    report = ["[Python validation] ast.parse: OK"]
    ruff = shutil.which("ruff")
    pyflakes = shutil.which("pyflakes")
    if ruff:
        # --no-cache: otherwise every write leaves a .ruff_cache folder in the user's project
        cmd = [ruff, "check", "--no-cache", "--output-format", "concise", str(p)]
        label = "ruff"
    elif pyflakes:
        cmd = [pyflakes, str(p)]
        label = "pyflakes"
    elif importlib.util.find_spec("ruff") is not None:
        cmd, label = [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "concise", str(p)], "ruff"
    elif importlib.util.find_spec("pyflakes") is not None:
        cmd, label = [sys.executable, "-m", "pyflakes", str(p)], "pyflakes"
    else:
        return ""
    # A linter that times out or cannot start is not a problem of the written file.
    with suppress(subprocess.SubprocessError, OSError, ValueError):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).strip()
            return "\n" + "\n".join(report + [f"{label} found issues (exit {proc.returncode}):\n"
                                              f"{output[:3500] or '(no details)'}"])
    return ""


# Code that changes the user's real system. A small model checks such code by running it or lets an
# existing test reach it (main() of an installer wrote the test's temp folder into the real PATH).
SYSTEM_CODE = [
    (re.compile(r"winreg\.(?:SetValue|CreateKey|DeleteValue|DeleteKey)|(?:Set|New|Remove)-ItemProperty[^\n]*HK(?:CU|LM)"
                r"|\breg(?:\.exe)?\s+(?:add|delete)\b", re.I), "registry"),
    (re.compile(r"SetEnvironmentVariable|\bsetx\b", re.I), "user/system environment variables"),
    (re.compile(r"CreateShortcut", re.I), "shortcuts"),
    (re.compile(r"schtasks[^\n]*/create|Register-ScheduledTask|\bsc(?:\.exe)?\s+create\b|New-Service", re.I),
     "scheduled tasks/services"),
]
_SYSTEM_NOTED = set()


def _tests_importing(p):
    """Test files near p (its folder, the parent, their tests/) that import p's module."""
    mod = re.compile(rf"^\s*(?:from|import)\s+(?:[\w.]+\.)?{re.escape(p.stem)}\b", re.M)
    dirs = {p.parent, p.parent.parent, p.parent / "tests", p.parent.parent / "tests"}
    found = []
    for d in dirs:
        with suppress(OSError):
            for f in sorted(d.glob("test*.py"))[:200]:
                with suppress(OSError):
                    if f != p and mod.search(f.read_text(encoding="utf-8", errors="replace")):
                        found.append(f.name)
    return sorted(set(found))


def _system_code_note(path):
    """Once per file: it writes to the real system, so checks and tests must stay off the real call."""
    p = Path(path)
    key = os.path.normcase(str(p.absolute()))
    if key in _SYSTEM_NOTED or p.suffix.lower() not in (".py", ".ps1", ".psm1", ".bat", ".cmd"):
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    what = [label for rx, label in SYSTEM_CODE if rx.search(text)]
    if not what:
        return ""
    _SYSTEM_NOTED.add(key)
    tests = _tests_importing(p) if p.suffix.lower() == ".py" else []
    return (f"\n[system code] this file changes the real system ({', '.join(what)}). Do not run it on this PC to "
            "check it; tests must mock that call or switch it off." +
            (f" Existing tests import it: {', '.join(tests)} - make sure they cannot reach the real call, then run them."
             if tests else ""))


def _write_result(path, message):
    clear_read_cache(path)
    mark_seen(path, only_known=True)   # our own edit does not make a file we had read "changed on disk"
    return message + _python_check(path) + _json_check(path) + _system_code_note(path)

def _size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _entries(d):
    """(folders, files) of one folder, by name; an inaccessible folder gives empty lists."""
    try:
        items = sorted(os.scandir(d), key=lambda e: e.name.lower())
    except OSError:
        return [], []
    dirs, files = [], []
    for e in items:
        with suppress(OSError):
            (dirs if e.is_dir() else files).append(e)
    return dirs, files


def tool_read_dir(root, small=4000, budget=14000, depth=3, per_dir=(60, 15, 8), max_lines=120):
    """Folder overview in one call: a tree by levels (the whole top level first) and the text of small files.

    Breadth-first: deep folders of a third-party program (LM Studio with 10,000 files) don't crowd out
    the neighbouring top-level folders. Texts come only from the folder itself and its direct subfolders."""
    root = Path(root)
    lines, bodies, used, more = [], [], 0, 0
    level = [root]
    for lvl in range(depth):
        nxt = []
        for d in level:
            dirs, files = _entries(d)
            cap = per_dir[min(lvl, len(per_dir) - 1)]
            for is_dir, e in ([(True, x) for x in dirs] + [(False, x) for x in files])[:cap]:
                p = Path(e.path)
                rel = p.relative_to(root)
                if len(lines) >= max_lines:
                    more += 1
                    continue
                if is_dir:
                    if e.name in SKIP_DIRS or blocked(p, read=True):
                        lines.append(f"{rel}\\  ({'protected' if blocked(p, read=True) else 'skipped'})")
                        continue
                    sub_d, sub_f = _entries(p)
                    lines.append(f"{rel}\\  ({len(sub_d)} folders, {len(sub_f)} files)")
                    nxt.append(p)
                    continue
                if blocked(p, read=True):
                    lines.append(f"{rel}  (protected)")
                    continue
                try:
                    size = e.stat().st_size
                except OSError:
                    continue
                lines.append(f"{rel}  ({_size(size)})")
                if (lvl < 2 and len(bodies) < MAX_FILES and size <= small and used + size <= budget
                        and p.suffix.lower() in TEXT_EXT
                        # a tiny csv/json (≤1 KB) is text too: otherwise the model reads it with a second call
                        and (p.suffix.lower() not in DATA_EXTS or size <= 1024)):
                    with suppress(OSError):
                        raw = p.read_bytes()
                        if b"\0" not in raw:
                            bodies.append(f"--- {rel}\n" + raw.decode("utf-8", errors="replace").rstrip())
                            used += size
            if len(dirs) + len(files) > cap:
                lines.append(f"{d.relative_to(root) if d != root else '.'}\\ ...  (+{len(dirs) + len(files) - cap} more)")
        level = nxt
    if more:
        lines.append(f"...[{more} more entries not shown; read_file a subfolder for its contents]")
    return clip(f"[folder {shown(root)}: tree {depth} levels deep, top level first; text of small files below; "
                "for more: read_file a subfolder, grep to search]\n"
                + "\n".join(lines) + ("\n\n" + "\n\n".join(bodies) if bodies else ""), 16000)


def _shortcut(p):
    """Windows shortcut: where it points (via WScript.Shell), not its binary content."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:MAXEDCODE_LNK);"
          "'target: '+$s.TargetPath;'arguments: '+$s.Arguments;'working dir: '+$s.WorkingDirectory")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15,
                           env={**os.environ, "MAXEDCODE_LNK": str(p)}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = r.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        info = f"(could not resolve: {type(e).__name__})"
    return f"[{p}: Windows shortcut]\n{info}"
def _bom_encoding(head):
    """Encoding by BOM: PowerShell 5.1 logs (Out-File, >) are written in UTF-16 LE."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    return "utf-8-sig" if head[:3] == b"\xef\xbb\xbf" else "utf-8"


@functools.lru_cache(maxsize=1)
def ansi_codepage():
    """The Windows ANSI code page (cp1251 on Russian Windows), or None when the system code page is UTF-8."""
    import codecs
    import locale
    with suppress(LookupError, ValueError):
        enc = locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)
        name = codecs.lookup(enc).name
        return None if name in ("utf-8", "ascii") else name
    return None


def decode_text(raw):
    """File bytes -> (text, encoding); encoding is None when the bytes are not text in UTF-8 or the ANSI code page.

    Old .bat/.txt files on Russian Windows are cp1251: decoded with errors="replace" they showed "������",
    and edit_file crashed on them, so a later write_file lost the text. Newlines are left as they are."""
    enc = _bom_encoding(raw[:4])
    if enc != "utf-8":
        return raw.decode(enc, "replace"), enc
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        legacy = ansi_codepage()
        if legacy:
            with suppress(UnicodeDecodeError):
                return raw.decode(legacy), legacy
        return raw.decode("utf-8", "replace"), None


def encode_text(path, text, enc):
    """(bytes, "") in the file's own encoding, or (None, error) when the text does not fit that encoding."""
    try:
        return text.encode(enc or "utf-8"), ""
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end]
        return None, (f"Error: {shown(path)} is {enc} text and {bad!r} cannot be stored in {enc}; "
                      "file was not changed. Use only characters that exist in that encoding.")


def not_text_error(path):
    return f"Error: {shown(path)} is not text in UTF-8 or {ansi_codepage() or 'the ANSI code page'}; file was not changed."


def _eol(text):
    """The line ending a file mostly uses: "\\r\\n", "\\n", or None when it has no line breaks.

    By majority: one stray CRLF used to turn a whole LF file into CRLF on the next edit."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if not crlf and not lf:
        return None
    return "\r\n" if crlf > lf else "\n"


def _encoding_note(enc):
    return "" if enc in ("utf-8", "utf-8-sig", None) else f" Encoding {enc} (not UTF-8); edits keep it."


def _tool_read_uncached(path, start=None, end=None, offset=None, limit=None):
    if blocked(path, allow_tool_output_read=True, read=True):
        return deny(path)
    p = Path(path)
    if p.is_dir():
        return tool_read_dir(p)
    try:
        size = p.stat().st_size
    except OSError as e:
        return f"Error: cannot stat {path}: {e}"
    if p.suffix.lower() == ".lnk" and os.name == "nt":
        return _shortcut(p)
    if p.suffix.lower() in IMAGE_EXTS and not NO_VISION:
        # The loop attaches the picture to this result (util.tool_content).
        dims = ""
        with suppress(Exception):
            from PIL import Image
            with Image.open(p) as im:
                dims = f"{im.size[0]}x{im.size[1]} px, "
        return f"[image {path}: {dims}{size:,} bytes]{IMAGE_NOTE}"
    try:
        with p.open("rb") as f:
            head = f.read(8192)
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    if b"\0" in head and _bom_encoding(head) != "utf-16":
        return f"[{path}: binary file, {size:,} bytes; not shown as text. Inspect it with py_run if really needed.]"
    # Large logs/data tables should be summarized or tailed in Python, not copied into model context.
    # Explicit line ranges remain available for narrow inspection.
    if p.suffix.lower() in DATA_EXTS and size > 24_000 and start is None and end is None and offset is None and limit is None:
        return (f"Error: {p.suffix.lower()} data file is {size:,} bytes; it was not read into context. "
                "Use one py_run to compute the requested counts/summary or print only the requested tail. "
                "For a specific excerpt, pass start_line/end_line.")
    if start is None and offset is not None:
        try:
            start = max(1, int(offset) + 1)
        except (TypeError, ValueError):
            start = 1
    a = max(1, int(start or 1))
    try:
        line_limit = max(1, min(2000, int(limit))) if limit is not None else 2000
    except (TypeError, ValueError):
        line_limit = 2000
    requested_b = min(int(end) if end is not None else a + line_limit - 1, a + line_limit - 1)
    selected = []
    n = 0
    try:
        if size <= DECODE_WHOLE_MAX:
            text, enc = decode_text(p.read_bytes())
            rows_all = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if rows_all and rows_all[-1] == "":
                rows_all.pop()
            n = len(rows_all)
            selected = rows_all[a - 1:requested_b]
        else:
            with p.open("rb") as f:
                enc = _bom_encoding(f.read(4))
            with p.open("r", encoding=enc, errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if a <= n <= requested_b:
                        selected.append(line.rstrip("\r\n"))
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    if n == 0:
        return "(file is empty)"
    if a > n:
        return f"Error: the file has only {n} lines"
    b = min(n, requested_b)
    # Cut by whole lines and point at the real file. Spilled by normalize_tool_output instead, the result
    # was head + tail of the numbered text and a copy file that a second read numbered once more.
    # Logs, data and saved tool output keep head + tail: their end (the error, the summary) matters most.
    room = config.read_output_limit() - 300
    if _trusted_tool_output_file(p) or p.suffix.lower() in DATA_EXTS:
        room = float("inf")
    rows, used = [], 0
    for i, line in enumerate(selected, a):
        if len(line) > READ_LINE_MAX:
            # Minified js/json: one line was 160k characters and pushed the whole read into a spill file.
            line = line[:READ_LINE_MAX] + f" …[line cut: {len(line):,} characters; inspect it with py_run]"
        row = f"{i}: {line}"
        if rows and used + len(row) + 1 > room:
            b = i - 1
            break
        rows.append(row)
        used += len(row) + 1
    tail = f" Continue: start_line={b + 1}." if b < n else ""
    if enc is None:
        tail += f" Not valid UTF-8 or {ansi_codepage() or 'ANSI'} text: '�' marks undecodable bytes; do not rewrite it."
    return f"[{shown(path)}: lines {a}-{b} of {n}.{_encoding_note(enc)}{tail}]\n" + "\n".join(rows)
def resolve_missing(path, depth=4, limit=20000):
    """A path that does not exist -> the only file with that name under the working directory, else None.

    A small model drops or doubles a folder when a name repeats (cwd ...\\config + config\\dev.json became
    ...\\config\\dev.json): read, edit, glob, edit again — four steps for one line. Only a UNIQUE name match
    is used, and edits still need their exact old_text, so a wrong file cannot be changed silently."""
    p = Path(str(path or ""))
    if not p.name or p.exists():
        return None
    root, want, hits, seen = Path.cwd(), p.name.lower(), [], 0
    for dirpath, dirs, files in os.walk(root):
        level = len(Path(dirpath).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")] if level < depth else []
        seen += len(files) + len(dirs)
        hits += [Path(dirpath) / f for f in files if f.lower() == want]
        if len(hits) > 1 or seen > limit:
            return None
    return hits[0] if hits and not blocked(hits[0], read=True) else None


def _resolve_path_arg(fn):
    """Run a file tool on the unique same-name file when the given path does not exist, and say so."""
    @functools.wraps(fn)
    def wrapper(path, *args, **kwargs):
        alt = resolve_missing(path)
        if alt is None:
            return fn(path, *args, **kwargs)
        out = fn(str(alt), *args, **kwargs)
        return f"[{path} does not exist; used the only file with that name in the working directory: {alt}]\n{out}"
    return wrapper


@_resolve_path_arg
def tool_read(path, start=None, end=None, offset=None, limit=None):
    """Read with a small LRU cache; any write through this module invalidates it."""
    result = _tool_read_cached(path, start, end, offset, limit)
    if not str(result).startswith("Error") and Path(path).is_file():
        mark_seen(path)
    return result


ATTACH_RE = re.compile(r'@(?:"([^"]+)"|([^\s,;]+?))(?:#(\d+)(?:-(\d+))?)?(?=[\s,;]|$)')


def attach_files(text):
    """Resolve @path and @path#start-end references into bounded read-only attachments after the text
    (the TUI, headless and the window send the same message, so the model does not spend a step on read_file)."""
    from ..util import ATTACH_MARK
    attachments = []
    for match in ATTACH_RE.finditer(text):
        raw_path = (match.group(1) or match.group(2) or "").rstrip(".,)")
        if not raw_path or not (match.group(1) or Path(raw_path).exists()):
            continue                       # "@name" in plain text (a mention, an e-mail) is not a file
        first = int(match.group(3)) if match.group(3) else None
        last = int(match.group(4)) if match.group(4) else first
        try:
            result = tool_read(raw_path, first, last)
        except Exception as exc:
            result = f"Error reading attachment: {type(exc).__name__}: {exc}"
        range_label = f" lines {first}-{last}" if first is not None else " (first 2000 lines max)"
        attachments.append(f"<file_attachment path=\"{raw_path}\"{range_label}>\n{result}\n</file_attachment>")
    return text + (ATTACH_MARK + "\n\n".join(attachments) if attachments else "")


def _tool_read_cached(path, start, end, offset, limit):
    if not READ_CACHE_LIMIT:
        return _tool_read_uncached(path, start, end, offset, limit)
    key, fingerprint = _cache_key(path, start, end, offset, limit)
    cached = READ_CACHE.get(key)
    if cached and cached[0] == fingerprint:
        READ_CACHE.move_to_end(key)
        return cached[1]
    result = _tool_read_uncached(path, start, end, offset, limit)
    READ_CACHE[key] = (fingerprint, result)
    READ_CACHE.move_to_end(key)
    while len(READ_CACHE) > READ_CACHE_LIMIT:
        READ_CACHE.popitem(last=False)
    return result


def _edit_threshold():
    try:
        value = float(SETTINGS.get("edit_fuzzy_threshold", 0.6))
    except (TypeError, ValueError):
        value = 0.6
    return max(0.6, min(0.98, value))


def find_similar_lines(search, content, threshold=None):
    """Return the best similar snippet and line number as a *hint*, never apply it."""
    search = str(search or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    wanted = search.split("\n")
    if not search or not lines:
        return None
    threshold = _edit_threshold() if threshold is None else max(0.0, min(1.0, float(threshold)))
    # ratio() is quadratic and holds the GIL: a full scan of a 300-line file with a 20-line block took
    # 80 s and froze the UI. quick_ratio() is a linear upper bound of ratio(), so it ranks the starts
    # and prunes; the exact ratio runs only for the few best starts, under a time limit.
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(search)   # the search side is indexed once, not once per window
    n = min(len(wanted), len(lines))
    ranked = []
    for start in range(len(lines) - n + 1):
        matcher.set_seq1("\n".join(lines[start:start + n]))
        if (q := matcher.quick_ratio()) >= threshold:
            ranked.append((q, start))
    ranked.sort(reverse=True)
    # Length windows around the requested block handle small indentation/line-wrap differences.
    min_len = max(1, len(wanted) - 2)
    max_len = min(len(lines), len(wanted) + 2)
    best = (0.0, None, None)
    deadline = time.monotonic() + 2.0
    for _q, start in ranked[:8]:
        for count in range(min_len, max_len + 1):
            end = start + count
            if end > len(lines):
                break
            candidate = "\n".join(lines[start:end])
            matcher.set_seq1(candidate)
            if matcher.quick_ratio() <= best[0]:
                continue
            ratio = matcher.ratio()
            if ratio > best[0]:
                best = (ratio, start + 1, candidate)
        if time.monotonic() > deadline:
            break
    if best[0] < threshold:
        return None
    return {"ratio": round(best[0], 3), "line": best[1], "text": best[2]}


def _fuzzy_hint(search, content):
    match = find_similar_lines(search, content)
    if not match:
        return ""
    shown = match["text"]
    if len(shown) > 1200:
        shown = shown[:1197] + "..."
    return (f"\nDid you mean the fragment at line {match['line']} (similarity {match['ratio']:.0%})? "
            "No fuzzy edit was applied; copy the exact fragment or include more context:\n"
            f"<did_you_mean line=\"{match['line']}\">\n{shown}\n</did_you_mean>")


def apply_edits_dry_run(original, edits, filename="<memory>"):
    """Apply exact edits to memory and validate before touching disk; return (text, error)."""
    text = str(original).replace("\r\n", "\n").replace("\r", "\n")
    for index, edit in enumerate(edits or [], 1):
        if not isinstance(edit, dict):
            return None, f"Error: edit {index} must be an object"
        old, new = edit.get("old_text"), edit.get("new_text")
        if not isinstance(old, str) or not old:
            return None, f"Error: edit {index}: old_text must be a non-empty string"
        if not isinstance(new, str):
            return None, f"Error: edit {index}: new_text must be a string"
        old, new = old.replace("\r\n", "\n").replace("\r", "\n"), new.replace("\r\n", "\n").replace("\r", "\n")
        count = text.count(old)
        if count != 1:
            hint = _fuzzy_hint(old, text) if count == 0 else ""
            return None, f"Error: edit {index}: old_text found {count} times; it must occur exactly once.{hint}"
        text = text.replace(old, new, 1)
    conflict = _conflict_error(text)
    if conflict:
        return None, conflict
    if str(filename).lower().endswith(".py"):
        try:
            ast.parse(text, filename=str(filename))
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return None, f"Error: dry-run Python validation failed at {where}: {exc.msg}. File was not changed."
    return text, ""


# An edit that leaves the file as it was must not report "OK": the model would believe the fix landed.
NO_CHANGE = ("Error: nothing changed in {path}: {what}. The file is untouched. "
             "Write the new code in new_text, or stop if the file already has it.")


_LINE_NO = re.compile(r"^\d+:(?: |$)", re.M)   # read_file prints every line as "12: code"
LINE_NO_NOTE = " (the \"N: \" line-number prefixes copied from read_file were removed; do not copy them)"


def strip_line_numbers(old, new, text):
    """old_text copied from read_file together with its "12: " prefixes -> (old, new) without them.

    Only when every non-empty line of old_text has the prefix and the stripped fragment occurs
    exactly once; new_text loses the prefix only on the lines that carry it."""
    lines = [line for line in old.split("\n") if line.strip()]
    if not lines or not all(_LINE_NO.match(line) for line in lines):
        return None
    bare = _LINE_NO.sub("", old)
    if not bare.strip() or text.count(bare) != 1:
        return None
    return bare, _LINE_NO.sub("", new)


# Typographic characters a model writes instead of the ASCII in the file (and vice versa).
_UNI = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                      "\u00a0": " ", "\u2026": "..."})
_TOLERANT = (("trailing whitespace ignored", str.rstrip),
             ("indentation adjusted", str.strip),
             ("quotes/dashes/indentation normalized", lambda s: s.translate(_UNI).strip()))


def _indent(line):
    return line[:len(line) - len(line.lstrip())]


def tolerant_match(text, old, new):
    """old_text that matches whole lines only up to whitespace/typography -> (exact old, new, note).

    The levels go from strict to loose (like opencode's and codex's matchers); a level is used only
    when it finds exactly ONE place, so an edit is never applied to a guessed location. With adjusted
    indentation new_text is re-indented by the same shift. A small quantized model gets the indent
    wrong often, and every such miss used to cost a full extra step."""
    want = old.strip("\n").split("\n")
    if not any(x.strip() for x in want):
        return None
    lines = text.split("\n")
    n = len(want)
    for note, norm in _TOLERANT:
        key = [norm(x) for x in want]
        hits = [i for i in range(len(lines) - n + 1) if [norm(x) for x in lines[i:i + n]] == key]
        if len(hits) > 1:
            return None
        if not hits:
            continue
        i = hits[0]
        exact = "\n".join(lines[i:i + n])
        new = new.strip("\n")
        if norm is not str.rstrip:
            first = next(k for k, x in enumerate(want) if x.strip())
            have, had = _indent(lines[i + first]), _indent(want[first])
            new = "\n".join((have + x[len(had):] if x.startswith(had) else x) if x.strip() else x
                            for x in new.split("\n"))
        return exact, new, f" (old_text matched with {note}; next time copy it exactly)"
    return None


def strip_new_line_numbers(old, new, text):
    """new_text lines that carry a "186: " prefix copied from a read/edit result -> new_text without it.

    Only when the file itself has no such lines (so real "1: x" content is never touched) and the bare
    line is text the model meant: a line of old_text or of the file. Returns (new, note)."""
    if not _LINE_NO.search(new) or _LINE_NO.search(text) or _LINE_NO.search(old):
        return new, ""
    known = {x.strip() for x in (old + "\n" + text).split("\n") if x.strip()}
    out, fixed = [], False
    for line in new.split("\n"):
        bare = _LINE_NO.sub("", line, count=1)
        if bare != line and bare.strip() in known:
            line, fixed = bare, True
        out.append(line)
    return ("\n".join(out), LINE_NO_NOTE) if fixed else (new, "")


def _line_matches(text, needle):
    return [text[:m.start()].count("\n") + 1 for m in re.finditer(re.escape(needle), text)]


def _already_applied(old, new, text):
    """old_text is gone but new_text is there: the edit most likely landed in an earlier step.

    Without this a small model reads "found 0 times", copies old_text from its memory again and
    loops (aider asks "Are you sure you need this block?" for the same case)."""
    if len(new.strip()) < 10 or old in text or new.strip() not in text:
        return ""
    at = _line_matches(text, new.strip())[:3]
    return (f"\nnew_text is already in the file (L{', L'.join(map(str, at))}): this change was most likely applied "
            "in an earlier step. Do not send it again; re-read that place if unsure, or move on.")


@_resolve_path_arg
def tool_edit(path, old, new):
    if blocked(path):
        return deny(path)
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    raw, enc = decode_text(data)
    if enc is None:
        return not_text_error(path)
    crlf = _eol(raw) == "\r\n"
    t, old, new = raw.replace("\r\n", "\n"), old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old:
        return "Error: old_text is empty"
    if old == new:
        return NO_CHANGE.format(path=path, what="old_text and new_text are identical")
    n, note = t.count(old), ""
    if n == 0 and (fixed := strip_line_numbers(old, new, t)):
        (old, new), n, note = fixed, 1, LINE_NO_NOTE
    if n == 0 and (fixed := tolerant_match(t, old, new)):
        old, new, note = fixed
        n = t.count(old)
    new, note2 = strip_new_line_numbers(old, new, t)
    note = note or note2
    if n != 1:
        if n == 0:
            return _edit_failure(path, "Error: old_text found 0 times; exact text must match. " +
                                 "Copy the fragment exactly (spaces, indentation) via read_file."
                                 + (_already_applied(old, new, t) or _fuzzy_hint(old, t)) + _stale_note(path))
        positions = ", ".join(f"L{x}" for x in _line_matches(t, old)[:20])
        more = " …" if len(_line_matches(t, old)) > 20 else ""
        return _edit_failure(path, f"Error: old_text found {n} times at {positions}{more}; it must occur exactly once. Add neighbouring lines so the fragment is unique.")
    first = t[:t.index(old)].count("\n") + 1
    updated, error = apply_edits_dry_run(t, [{"old_text": old, "new_text": new}], filename=str(p))
    if error:
        return error
    out, error = encode_text(path, updated.replace("\n", "\r\n") if crlf else updated, enc)
    if error:
        return error
    undo.snap(str(p), data)
    p.write_bytes(out)
    _edit_success(path)
    if not note:
        # An exact match wrote exactly the model's new_text: echoing it back only costs prefill.
        return clip(_write_result(path, f"OK, edited {shown(path)}."), 5000)
    lines = updated.split("\n")
    a, b = max(1, first - 2), min(len(lines), first + new.count("\n") + 2)
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(a, b + 1))
    return clip(_write_result(path, f"OK, edited {shown(path)}.{note} Lines {a}-{b} now:\n{body}"), 5000)

@_resolve_path_arg
def tool_patch(path, blocks):
    """Apply multiple exact search/replace blocks with one checkpoint and one disk write."""
    if blocked(path):
        return deny(path)
    if not isinstance(blocks, list) or not blocks:
        return "Error: blocks must be a non-empty array of {old_text, new_text} objects"
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    raw, enc = decode_text(data)
    if enc is None:
        return not_text_error(path)
    crlf = _eol(raw) == "\r\n"
    text = raw.replace("\r\n", "\n")
    ranges, note, failed = [], "", []
    for i, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            return f"Error: block {i} must be an object with old_text and new_text"
        old, new = block.get("old_text"), block.get("new_text")
        if not isinstance(old, str) or not old:
            return f"Error: block {i}: old_text must be a non-empty string"
        if not isinstance(new, str):
            return f"Error: block {i}: new_text must be a string"
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0 and (fixed := strip_line_numbers(old, new, text)):
            (old, new), count, note = fixed, 1, LINE_NO_NOTE
        if count == 0 and (fixed := tolerant_match(text, old, new)):
            old, new, note = fixed
            count = text.count(old)
        new, note2 = strip_new_line_numbers(old, new, text)
        note = note or note2
        if count != 1:
            # Every block is checked, so one reply can fix all of them: stopping at the first failed block
            # cost one more step per further failed block.
            advice =("Copy the fragment exactly (spaces, indentation) via read_file." if count == 0
                      else "Add neighbouring lines so the fragment is unique.")
            hint = (_already_applied(old, new, text) or _fuzzy_hint(old, text)) if count == 0 and not failed else ""
            failed.append(f"block {i}: old_text found {count} times; it must occur exactly once. {advice}{hint}")
            continue
        pos = text.index(old)
        first = text[:pos].count("\n") + 1
        ranges.append((max(1, first - 2), min(text.count("\n") + 1,
                                              first + max(new.count("\n"), old.count("\n")) + 2)))
        text = text[:pos] + new + text[pos + len(old):]
    if failed:
        ok = len(blocks) - len(failed)
        head = (f"Error: {len(failed)} of {len(blocks)} block(s) did not match; nothing was applied, the file was "
                f"not changed" + (f" (the other {ok} matched: send them again together with the fixed ones)" if ok else ""))
        return head + ".\n" + "\n".join(failed) + _stale_note(path)
    if text == raw.replace("\r\n", "\n"):
        return NO_CHANGE.format(path=path, what="every block replaces text with the same text")
    conflict = _conflict_error(text)
    if conflict:
        return conflict
    if p.suffix.lower() == ".py":
        try:
            ast.parse(text, filename=str(p))
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return f"Error: dry-run Python validation failed at {where}: {exc.msg}. File was not changed."
    out, error = encode_text(path, text.replace("\n", "\r\n") if crlf else text, enc)
    if error:
        return error
    # One checkpoint means one /undo restores the entire multi-block patch.
    undo.snap(str(p), data)
    p.write_bytes(out)
    if not note:
        # Exact matches: the file has exactly the model's new_text, so the excerpt is left out.
        return clip(_write_result(path, f"OK, applied {len(blocks)} patch block(s) to {shown(path)}."), 5000)
    lines = text.split("\n")
    merged = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    excerpt = []
    for a, b in merged[:4]:
        excerpt.extend(f"{i}: {lines[i - 1]}" for i in range(a, min(b, len(lines)) + 1))
    return clip(_write_result(path, f"OK, applied {len(blocks)} patch block(s) to {shown(path)}.{note}\n"
                                + "\n".join(excerpt)), 5000)

def prev_content(path):
    """What the file contained before the change (empty if it is a new file)."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def disk_text(path, limit=2_000_000):
    """File text as it is on disk now: "" for a missing file, None when it cannot be compared."""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        if not p.is_file() or p.stat().st_size > limit:
            return None
        return p.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, TypeError, ValueError):
        return None
DIFF_LIMIT = 40
DEL_C, ADD_C = "#e06c75", "#7ec69a"
# whole-line background + a lighter background for the words that actually differ
DEL_LINE, DEL_WORD = f"{DEL_C} on #2a1619", f"{DEL_C} on #523034"
ADD_LINE, ADD_WORD = f"{ADD_C} on #16271c", f"{ADD_C} on #2f5138"
CTX_STYLE = "grey62"
# hex, not a name: rich 15 has no 'grey45' (only grey42/grey46), and an unknown
# color name crashes the textual compositor when a diff card is rendered
NUM_STYLE = {"del": "#9c555c on #2a1619", "ins": "#588a63 on #16271c", "ctx": "#737373"}
LINE_STYLE = {"del": DEL_LINE, "ins": ADD_LINE, "ctx": CTX_STYLE}
WORD_STYLE = {"del": DEL_WORD, "ins": ADD_WORD, "ctx": None}
SIGN = {"del": "-", "ins": "+", "ctx": " "}

def _toks(s):
    return re.findall(r"\S+|\s+", s)

def _word_pair(a, b):
    """Word-level opcodes between a changed pair: returns (side_a, side_b) as [(token, changed)]."""
    ta, tb = _toks(a), _toks(b)
    sa, sb = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb).get_opcodes():
        if tag == "equal":
            sa += [(t, False) for t in ta[i1:i2]]
            sb += [(t, False) for t in tb[j1:j2]]
        elif tag == "delete":
            sa += [(t, True) for t in ta[i1:i2]]
        elif tag == "insert":
            sb += [(t, True) for t in tb[j1:j2]]
        else:
            sa += [(t, True) for t in ta[i1:i2]]
            sb += [(t, True) for t in tb[j1:j2]]
    return sa, sb

def _hunk_entries(body, on, nn):
    """Render entries for one hunk: (kind, line_number, text, word_segments).
    Number of a removed line is from the old file, of added/context from the new file."""
    out, i = [], 0
    while i < len(body):
        l = body[i]
        if l.startswith(" "):
            out.append(("ctx", nn, l[1:], None)); on += 1; nn += 1; i += 1
        elif l.startswith("-"):
            j = i
            dels = []
            while j < len(body) and body[j].startswith("-"):
                dels.append(body[j][1:]); j += 1
            ins = []
            while j < len(body) and body[j].startswith("+"):
                ins.append(body[j][1:]); j += 1
            for k in range(max(len(dels), len(ins))):
                a_seg = b_seg = None
                if k < len(dels) and k < len(ins):
                    a_seg, b_seg = _word_pair(dels[k], ins[k])
                if k < len(dels):
                    out.append(("del", on, dels[k], a_seg)); on += 1
                if k < len(ins):
                    out.append(("ins", nn, ins[k], b_seg)); nn += 1
            i = j
        else:
            out.append(("ins", nn, l[1:], None)); nn += 1; i += 1
    return out

def diff_text(path, old, new, limit=None, show=True):
    """Claude Code-style diff: red = removed, green = added, line numbers on the left,
    word-level highlight inside changed pairs, `...` between hunks.
    show=False — only the header line (path +N −M)."""
    d = [l for l in difflib.unified_diff(str(old).splitlines(), str(new).splitlines(), lineterm="")
         if not l.startswith(("---", "+++"))]
    rem = sum(1 for l in d if l.startswith("-"))
    add = sum(1 for l in d if l.startswith("+"))
    t = Text()
    t.append(f"{path}  ", style="bold")
    # "+2 −1" instead of "Added 2 lines, removed 1 lines": shorter and no "1 lines"
    t.append(f"+{add}", style=ADD_C)
    t.append(f" −{rem}\n", style=DEL_C)
    if not show:
        return t
    entries, i, hunk = [], 0, 0
    while i < len(d):
        if not d[i].startswith("@@"):
            i += 1
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", d[i])
        on, nn = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        if hunk:
            entries.append(("gap", None, None, None))
        hunk += 1
        i += 1
        body = []
        while i < len(d) and not d[i].startswith("@@"):
            body.append(d[i]); i += 1
        entries += _hunk_entries(body, on, nn)
    total = sum(1 for e in entries if e[0] != "gap")
    width = max((len(str(n)) for k, n, _, _ in entries if n), default=2)
    width = max(width, 2)
    shown = 0
    for kind, num, text, seg in entries:
        if kind == "gap":
            t.append(" " * width + "  ...\n", style="grey50")
            continue
        if limit and shown >= limit:
            t.append(f"… {total - shown} more lines\n", style="grey50")
            break
        shown += 1
        t.append(f"{num:>{width}} ", style=NUM_STYLE[kind])
        base, word = LINE_STYLE[kind], WORD_STYLE[kind]
        t.append(SIGN[kind], style=base)
        if seg:
            for tok, ch in seg:
                t.append(tok, style=(word if ch and word else base))
        else:
            t.append(text, style=base)
        t.append("\n", style=base)
    return t


_RG_LINE = re.compile(r"(\d+)([:-])(.*)")


def _rg_grouped(lines, cap):
    """rg --heading output -> each file path once, then "N: match" / "N- context" lines.

    A path repeated on every line was a third of a grep result; the header keeps the absolute
    path the model needs for read_file."""
    out, shown = [], 0
    for line in lines:
        m = _RG_LINE.match(line)
        if not m:
            if line and line != "--":
                out.append(line)            # file header
            continue
        if shown >= cap:
            return "\n".join(out) + f"\n...[first {cap} matches, refine the query]"
        out.append(f"{m.group(1)}{m.group(2)} {m.group(3)[:240]}")
        shown += 1
    return "\n".join(out)


GREP_WALK_MAX = 2_000_000      # per file while walking a folder
GREP_NAMED_MAX = 500_000_000   # a file passed as the path itself


def tool_grep(pattern, path=".", glob=None, context=None, output_mode="content"):
    mode = output_mode or "content"
    if mode not in ("content", "files_with_matches", "count"):
        return "Error: output_mode must be content, files_with_matches or count"
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    root, out, seen = Path(path or "."), [], 0
    if not root.exists():
        return f"Error: path not found: {root}"
    if blocked(root, allow_tool_output_read=True, read=True):
        return deny(root)
    if context is None:
        is_code_search = (root.suffix.lower() in {".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs"}
                          or bool(glob and glob.lower() in {"*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.cs", "*.java", "*.go", "*.rs"}))
        context = 3 if mode == "content" and is_code_search else 0
    context = int(context or 0)
    cap = 100
    # ripgrep if it is on PATH: much faster, then pure python as fallback
    # Inside the allowed part of AppData (Local\Programs, Local\Temp) the "!**/AppData/**" glob would cut everything: python walks it there.
    rg = shutil.which("rg") if (not SETTINGS.get("protected_paths") and not _trusted_tool_output_file(root)
                                and "appdata" not in str(root).lower()) else None
    if rg:
        flags = {"content": ["-i", "--heading", "--line-number"],
                 "files_with_matches": ["-i", "--files-with-matches"],
                 "count": ["-i", "--count"]}[mode]
        cmd = [rg] + flags
        for g in RG_EXCLUDE:
            cmd += ["--glob", g]
        for g in name_patterns(glob) if glob else ():
            cmd += ["--glob", g]
        if context and mode == "content":
            cmd += ["-C", str(context)]
        cmd += [pattern, str(root)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30, text=True, encoding="utf-8")
            lines = r.stdout.splitlines()
            if lines and mode == "content":
                return _rg_grouped(lines, cap)
            if lines:
                preview = []
                for line in lines[:cap]:
                    # keep the matched source line as a short preview rather than a full dump
                    preview.append(line[:240])
                return "\n".join(preview) + (f"\n...[first {cap} matches, refine the query]" if len(lines) > cap else "")
            if r.returncode == 1:
                return "No matches."
        except Exception:
            pass  # ripgrep failed — fall back to pure python

    def files():
        if root.is_file():
            yield root
            return
        for d, ds, fs in os.walk(root):
            ds[:] = [x for x in ds if x not in SKIP_DIRS and not blocked(Path(d) / x, read=True)]
            for f in fs:
                if not blocked(Path(d) / f, allow_tool_output_read=True, read=True):
                    yield Path(d) / f

    # A file named explicitly is searched whatever its size; in a folder walk big files are skipped, but
    # named in the result: "No matches." for a 15 MB log that had 1234 hits sent the model off guessing.
    per, shown, skipped, stop = {}, 0, [], ""
    masks = name_patterns(glob) if glob else None
    started = time.monotonic()
    for f in files():
        if masks and not any(fnmatch.fnmatch(f.name, m) for m in masks):
            continue
        seen += 1
        # Stop the walk but keep what was found: the early return dropped every files_with_matches/count hit
        # (20k-file tree: 10.9 s and not one result).
        if seen > GREP_FILES_MAX:
            stop = f"more than {GREP_FILES_MAX} files"
            break
        if time.monotonic() - started > GREP_TIME_MAX:
            stop = f"{GREP_TIME_MAX} s limit"
            break
        try:
            size = f.stat().st_size
            if size > (GREP_NAMED_MAX if root.is_file() else GREP_WALK_MAX):
                skipped.append(f"{f} ({size // 1_000_000} MB)")
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if _bom_encoding(raw[:4]) == "utf-8" and b"\0" in raw[:2048]:
            continue
        lines = decode_text(raw)[0].splitlines()
        hits = [i for i, line in enumerate(lines, 1) if rx.search(line)]
        if not hits:
            continue
        per[str(f)] = len(hits)
        if mode != "content":
            continue
        filecap = len(hits) if glob else 5
        if not root.is_file():
            out.append(str(f))                  # file header once, as with rg --heading
        hitset, last = set(hits), 0
        for i in hits[:filecap]:
            lo, hi = max(last + 1, i - context), min(len(lines), i + context)    # overlapping context once
            for j in range(lo, hi + 1):
                out.append(f"{j}{':' if j in hitset else '-'} {lines[j - 1].strip()[:240]}")
                shown += 1
                if shown >= cap:
                    return "\n".join(out) + f"\n...[first {cap} matches, refine the query]"
            last = max(last, hi)
        if len(hits) > filecap:
            out.append(f"...{len(hits) - filecap} more matches in this file (pass glob to see all)")
    note = ""
    if skipped:
        note = (f"\n[skipped {len(skipped)} big file(s): {', '.join(skipped[:5])}"
                f"{' …' if len(skipped) > 5 else ''}. To search one, pass its path; for counts/stats use py_run]")
    if stop:
        note += (f"\n[search stopped after {seen - 1} files ({stop}): the results are partial. "
                 "Narrow path or glob to search the rest]")
    if not per:
        return ("No matches in the files searched." if stop else "No matches.") + note
    out = out if not note else out + [note.strip()]
    if mode == "count":
        shown = list(per.items())[:cap]
        return "\n".join(f"{p}: {n}" for p, n in shown) + (f"\n...[first {cap} files, refine the query]" if len(per) > cap else "") + note
    if mode == "files_with_matches":
        shown = list(per)[:cap]
        return "\n".join(shown) + (f"\n...[first {cap} files, refine the query]" if len(per) > cap else "") + note
    return "\n".join(out)


def name_patterns(mask):
    """'*.{md,json}' / '*.py,*.ts' -> ['*.md', '*.json'] / ['*.py', '*.ts']. fnmatch knows neither braces nor
    lists, so such a mask matched nothing and grep answered "No matches." (13 of 941 real greps; ripgrep is not
    installed everywhere). A comma inside braces belongs to the brace group."""
    parts, depth, cur = [], 0, ""
    for ch in str(mask or ""):
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (ch == "{") - (ch == "}")
        cur += ch
    parts.append(cur)
    out = []

    def expand(p):
        m = re.search(r"\{([^{}]*)\}", p)
        if not m:
            if p and p not in out:
                out.append(p)
            return
        for alt in m.group(1).split(","):
            expand(p[:m.start()] + alt.strip() + p[m.end():])

    for part in parts:
        expand(part.strip())
    return out or [str(mask or "")]


def _split_abs_glob(pattern):
    """C:/x/y/*.py -> ("C:/x/y", "*.py"): absolute pattern never matches a path relative to the root."""
    p = str(pattern or "").replace("\\", "/")
    if not re.match(r"^(?:[A-Za-z]:/|/)", p):
        return None
    parts = p.split("/")
    i = next((k for k, s in enumerate(parts) if any(ch in s for ch in "*?[")), len(parts) - 1)
    base = "/".join(parts[:i]) or "/"
    return (base + "/" if base.endswith(":") else base), "/".join(parts[i:])


def tool_glob(pattern, path=".", limit=100):
    """Return matching file paths only, keeping recursive listing out of the context."""
    split = _split_abs_glob(pattern)
    if split:
        path, pattern = split
    root = Path(path or ".")
    if not root.exists():
        return f"Error: path not found: {root}"
    if blocked(root, read=True):
        return deny(root)
    if root.is_file():
        return str(root) if fnmatch.fnmatch(root.name, pattern) or fnmatch.fnmatch(str(root), pattern) else "No matches."
    try:
        cap = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        cap = 100
    found = []
    # "**/" means zero or more folders: fnmatch needs a "/" for it, so "**/*" missed files in the root.
    patterns = {q for p in name_patterns(pattern) for q in (p, p.replace("**/", ""))}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not blocked(Path(directory) / d, read=True))
        for filename in sorted(files):
            item = Path(directory) / filename
            if blocked(item, read=True):
                continue
            rel = item.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(filename, p) for p in patterns):
                found.append(str(item))
                if len(found) >= cap:
                    return "\n".join(found) + "\n...[100-path limit reached; narrow the glob pattern]"
    # The model often looks up a named file with a glob ("slow.py") and reads it on the next turn: a single exact
    # file without wildcards is returned with its content right away — one model turn less.
    if len(found) == 1 and not any(ch in pattern.replace("**/", "") for ch in "*?["):
        if Path(found[0]).suffix.lower() in IMAGE_EXTS and not NO_VISION:
            # Only read_file attaches the picture; an inlined text stub made the model describe an image it never saw.
            return f"{found[0]}\n(exact match: an image file; call read_file {Path(found[0]).resolve()} to see the picture)"
        return f"{found[0]}\n(exact match; its content follows, no need to read it again)\n" + tool_read(found[0])
    return "\n".join(found) if found else "No matches."


def tool_write(path, content):
    if blocked(path):
        return deny(path)
    if not isinstance(content, str):
        return "Error: content must be a string"
    conflict = _conflict_error(content)
    if conflict:
        return conflict
    p = Path(path)
    try:
        data = p.read_bytes() if p.is_file() else b""
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    previous, enc = decode_text(data)
    if p.exists() and previous.replace("\r\n", "\n") == content.replace("\r\n", "\n"):
        return f"OK, but {path} already had exactly this content: nothing changed on disk."
    refused = _overwrite_error(p, previous, content) if p.is_file() else ""
    if refused:
        return refused
    blind = p.is_file() and _unread(p, previous)
    # Line endings: the existing file's own; a new file gets the platform's as before (CRLF on Windows).
    # write_text() translated every "\n", so an LF file turned CRLF and "\r\n" in content became "\r\r\n".
    eol = _eol(previous) or ("\r\n" if "\r\n" in content else os.linesep)
    body = content.replace("\r\n", "\n")
    body = body.replace("\n", eol) if eol != "\n" else body
    note = ""
    # A cp1251 file stays cp1251 while the new text fits; otherwise it becomes UTF-8 (said in the result).
    keep = enc if enc and enc not in ("utf-8", "utf-8-sig") else None
    out, error = encode_text(path, body, keep) if keep else (None, "")
    if out is None:
        out = body.encode("utf-8-sig" if enc == "utf-8-sig" else "utf-8")
        if keep:
            note = f"\n[the file was {keep}; the new content does not fit {keep}, so it was written as UTF-8]"
    # Raw bytes: read_text() dropped CRLF and non-UTF-8 bytes, so /undo did not give the original file back.
    undo.snap(str(p), data if p.is_file() else "", new=not p.exists())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(out)
    mark_seen(p)
    if blind:
        note += (f"\n[it replaced an existing file you had not read; its previous content (kept in a checkpoint, "
                 f"/undo restores it):]\n{previous.rstrip()}")
    return _write_result(path, f"OK, wrote {content.count(chr(10)) + 1} lines to {shown(path)}{note}")


@_resolve_path_arg
def tool_apply_diff(path, diff_text):
    """Apply a standard unified diff to one file, refusing fuzzy/ambiguous matches."""
    if blocked(path):
        return deny(path)
    if not isinstance(diff_text, str) or not diff_text.strip():
        return "Error: diff must be a non-empty unified diff string"
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        return f"Error: cannot read {path}: {e}"
    raw, enc = decode_text(data)
    if enc is None:
        return not_text_error(path)
    crlf = _eol(raw) == "\r\n"
    original_lines = raw.replace("\r\n", "\n").splitlines()
    had_final_newline = raw.endswith(("\n", "\r"))
    diff_lines = diff_text.replace("\r\n", "\n").splitlines()
    hunks = []
    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m and re.match(r"^@@ *[-+\d]", line):
                return f"Error: malformed unified diff hunk header: {line[:120]}"
            # Bare "@@" / "@@ def f(): @@" (Aider format, no line numbers): the location is found by context.
            old_start = max(0, int(m.group(1)) - 1) if m else None
            i += 1
            old_chunk, new_chunk = [], []
            while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
                item = diff_lines[i]
                if item.startswith(("--- ", "+++ ")):
                    break
                if item.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                # Some local models omit the single leading space on an empty context line.
                # Treat that empty line as context instead of rejecting the entire patch.
                if not item:
                    old_chunk.append("")
                    new_chunk.append("")
                    i += 1
                    continue
                if item[0] not in " +-":
                    return f"Error: invalid unified diff line: {item[:120]}"
                if item[0] in " -":
                    old_chunk.append(item[1:])
                if item[0] in " +":
                    new_chunk.append(item[1:])
                i += 1
            hunks.append((old_start, old_chunk, new_chunk))
            continue
        i += 1
    if not hunks:
        return "Error: no unified diff hunks found; provide @@ hunk headers."

    lines = list(original_lines)
    offset, note = 0, ""
    for old_start, old_chunk, new_chunk in hunks:
        expected = None if old_start is None else max(0, min(len(lines), old_start + offset))
        if expected is not None and lines[expected:expected + len(old_chunk)] == old_chunk:
            match = expected
        else:
            # Only relocate a stale hunk if its full old-context occurs at exactly one location.
            matches = [pos for pos in range(0, max(0, len(lines) - len(old_chunk) + 1))
                       if lines[pos:pos + len(old_chunk)] == old_chunk] if old_chunk else []
            if not matches and old_chunk and (fixed := _tolerant_hunk(lines, old_chunk, new_chunk)):
                # Same levels as edit_file (whitespace, indentation, typography), only at one place:
                # 7 of 38 real apply_diff calls failed on context that differed only like that.
                matches, old_chunk, new_chunk, note = fixed
            if not matches:
                near = "" if old_start is None else f" near old line {old_start + 1}"
                return (f"Error: unified diff context did not match {path}{near}; reread the file and regenerate the diff."
                        + _fuzzy_hint("\n".join(old_chunk), "\n".join(lines)))
            if len(matches) > 1:
                return f"Error: unified diff context is ambiguous in {path}; include more neighbouring context."
            match = matches[0]
        # A repeated snippet elsewhere is not ambiguous when the hunk matches exactly at its
        # declared line. Rejecting it caused valid model-generated patches to be discarded.
        lines[match:match + len(old_chunk)] = new_chunk
        offset += len(new_chunk) - len(old_chunk)

    updated = "\n".join(lines) + ("\n" if had_final_newline else "")
    if lines == original_lines:
        return NO_CHANGE.format(path=path, what="the diff adds and removes the same lines")
    conflict = _conflict_error(updated)
    if conflict:
        return conflict
    if p.suffix.lower() == ".py":
        try:
            ast.parse(updated, filename=str(p))
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return f"Error: dry-run Python validation failed at {where}: {exc.msg}. File was not changed."
    out, error = encode_text(path, updated.replace("\n", "\r\n") if crlf else updated, enc)
    if error:
        return error
    undo.snap(str(p), data)
    p.write_bytes(out)
    return clip(_write_result(path, f"OK, applied unified diff to {shown(path)} ({len(hunks)} hunk(s)).{note}"), 5000)


def _tolerant_hunk(lines, old_chunk, new_chunk):
    """A hunk whose context matches only up to whitespace/typography -> ([pos], old, new, note), else None."""
    fixed = tolerant_match("\n".join(lines), "\n".join(old_chunk), "\n".join(new_chunk))
    if not fixed:
        return None
    exact, new, note = fixed
    old = exact.split("\n")
    hits = [i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
    return (hits[:1], old, new.split("\n") if new else [], note) if len(hits) == 1 else None
