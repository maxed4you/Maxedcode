"""Small helpers: tool schema, tokens, clipping, clipboard, prompt history."""
from contextlib import closing, suppress
from pathlib import Path
import ast
import base64
import ctypes
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
import urllib.request
from . import config
from .config import BASE_URL, HOME, MAX_OUT, SESS, SYSTEM_AS_USER, MODEL, model_price, usage_cost, cost_text, NO_VISION

def T(name, desc, props, req=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(props) if req is None else req}}}
def s(d):
    return {"type": "string", "description": d}
def server_ctx():
    """Context size the server really allocated (Strata /health, llama.cpp /props, LM Studio, Ollama, vLLM ...).
    None if it does not respond or does not say."""
    from .servers import context
    return context(config.BASE_URL, config.API_KEY, config.SERVER_KIND, None if config.MODEL == "local" else config.MODEL)
def save_full(x):
    """Write the whole tool output to HOME/outputs and return its path (None if it does not fit)."""
    x = x or ""
    f = HOME / "outputs" / f"{int(time.time() * 1000)}.txt"
    with suppress(OSError):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(x, encoding="utf-8")
        return f
    return None


# Terminal escapes (CSI colors, OSC titles/links, lone ESC codes) and other C0 controls except \t \n \r.
# The TUI sets FORCE_COLOR, so child programs (Python 3.13+ tracebacks, pytest, git) print colors into
# the pipe; raw ESC codes then reach the terminal past Textual and break card frames and the scrollbar.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]?|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(x):
    """Plain text without terminal escape sequences and control characters."""
    return ANSI.sub("", x) if x else x


def clip(x, n=MAX_OUT):
    x = x or ""
    if len(x) <= n:
        return x
    f = HOME / "outputs" / f"{int(time.time() * 1000)}.txt"
    with suppress(OSError):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(x, encoding="utf-8")
        return x[:n] + f"\n...[truncated, {len(x)} characters total; full output saved to {f}, read_file it]"
    return x[:n] + f"\n...[truncated, {len(x)} characters total]"


def tool_output_dir():
    """Directory for complete tool output kept out of the model context (pruned by housekeeping())."""
    return HOME / "outputs"


def housekeeping(days=7):
    """Delete agent leftovers older than `days`: saved tool outputs, background-task logs, stale checkpoints,
    and the old %TEMP%\\opencode output folder. Nothing in the user's projects is touched."""
    from .tools import undo
    cutoff = time.time() - days * 86400
    old_tmp = Path(tempfile.gettempdir()) / "opencode"
    for d, pattern in ((HOME / "outputs", "*"), (HOME / "bg", "*.log"), (old_tmp, "tool-output-*.txt")):
        with suppress(OSError):
            for f in d.glob(pattern):
                with suppress(OSError):
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
    with suppress(OSError):
        old_tmp.rmdir()   # only when empty: a real opencode install may share the folder
    undo.prune(keep={undo.sid})


def normalize_tool_output(value, limit=None):
    """Keep large tool results on disk and pass only a stable, useful preview to the model.

    The complete output is saved once; the returned marker gives the model a usable path
    for targeted follow-up reads without copying the whole result into every API request.
    """
    text = value if isinstance(value, str) else str(value)
    try:
        maximum = max(2000, int(limit or os.environ.get("AGENT_TOOL_OUTPUT_LIMIT", "12000")))
    except (TypeError, ValueError):
        maximum = 12000
    if len(text) <= maximum:
        return text
    path = None
    try:
        d = tool_output_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"tool-output-{int(time.time() * 1000)}-{os.getpid()}.txt"
        # Avoid accidental overwrite if two calls complete in the same millisecond.
        suffix = 0
        while path.exists():
            suffix += 1
            path = d / f"tool-output-{int(time.time() * 1000)}-{os.getpid()}-{suffix}.txt"
        path.write_text(text, encoding="utf-8", errors="replace")
    except OSError:
        path = None
    preview_size = min(7800, maximum - 1200)
    head = text[:preview_size]
    tail_size = min(900, max(0, maximum - len(head) - 500))
    tail = text[-tail_size:] if tail_size else ""
    marker = (f"\n…[output truncated: {len(text):,} characters; complete output saved to {path}. "
              f"Use read_file on that path with start_line/end_line or grep for a specific excerpt.]"
              if path else f"\n…[output truncated: {len(text):,} characters; temporary storage failed]")
    if tail:
        return head + "\n…[middle omitted]…\n" + tail + marker
    return head + marker
def repair_json(s):
    """Parse tool-call JSON from the model, fixing typical local-model breakage: trailing commas,
    raw control chars, truncated/unbalanced brackets, python-style dicts. None if unfixable."""
    if not isinstance(s, (str, bytes, bytearray)):     # arguments some servers send as an object, or null
        return s if isinstance(s, dict) else None
    with suppress(Exception):
        return json.loads(s)
    t = re.sub(r",\s*([}\]])", r"\1", s)
    out, stack, in_str, esc = [], [], False, False
    for ch in t:
        if esc:
            esc = False
            out.append(ch)
            continue
        if ch == "\\" and in_str:
            esc = True
            out.append(ch)
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            # raw newline/tab/control char inside a string value: escape it
            out.append("\\n" if ch == "\n" else "\\t" if ch == "\t" else " " if ord(ch) < 32 else ch)
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and (stack[-1] == "{" if ch == "}" else stack[-1] == "["):
                stack.pop()
        out.append(ch)
    if in_str:
        out.append('"')
    out.extend("}" if b == "{" else "]" for b in reversed(stack))
    t = "".join(out)
    with suppress(Exception):
        return json.loads(t)
    with suppress(Exception), warnings.catch_warnings():
        warnings.simplefilter("ignore")      # "C:\path" in a value: a SyntaxWarning on stderr for every call
        v = ast.literal_eval(t)
        # only what JSON can carry: {"a": {"b"}} is a Python set, and the canonical json.dumps of it crashed the run
        return json.loads(json.dumps(v)) if isinstance(v, dict) else None
    return None
def tok(text):
    """Token estimate for any language: ASCII ≈ 4 characters per token, other alphabets (Cyrillic, Greek,
    Arabic, Hebrew, accented Latin) ≈ 2, CJK / Thai / Devanagari and the like ≈ 1.

    The UTF-8 length tells the groups apart without a Python loop: 1, 2 and 3+ bytes per character."""
    n = len(text)
    ascii_n = len(text.encode("ascii", "ignore"))
    wide = max(0, len(text.encode("utf-8", "surrogatepass")) - ascii_n - 2 * (n - ascii_n))
    return ascii_n // 4 + max(0, n - ascii_n - wide) // 2 + wide
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode", ".kilo", "site-packages"}
TEXT_EXT = {".py", ".md", ".txt", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml", ".js", ".ts", ".html", ".css",
            ".bat", ".cmd", ".ps1", ".sh", ".xml", ".sql", ".php", ".java", ".cs", ".go", ".rs", ".c", ".h", ".cpp", ".bak",
            ".csv", ".tsv", ".log", ""}
def split_think(raw):
    th = []
    vis = re.sub(r"<think>([\s\S]*?)(</think>|$)", lambda m: th.append(m.group(1)) or "", raw)
    return "".join(th), vis
# A quantized model's reasoning can collapse into "Hmm, hmm. Hmm." until the thinking budget runs out.
# A run is LOOP_MIN_WORDS+ consecutive words drawn from at most 3 distinct ones; normal prose never
# does that, but data does ("00 ff 00 ff", "[0,0,0]", "(r-1,c), (r,c+1)"), so a run counts only when
# at least a third of its words are real words: letters, 2+ of them, not hex.
_LOOP_WORD = re.compile(r"[^\W_]+")
_HEXISH = re.compile(r"(?:0x)?[0-9a-f]+", re.I)
LOOP_MIN_WORDS = 12
THINK_LOOP_WORDS = 40   # while streaming: enough to be sure, early enough to waste ~100 tokens


def _loop_runs(text, min_words=LOOP_MIN_WORDS):
    """(start, keep, end) character spans of repetition runs; keep is the end of the run's first words."""
    words = [(m.group().lower(), m.start(), m.end()) for m in _LOOP_WORD.finditer(text)]
    runs, i = [], 0
    while i < len(words):
        vocab, j = set(), i
        while j < len(words) and (words[j][0] in vocab or len(vocab) < 3):
            vocab.add(words[j][0])
            j += 1
        if j - i < min_words:
            i += 1
            continue
        real = sum(1 for w, _, _ in words[i:j] if w.isalpha() and len(w) > 1 and not _HEXISH.fullmatch(w))
        if real * 3 >= j - i:
            runs.append((words[i][1], words[i + 2][2], words[j - 1][2]))
        i = j
    return runs


_SENTENCE = re.compile(r"[.!?\n]+")
PHRASE_LOOP_SENTENCES = 12   # the last N sentences ...
PHRASE_LOOP_DISTINCT = 4     # ... drawn from at most this many different ones


def _phrase_loop(text):
    """A cycle of whole phrases: "Enough! Let me consider Argan? no." over and over (live X5). The word
    detector needs a run from at most 3 distinct words, so a 6-word phrase repeated slipped through."""
    parts = [" ".join(_LOOP_WORD.findall(p.lower())) for p in _SENTENCE.split(text or "")]
    tail = [p for p in parts if p][-PHRASE_LOOP_SENTENCES:]
    if len(tail) < PHRASE_LOOP_SENTENCES or len(set(tail)) > PHRASE_LOOP_DISTINCT:
        return False
    # Brainstorming ("Hmm. What about handball? Hmm. What about rugby?") has few distinct short sentences
    # too: a loop needs one multi-word phrase coming back again and again, not just a repeated "Hmm".
    long_repeats = [p for p in set(tail) if len(p.split()) >= 3 and tail.count(p) >= 4]
    return bool(long_repeats) and sum(len(p.split()) for p in tail) >= 24


def think_loop(reasoning):
    """True when the tail of a streaming reasoning is stuck repeating itself."""
    tail = (reasoning or "")[-1500:]
    return bool(_loop_runs(tail[-800:], THINK_LOOP_WORDS)) or _phrase_loop(tail)


def squash_loops(text):
    """Cut every repetition run down to its first words. Sent back as history, a looped reasoning
    teaches the model to loop again: each next step repeated more, until every step hit the budget."""
    if not text:
        return text
    for start, keep, end in reversed(_loop_runs(text)):
        text = text[:keep] + text[end:]
    return text
_ARG_PATH = re.compile(r'"(?:path|file_path)"\s*:\s*"((?:[^"\\]|\\.)+)"')


def track_tool_call(c, piece):
    """Count what a streaming tool call has written so far without rescanning its whole arguments:
    a 500-line write_file arrives in thousands of chunks."""
    c["_lines"] = c.get("_lines", 0) + piece.count("\\n")
    if "_target" not in c and len(c["args"]) < 4000:
        m = _ARG_PATH.search(c["args"])
        if m:
            c["_target"] = re.split(r"[\\/]+", m.group(1).replace("\\\\", "\\"))[-1]


def tool_call_status(c, count=1, titles=None):
    """Status line while the model streams a tool call: "Composing: Writing file → app.py · 120 lines (call 2)…"."""
    name = (titles or {}).get(c.get("name"), c.get("name")) or "tool call"
    status = f"Composing: {name}"
    if c.get("_target"):
        status += f" → {c['_target']}"
    if c.get("_lines"):
        status += f" · {c['_lines']} lines"
    return status + (f" (call {count})" if count > 1 else "") + "…"
def ago(ts):
    d = max(0, time.time() - ts)
    return "just now" if d < 60 else f"{int(d // 60)} min ago" if d < 3600 else \
        f"{int(d // 3600)} h ago" if d < 86400 else f"{int(d // 86400)} d ago"
IMG = re.compile(r'"([^"]+\.(?:png|jpe?g|webp|gif))"|(\S+\.(?:png|jpe?g|webp|gif))', re.I)
def build_content(wrapped, text):
    """If the text contains image paths, attach them to the message."""
    parts = []
    for m in IMG.finditer(text):
        p = Path((m.group(1) or m.group(2)).lstrip("@"))
        if p.is_file():
            mime = mimetypes.guess_type(p.name)[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return [{"type": "text", "text": wrapped}] + parts if parts else wrapped
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
IMAGE_SIDE = 1600                      # a phone photo is scaled down: same reading, a fraction of the tokens
# Without the attached picture (code_mode, a result that was not attached) the stub must not look like the content.
IMAGE_NOTE = " (no picture in this text: only a direct read_file call shows the image itself; do not guess its content)"
def image_part(path):
    """An image file as an image_url part; big pictures are scaled to IMAGE_SIDE px when PIL is there."""
    p = Path(path)
    data, mime = p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/png"
    with suppress(Exception):
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) > IMAGE_SIDE or len(data) > 1_500_000 or mime not in ("image/png", "image/jpeg", "image/webp"):
            im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85)
            data, mime = buf.getvalue(), "image/jpeg"
    if len(data) > 4_000_000:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
def tool_content(name, args, out):
    """A read_file result for an image carries the picture itself, so a vision model looks at it
    instead of hunting for an OCR tool. Everything else stays a plain string."""
    if name not in ("read_file", "browser") or NO_VISION or not isinstance(out, str) or not out.startswith("[image "):
        return out
    part = None
    # browser screenshot (tools/browser.py): the file is named in the result, not in the arguments
    path = (args or {}).get("path") if name == "read_file" else out[len("[image "):out.find(": ")]
    with suppress(Exception):
        part = image_part(path or "")
    text = out.replace(IMAGE_NOTE, "") + " The picture is attached below: look at it directly."
    return [{"type": "text", "text": text}, part] if part else out
def dur(d):
    d = max(0, d)
    if d < 60:
        return f"{d:.1f} s" if d < 10 else f"{int(d)} s"
    if d < 3600:
        return f"{int(d // 60)} min {int(d % 60)} s"
    if d < 86400:
        return f"{int(d // 3600)} h {int(d % 3600 // 60)} min"
    return f"{int(d // 86400)} d {int(d % 86400 // 3600)} h"
def unwrap(t):
    if isinstance(t, list):
        t = next((p["text"] for p in t if p["type"] == "text"), "")
    return re.sub(r"</?(user_input|mode_notice)[^>]*>", "", t).strip()
def shift_down():
    """Without the kitty protocol the terminal sends Shift+Enter as a plain Enter, and they cannot be
    distinguished by bytes, so we check the physical state of the key."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except OSError:
        return False
def to_clip(text):
    """Copies text to the clipboard."""
    if not text:
        return False
    if sys.platform != "win32":       # pbcopy / wl-copy / xclip / xsel; the TUI sends OSC 52 as well
        from . import plat
        return plat.copy(text)
    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), capture_output=True)
        return True
    except OSError:
        return False
def sent_reasoning(m):
    """The reasoning of an assistant message that wire() sends back: all of it with KEEP_REASONING, else none.
    Nothing is cut or dropped: on LCB, dropping long thinks turned a pass (176 s) into a wrong answer (413 s)."""
    return (m.get("reasoning_content") or "") if config.KEEP_REASONING else ""


UI_ONLY = {"turn_time", "file_changes", "edit_changes"}   # the "⏱ total …" line of a turn; diffs of files a shell/script changed


def wire(msgs, vision=None):
    """Messages as they go to the server. With SYSTEM_AS_USER every system message after the first
    becomes a user message (for chat templates that allow only one leading system message).

    Two request-level guards, both cache-safe because they only change history bytes in a stable way:
    the oldest images are dropped only when the media total passes TRIGGER (down to TARGET), so the
    cut does not move on every request; and media is replaced by a fixed stub for models that cannot
    read it at all.
    """
    # Late import: compact imports util (tok/split_think), so a module-level import here would cycle.
    from . import compact
    msgs, _ = compact.bound_images(msgs, 0)
    if vision is None:
        vision = not NO_VISION
    msgs, _ = compact.unsupported_parts(msgs, 0, vision)
    out = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and not config.KEEP_REASONING:
            # Reasoning is rendered by the template only for the current turn; sending it makes the
            # same message look different in the next turn and breaks the prefix cache. The message
            # is rendered the same way in every request instead.
            if "reasoning_content" in m or "reasoning" in m:
                m = {k: v for k, v in m.items() if k not in ("reasoning_content", "reasoning")}
            c = m.get("content")
            if isinstance(c, str) and "</think>" in c:
                m = {**m, "content": c.rsplit("</think>", 1)[1].lstrip("\n")}
            if not (m.get("content") or "").strip() and not m.get("tool_calls"):
                continue
        # With KEEP_REASONING the reasoning goes back exactly as the model wrote it: the same bytes every request
        # keep the server's cache of the reply valid, and the model keeps every note it made (a squashed copy of a
        # looped think made the server re-read the whole step: 9.4k tokens, 98 s).
        if SYSTEM_AS_USER and i and m.get("role") == "system":
            m = {**m, "role": "user"}
        if UI_ONLY.intersection(m):   # what the UI saved for /history: never part of the request
            m = {k: v for k, v in m.items() if k not in UI_ONLY}
        out.append(m)
    return _utf8_safe(out)


def _utf8_safe(msgs):
    """A lone UTF-16 surrogate (an unpaired escape in a model reply, or a tool result that echoes it) cannot be
    sent as UTF-8: the SDK failed on every request of the run. Replaced by "?" in the request only, the same way
    each time (cache-safe); the history keeps the text as it came."""
    with suppress(TypeError, ValueError):
        text = json.dumps(msgs, ensure_ascii=False)
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return json.loads(text.encode("utf-8", "replace").decode("utf-8"))
    return msgs

LATE_OPEN, LATE_CLOSE = "<injected>", "</injected>"


def attach_late(msgs, items):
    """Deliver side-channel context (folder docs, path rules, background results) WITHOUT adding a
    user/system message in the middle of a turn.

    Chat templates (Qwen & co.) render an assistant message differently depending on whether it comes
    after the LAST user message. Any user-role message injected between two assistant steps of one turn
    moves that boundary, the whole turn is rendered differently and the server re-reads it from the
    start. The text goes into the freshest tool result instead (it has not been sent yet), so the
    history stays append-only. Without a pending tool result it becomes an ordinary user message.
    """
    items = [str(x).strip() for x in items if str(x or "").strip()]
    if not items:
        return False
    text = "\n\n".join(f"{LATE_OPEN}\n{x}\n{LATE_CLOSE}" for x in items)
    last = msgs[-1] if msgs else None
    if last and last.get("role") == "tool" and isinstance(last.get("content"), str):
        last["content"] += "\n\n" + text
    elif last and last.get("role") == "tool" and isinstance(last.get("content"), list) and last["content"] \
            and last["content"][0].get("type") == "text":
        last["content"][0] = {**last["content"][0], "text": last["content"][0]["text"] + "\n\n" + text}   # image result
    else:
        msgs.append({"role": "user", "content": text})
    return True


def hash_cwd(cwd=None):
    return hashlib.sha1(str(cwd or os.getcwd()).lower().encode("utf-8")).hexdigest()[:12]

def sess_dir(cwd=None):
    """Sessions live per cwd, so /resume does not mix projects."""
    d = SESS / hash_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d

def session_file(sid, cwd=None):
    return sess_dir(cwd) / f"{sid}.json"

def session_path(sid, cwd=None):
    """Session file: first this cwd's folder, then any other — otherwise /history can't
    open a session saved in another project."""
    f = session_file(sid, cwd)
    return f if f.exists() else next(iter(SESS.glob(f"*/{sid}.json")), f)

def frontmatter(raw):
    """Frontmatter-lite (key: value, plus '- item' lists) → (dict, body). Used by memory rules,
    skills, commands and agent definitions."""
    if not raw.startswith("---"):
        return {}, raw
    head, _, body = raw[3:].partition("---")
    fm, lst = {}, None
    for line in head.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith("- ") and lst:
            fm[lst].append(line.strip()[2:].strip('"'))
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        if not k:
            continue
        v = v.strip().strip('"')
        if v:
            fm[k], lst = v, None
        else:
            fm[k], lst = [], k
    return fm, body.strip()

def cached_tokens(usage):
    """How much of the prompt the prefix cache served (the server does not always report it)."""
    try:
        return int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    except Exception:
        return 0

def cache_stats():
    """GET /metrics of the engine (Strata/vLLM): prefix cache queries and hits → (q, h) or (None, None)."""
    try:
        root = config.BASE_URL[:-3] if config.BASE_URL.endswith("/v1") else config.BASE_URL
        r = urllib.request.urlopen(root.rstrip("/") + "/metrics", timeout=5)
        g = {}
        for line in r.read().decode("utf-8", "replace").splitlines():
            if line.startswith("vllm:prefix_cache_"):
                g[line.split()[0]] = float(line.split()[-1])
        q, h = g.get("vllm:prefix_cache_queries_total"), g.get("vllm:prefix_cache_hits_total")
        return (q, h) if q is not None and h is not None else (None, None)
    except Exception:
        return (None, None)

_SERVICE_MSG = re.compile(r"<(env|repo_map|direct_shell|compact|pending_work|btw|finish_tool_feedback|hook)\b")

_USER_INPUT = re.compile(r"<user_input\b[^>]*>([\s\S]*?)(?:</user_input>|$)")
# what the browser panel (chrome/host.py) puts before the person's words: its rules and the current tab
_PANEL_BLOCKS = re.compile(r"^\s*(?:<(browser_panel|current_tab)>[\s\S]*?</\1>\s*)+")
_SERVICE_TAG = re.compile(r"^\s*<([a-z][a-z_]*)\b[^>]*>")
ATTACH_MARK = "\n\nAttached file context:\n"   # expand_attachments() appends the files after it


def visible_user_text(content):
    """What the person typed, as the chat showed it: no <env>, <repo_map>, <edit_strategy>, <mode_notice>,
    attached file bodies or other service blocks. None for a purely service message (hidden in the chat)."""
    if isinstance(content, list):
        content = "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    text = _PANEL_BLOCKS.sub("", str(content or ""), count=1)
    m = _USER_INPUT.search(text)
    if m:
        text = _PANEL_BLOCKS.sub("", m.group(1), count=1)
    else:
        tag = _SERVICE_TAG.match(text)
        if tag and tag.group(1) == "direct_shell":
            cmd = re.search(r'command="([^"]*)"', text)
            return "! " + (cmd.group(1) if cmd else "")
        if tag:
            return None
    text = text.split(ATTACH_MARK, 1)[0].strip()
    return text or None


def session_title(msgs, limit=60):
    """Session title — the first real user message. Service user messages
    (<env>, <repo_map>, <direct_shell>, <compact>…) and images are skipped."""
    for m in msgs or []:
        if m.get("role") != "user":
            continue
        c = re.sub(r"\s+", " ", visible_user_text(m.get("content")) or "").strip()
        if c and not c.startswith("! ") and not _SERVICE_MSG.match(c):
            return c[:limit]
    return ""


CLOSED_CALL = "Interrupted: the session was closed before this call finished; nothing is known about its result."


def close_open_calls(msgs):
    """Copy of a history where every tool call has a result. Saved mid-tool (window closed, quit), the
    history would have a call without its result, and the API rejects the resumed session with HTTP 400."""
    out, i = [], 0
    msgs = list(msgs or [])
    while i < len(msgs):
        m = msgs[i]
        out.append(m)
        i += 1
        ids = [str(c.get("id") or "") for c in (m.get("tool_calls") or []) if isinstance(c, dict)] \
            if isinstance(m, dict) and m.get("role") == "assistant" else []
        if not ids:
            continue
        while i < len(msgs) and isinstance(msgs[i], dict) and msgs[i].get("role") == "tool":
            out.append(msgs[i])
            with suppress(ValueError):
                ids.remove(str(msgs[i].get("tool_call_id") or ""))
            i += 1
        out += [{"role": "tool", "tool_call_id": cid, "content": CLOSED_CALL} for cid in ids if cid]
    return out


def has_dialogue(msgs):
    """True when a saved session has anything the person said: an empty chat is not worth listing."""
    return any(m.get("role") == "user" and visible_user_text(m.get("content"))
               for m in msgs or [] if isinstance(m, dict))

def write_json(path, data):
    """Write JSON atomically: a crash or a closed window mid-write must not leave a broken session file
    (list_sessions skips unreadable files, so the whole session would look lost)."""
    f = Path(path)
    tmp = f.with_name(f"{f.name}.{time.time_ns()}.tmp")
    try:
        text = json.dumps(data, ensure_ascii=False)
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:      # a lone surrogate (an unpaired escape in a model reply): kept escaped
            raw = json.dumps(data).encode("utf-8")
        tmp.write_bytes(raw)
        tmp.replace(f)
    finally:
        with suppress(OSError):
            tmp.unlink()

def clean_title(title):
    """The saved title, if it is real: old sessions store "(untitled)" or the start of <env>."""
    t = str(title or "").strip()
    return "" if t == "(untitled)" or _SERVICE_MSG.match(t) else t

def delete_session(path):
    """Delete a session file together with its child subagent sessions and search entries → deleted ids."""
    f = Path(path)
    targets = [f]
    for g in f.parent.glob("*.json"):
        with suppress(Exception):
            if g != f and str(json.loads(g.read_text(encoding="utf-8")).get("parent_sid") or "") == f.stem:
                targets.append(g)
    gone = []
    for g in targets:
        with suppress(OSError):
            g.unlink()
            gone.append(g.stem)
    if gone and SESSION_INDEX.exists():
        with suppress(Exception):
            with _index_db() as db, db:
                db.executemany("DELETE FROM session_search WHERE session_id=? AND (cwd_hash=? OR file_path=?)",
                               [(g.stem, f.parent.name, str(g)) for g in targets if g.stem in gone])
    return gone

# The desktop window's chats ("app-<time>", and "session-<uuid>" from the old DeepSeek Harness window): they live
# with the terminal's sessions but are listed apart, the window shows its own and the CLI its own.
WINDOW_PREFIXES = ("app-", "session-")


def list_sessions(cwd=None, all_cwds=False, window=False):
    """Sessions of this cwd, or of all projects (all_cwds — what /history shows); window=True: the window's
    chats instead of the terminal's (WINDOW_PREFIXES).
    project — the project folder name, here — whether it is the current project (old sessions don't store cwd,
    then the folder hash is shown)."""
    here = sess_dir(cwd)
    dirs = [here] if not all_cwds else [d for d in sorted(SESS.glob("*")) if d.is_dir()]
    out = []
    for d in dirs:
        for f in d.glob("*.json"):
            if f.stem.startswith(WINDOW_PREFIXES) != window:
                continue
            with suppress(Exception):
                x = json.loads(f.read_text(encoding="utf-8"))
                c = str(x.get("cwd") or "")
                # messages queued in it and never sent still count: the session is listed with them
                queued = [str(q) for q in x.get("queued") or [] if str(q).strip()]
                out.append({"id": x.get("id") or f.stem,
                            "title": clean_title(x.get("title")) or x.get("name") or session_title(x.get("msgs"))
                                     or re.sub(r"\s+", " ", queued[0] if queued else "").strip()[:60],
                            "ts": x.get("ts") or 0, "cwd": c, "path": f, "here": d == here,
                            "project": Path(c).name if c else d.name[:8],
                            "empty": not queued and not has_dialogue((x.get("archive") or []) + (x.get("msgs") or []))})
    return sorted(out, key=lambda x: -x["ts"])

def last_session(cwd=None):
    """The latest session with a real conversation (an empty chat opened and closed is skipped)."""
    return next((x["id"] for x in list_sessions(cwd) if not x["empty"]), None)

SESSION_INDEX = HOME / "sessions_index.db"

def _index_db():
    """Session index connection that is also closed: sqlite3's own `with` only commits, and an open
    file stays locked on Windows."""
    return closing(sqlite3.connect(SESSION_INDEX, timeout=5))

def _session_text(message):
    """Flatten a stored message into searchable plain text."""
    parts = []
    content = message.get("content") or ""
    if isinstance(content, list):
        parts.extend(str(x.get("text") or "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    elif isinstance(content, str):
        parts.append(content)
    for key in ("reasoning_content",):
        if message.get(key):
            parts.append(str(message[key]))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.extend((str(fn.get("name") or ""), str(fn.get("arguments") or "")))
    return "\n".join(x for x in parts if x).strip()

def index_session(msgs, sid, cwd=None, path=None, ts=None):
    """Upsert the messages of one session into FTS5; never scans or rebuilds the session catalog."""
    f = Path(path) if path else session_file(sid, cwd)
    stamp = float(ts or time.time())
    cwd_key = hash_cwd(cwd)
    records = []
    for n, message in enumerate(msgs or [], 1):
        role = str(message.get("role") or "unknown")
        if role == "system":
            continue
        text = _session_text(message)
        if text:
            records.append((str(sid), stamp, role, text, cwd_key, n, str(f)))
    with suppress(Exception):
        SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
        with _index_db() as db, db:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS session_search USING fts5(session_id UNINDEXED, ts UNINDEXED, role UNINDEXED, text, cwd_hash UNINDEXED, msg_no UNINDEXED, file_path UNINDEXED, tokenize='unicode61')")
            db.execute("DELETE FROM session_search WHERE session_id=? AND cwd_hash=?", (str(sid), cwd_key))
            db.executemany("INSERT INTO session_search(session_id,ts,role,text,cwd_hash,msg_no,file_path) VALUES(?,?,?,?,?,?,?)", records)

def search_sessions(query, cwd=None, limit=30):
    """Search the current cwd's saved sessions and return session/message hits with snippets."""
    terms = re.findall(r"[\w]+", str(query or ""), flags=re.UNICODE)
    if not terms or not SESSION_INDEX.exists():
        return []
    match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    try:
        with _index_db() as db, db:
            db.row_factory = sqlite3.Row
            # Older index files may exist without the latest auxiliary columns; recreate is not attempted
            # because this index is disposable and should never make session saving fail.
            rows = db.execute("SELECT session_id, ts, role, text, msg_no, file_path, "
                              "snippet(session_search,3,'‹','›',' … ',18) AS snippet "
                              "FROM session_search WHERE session_search MATCH ? AND cwd_hash=? "
                              "ORDER BY rank LIMIT ?", (match, hash_cwd(cwd), max(1, int(limit)))).fetchall()
        return [dict(r) for r in rows if not str(r["session_id"]).startswith(WINDOW_PREFIXES)]   # the window's chats stay in the window
    except (sqlite3.Error, ValueError):
        return []

_EXTRAS = {}      # session file -> its fields other than the ones save_session writes, read once per process


def _session_extras(f):
    """The other fields of an existing session file (a headless --continue of a TUI session dropped them)."""
    key = str(f)
    if key not in _EXTRAS:
        data = {}
        with suppress(OSError, ValueError):
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        _EXTRAS[key] = {k: v for k, v in (data.items() if isinstance(data, dict) else ())
                        if k not in ("id", "title", "name", "ts", "cwd", "msgs")}
    return _EXTRAS[key]


def save_session(msgs, sid, name="", cwd=None, index=True):
    """index=False: the file only (a save after every step); the search index is updated at the end."""
    with suppress(Exception):
        f = session_file(sid, cwd)
        ts = time.time()
        data = {**_session_extras(f),         # what the TUI keeps there (archive, queued, draft ...) stays
                "id": sid, "title": name or session_title(msgs),     # a named session keeps its name as the title
                "name": name, "ts": ts, "cwd": str(cwd or Path.cwd()), "msgs": msgs}
        write_json(f, data)                   # atomic: a run killed mid-save left a broken file
        if index:
            index_session(msgs, sid, cwd=cwd, path=f, ts=ts)

def load_session(sid, cwd=None):
    f = session_path(sid, cwd)
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    return d.get("id") or sid, d.get("msgs") or [], d.get("name") or ""

