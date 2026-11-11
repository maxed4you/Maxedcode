"""maxedcode app: the window's local web server (stdlib only, no Node, no build step).

    python local_agent.py --app [--port 3077]      prints "maxedcode app: http://127.0.0.1:<port>/?t=<token>"

On macOS / Linux `--app` also shows the page (webapp/native.py: a pywebview window, else the default browser;
--browser, --no-window) and runs the server itself with `--app --serve` in the background.

The page (static/) talks to this server; the server runs maxedcode exactly as the CLI's headless mode does:
one turn = `local_agent.py --print --output-format stream-json --session <id>` in the chat's folder, with
AGENT_MODE (plan/auto) and AGENT_EFFORT. Its JSON lines are relayed to the page as they come. So the window
has the CLI's prompt, tools, cache and sessions (~/.maxedcode/sessions): a chat started here opens in /history
and the other way round.

Only this machine can reach it (127.0.0.1) and only a browser that opened the printed URL: the token is turned
into a SameSite=Strict cookie, and every other request without it is refused.
"""
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import plat, util
from .pty import Pty

STATIC = Path(__file__).resolve().parent / "static"
AGENT_DIR = Path(__file__).resolve().parents[2]
AGENT = AGENT_DIR / "local_agent.py"
TOKEN = secrets.token_urlsafe(24)
COOKIE = "mxapp"
MODES = {"plan", "auto", "manual", "accept-edits"}   # as the TUI's modes; Manual / Accept edits ask in the window
APPROVALS = Path.home() / ".maxedcode-app" / "approvals"   # the window's answers to a turn's approval requests
# How long each turn, model step and tool took, and a helper's reasoning: the session file keeps only messages
# (extra keys there would go to the model), so a reopened chat got them from here.
TIMES = Path.home() / ".maxedcode-app" / "times"
HELPERS = {"browser", "subagent"}     # tools whose `child` events belong to their card
EFFORTS = {"off", "low", "medium", "high", "ultra"}
RUNS = {}                     # session id -> running turn (Run)
PTYS = {}                     # terminal id -> {"pty": Pty, "cwd": folder}: the side panel's terminals
RUNS_LOCK = threading.Lock()
UPLOAD_LIMIT = 100_000_000      # one dropped or pasted file
ATTACHMENTS = Path.home() / ".maxedcode-app" / "attachments"
ARCHIVED = Path.home() / ".maxedcode-app" / "archived.json"   # the window's archived chats (ids); the files stay
TOOL_CHARS = 30_000           # a tool result shown in a card (the model saw maxedcode's own clip)
LAST_SEEN = [time.monotonic()]       # the last request of a page
QUIT_WHEN_IDLE = threading.Event()   # the launcher's window closed: exit once no turn runs and no page is left
# macOS / Linux with the page in a browser tab (webapp/native.py): nobody reports the tab closing and a page load
# must not cancel the wish, so the server exits once idle even with a page that loaded again
QUIT_STICKY = threading.Event()
# a turn in a session of its own (macOS / Linux): kill_tree ends its whole process group
POPEN_GROUP = {} if os.name == "nt" else {"start_new_session": True}
IDLE_GRACE = 90               # seconds without a page request (an open window pings every 30 s)


def agent_env(**extra):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(AGENT_DIR)}
    env.update({k: v for k, v in extra.items() if v})
    return env


def kill_tree(proc):
    """The turn runs PowerShell, Python and test children: stop the whole tree."""
    if proc.poll() is None:
        if os.name != "nt":
            import signal
            with suppress(Exception):
                os.killpg(proc.pid, signal.SIGKILL)     # the turn's own session (POPEN_GROUP)
        with suppress(Exception):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with suppress(Exception):
            proc.kill()


class Run:
    """One turn, owned by the server and not by the page (as in the old window, whose host ran the agent):
    the page may leave (another chat, a reload, the window closed) and come back. The turn's lines are kept,
    so a page that comes back gets the chat as of the turn's start plus every line so far, then the new ones.
    Only /api/stop ends a turn early."""

    def __init__(self, sid, cwd, prompt, proc, before):
        self.sid, self.cwd, self.prompt, self.proc = sid, cwd, prompt, proc
        self.before = before            # the chat as of the turn's start (the file is saved after every step)
        self.started = time.time()
        self.lines = []                 # the turn's JSON lines, as sent to the page
        self.done = False
        self.cond = threading.Condition()
        self.add({"type": "session", "id": sid, "cwd": cwd})
        threading.Thread(target=self.pump, daemon=True).start()

    def add(self, obj):
        line = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        with self.cond:
            self.lines.append(line + b"\n")
            self.cond.notify_all()

    def pump(self):
        err = []
        reader = threading.Thread(target=lambda: err.extend(self.proc.stderr.read().decode("utf-8", "replace").splitlines()),
                                  daemon=True)
        reader.start()
        try:
            for raw in self.proc.stdout:
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("{"):
                    self.add(text.encode("utf-8"))
            self.proc.wait()
            reader.join(timeout=2)
            with suppress(Exception):
                save_times(self.sid, self.cwd, self.started, self.lines)
            prune_chat_dir(self.cwd)
            self.add({"type": "exit", "code": self.proc.returncode, "stderr": "\n".join(err[-8:])})
        finally:
            for pipe in (self.proc.stdout, self.proc.stderr):
                with suppress(Exception):
                    pipe.close()
            with RUNS_LOCK:
                if RUNS.get(self.sid) is self:
                    RUNS.pop(self.sid)
            with self.cond:
                self.done = True
                self.cond.notify_all()

    def follow(self, start=0):
        """The lines from `start` on, then each new one as it comes, until the turn ends."""
        i = max(0, int(start))
        while True:
            with self.cond:
                while i >= len(self.lines) and not self.done:
                    self.cond.wait()
                batch, done = self.lines[i:], self.done
            i += len(batch)
            yield from batch
            if done and i >= len(self.lines):
                return

    def title(self):
        return clean_title(self.prompt)[:60] or "New session"


def times_file(sid):
    return TIMES / f"{sid}.json"


def load_times(sid):
    """{"turns": {"<n>": {"ms", "model": [ms per step]}}, "tools": {call id: ms}, "think": {call id: [[step, text]]}}"""
    with suppress(OSError, ValueError):
        data = json.loads(times_file(sid).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {"turns": {}, "tools": {}, "think": {}}


def save_times(sid, cwd, started, lines):
    """A finished turn's timings from its stream-json lines, under the turn's number in the saved chat."""
    turn = {"ms": int((time.time() - started) * 1000), "model": []}
    tools, think, acts, open_helpers = {}, {}, {}, []
    for raw in lines:
        with suppress(ValueError):
            ev = json.loads(raw)
            kind = ev.get("type")
            if kind == "assistant":
                turn["model"].append(int(ev.get("model_ms") or 0))
                for c in (ev.get("message") or {}).get("tool_calls") or []:
                    if (c.get("function") or {}).get("name") in HELPERS:
                        open_helpers.append(c.get("id"))
            elif kind == "tool_result":
                if ev.get("id"):
                    tools[ev["id"]] = int(ev.get("ms") or 0)
                if ev.get("id") in open_helpers:
                    open_helpers.remove(ev["id"])
            elif kind == "child" and ev.get("think") and open_helpers:
                think.setdefault(open_helpers[-1], []).append([ev.get("step") or 0, ev["think"]])
            elif kind == "child" and ev.get("text") and open_helpers:
                acts.setdefault(open_helpers[-1], []).append(ev["text"])
    if not turn["model"]:
        return
    n = sum(1 for m in transcript(sid, cwd)["items"] if m["role"] == "user")
    data = load_times(sid)
    data.setdefault("turns", {})[str(n)] = turn
    data.setdefault("tools", {}).update(tools)
    data.setdefault("think", {}).update(think)
    data.setdefault("acts", {}).update(acts)
    TIMES.mkdir(parents=True, exist_ok=True)
    times_file(sid).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def clean_title(text):
    """Old-window attachments start the title as [File "name" (size, sha256:…)]: keep just the name."""
    return re.sub(r'\[File "([^"]+)"[^\]]*(?:\]|$)\s*', r"\1 ", str(text or "")).strip()


# The window lists its own chats, as the old window did: ids it made ("app-<time>") and the ones the old
# DeepSeek Harness window made ("session-<uuid>"). Terminal sessions stay in the CLI's /history.
APP_PREFIXES = util.WINDOW_PREFIXES
# A chat works in a project folder the user picked; without one it works in its own folder <chats>\<chat id>, made
# when a turn or the user needs it and removed again while empty. <chats> is ~\.maxedcode\chats, next to the CLI's
# sessions and memory, or the folder chosen in Settings -> General (kept in WORKSPACE_FILE).
WORKSPACE_FILE = Path.home() / ".maxedcode-app" / "workspace.txt"
# The window's language (its own localStorage holds it): the browser extension follows it (chrome/host.py)
LANG_FILE = Path.home() / ".maxedcode-app" / "lang.txt"
LANGS = ("ru", "en")
SHARED_DEFAULT = util.HOME / "chats"
# Where chats without a project used to share one folder: they move to their own folders (move_shared_chats).
OLD_SHARED = Path.home() / "Documents" / "maxedcode"
# The DeepSeek Harness window's default workspace: its chats move to the shared folder (move_old_workspace).
OLD_WORKSPACE = Path.home() / "Documents" / "deepseek-harness" / "default-workspace"
OLD_JUNK = {"debug.log", ".tmp"}      # DSH's own files there


def shared_folder():
    """The folder of chats without a project."""
    with suppress(OSError):
        p = Path(WORKSPACE_FILE.read_text(encoding="utf-8").strip())
        if p.is_absolute() and p.is_dir():
            return p
    return SHARED_DEFAULT


def chat_dir(path):
    """`path` if it is the own folder of a chat without a project (<chats>\\<window chat id>), else None."""
    with suppress(Exception):
        p = Path(str(path or ""))
        if p.is_absolute() and p.parent == shared_folder() and valid_session(p.name) and p.name.startswith(APP_PREFIXES):
            return p
    return None


def work_dir(path, sid):
    """The folder a turn of chat `sid` runs in: the chats folder itself means "no project", which is the chat's
    own folder; that folder (or one asked for by the page) is made now."""
    p = Path(str(path or ""))
    with suppress(Exception):
        if p.is_absolute() and p == shared_folder():
            p = p / sid
    # the chat's own folder, also under an older chats folder (Settings -> General changed it): a missing one ran
    # the turn in the user's home and saved a second copy of the chat there
    if chat_dir(p) or (p.is_absolute() and p.name == sid and str(sid).startswith(APP_PREFIXES)
                       and p.parent.is_dir()):
        with suppress(OSError):
            p.mkdir(parents=True, exist_ok=True)
    return folder(p)


def ensure_dir(path):
    """A chat folder removed while empty comes back when the user opens it (explorer, terminal)."""
    p = chat_dir(path)
    if p:
        with suppress(OSError):
            p.mkdir(parents=True, exist_ok=True)
    return path


def prune_chat_dir(path):
    """A chat's own folder the turn left empty is removed: nothing litters the chats folder."""
    p = chat_dir(path)
    if p:
        with suppress(OSError):
            p.rmdir()                   # only when empty


PROJECTS = util.HOME / "projects"     # "New project" in the window: an empty folder here


def new_project(name):
    """A new empty project folder named by the user (characters Windows forbids are dropped; a taken name
    gets " 2", " 3"...)."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().rstrip(". ")[:80]
    if not clean or clean.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", clean):
        raise ValueError("bad name")
    p, n = PROJECTS / clean, 1
    while p.exists():
        n += 1
        p = PROJECTS / f"{clean} {n}"
    p.mkdir(parents=True)
    return str(p)


def set_shared_folder(path):
    p = Path(str(path or "")).expanduser()
    if not (p.is_absolute() and p.is_dir()):
        raise ValueError("not a folder")
    WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_FILE.write_text(str(p), encoding="utf-8")
    return str(p)


def move_sessions(old, new):
    """Moves the sessions (window chats and terminal sessions) of folder `old` to folder `new`: the files go to
    new's session folder with cwd rewritten, the search index and the project memory follow. -> moved count."""
    src = util.SESS / util.hash_cwd(old)
    if not src.is_dir():
        return 0
    dst = util.sess_dir(new)
    moved = 0
    for f in list(src.glob("*.json")):
        with suppress(OSError, ValueError):
            data = json.loads(f.read_text(encoding="utf-8"))
            data["cwd"] = str(new)
            target = dst / f.name
            if target.exists():
                continue
            target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            f.unlink()
            util.index_session(data.get("msgs") or [], data.get("id") or f.stem, cwd=new, path=target, ts=data.get("ts"))
            moved += 1
    if util.SESSION_INDEX.exists():
        with suppress(Exception):
            with util._index_db() as db, db:
                db.execute("DELETE FROM session_search WHERE cwd_hash=?", (src.name,))
    with suppress(OSError):
        src.rmdir()                     # only when empty
    from ..config import MEM_DIR
    old_mem, new_mem = MEM_DIR / util.hash_cwd(old), MEM_DIR / util.hash_cwd(new)
    if old_mem.is_dir() and not new_mem.exists():
        with suppress(OSError):
            old_mem.rename(new_mem)
    return moved


def move_shared_chats():
    """Once: the window's chats that shared Documents\\maxedcode get their own (not yet made) folders under the
    chats folder; the old folder is removed when nothing is left in it. A folder set in Settings is left alone."""
    src = util.SESS / util.hash_cwd(OLD_SHARED)
    if WORKSPACE_FILE.exists() or not src.is_dir():
        return 0
    moved = 0
    for f in list(src.glob("*.json")):
        with suppress(OSError, ValueError):
            data = json.loads(f.read_text(encoding="utf-8"))
            sid = data.get("id") or f.stem
            # a chat, or a subagent session of one: it goes with its chat (deleting the chat deletes it too)
            owner = sid if sid.startswith(APP_PREFIXES) else str(data.get("parent_sid") or "")
            if not (valid_session(owner) and owner.startswith(APP_PREFIXES)):
                continue
            new = SHARED_DEFAULT / owner
            data["cwd"] = str(new)
            target = util.sess_dir(new) / f.name
            if target.exists():
                continue
            target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            f.unlink()
            util.index_session(data.get("msgs") or [], sid, cwd=new, path=target, ts=data.get("ts"))
            moved += 1
    with suppress(OSError):
        src.rmdir()                     # only when empty: terminal sessions there stay
    with suppress(OSError):
        OLD_SHARED.rmdir()              # only when empty: files the chats made stay where they are
    return moved


def move_old_workspace():
    """Once: the chats of the DeepSeek Harness default workspace move to the shared folder, its files too (DSH's
    own log and temp folder are dropped), and the empty deepseek-harness folder is removed."""
    if not OLD_WORKSPACE.is_dir():
        return
    new = shared_folder()
    new.mkdir(parents=True, exist_ok=True)
    move_sessions(OLD_WORKSPACE, new)
    for item in list(OLD_WORKSPACE.iterdir()):
        with suppress(OSError):
            if item.name in OLD_JUNK:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
            elif not (new / item.name).exists():
                shutil.move(str(item), str(new / item.name))
    for d in (OLD_WORKSPACE, OLD_WORKSPACE.parent):
        with suppress(OSError):
            d.rmdir()                   # only when empty: a file left behind keeps it


def new_session_id():
    """A window chat id: "app-" + the CLI's time format, unique among the sessions."""
    base = time.strftime("app-%Y%m%d-%H%M%S")
    sid, n = base, 1
    while util.session_path(sid).exists() or sid in RUNS:
        n += 1
        sid = f"{base}-{n}"
    return sid


def valid_session(sid):
    return isinstance(sid, str) and 0 < len(sid) <= 100 and all(c.isalnum() or c in "-_" for c in sid)


def folder(path):
    """An existing folder (absolute), else the user's home."""
    with suppress(Exception):
        p = Path(str(path or "")).expanduser()
        if p.is_absolute() and p.is_dir():
            return str(p)
    return str(Path.home())


def sessions():
    """The window's chats of every folder, newest first (APP_PREFIXES)."""
    out = []
    with RUNS_LOCK:
        runs = dict(RUNS)
    archived = archived_ids()
    for s in util.list_sessions(all_cwds=True, window=True):
        if not str(s["id"]).startswith(APP_PREFIXES) or (s["empty"] and s["id"] not in runs):
            continue
        run = runs.pop(s["id"], None)
        title = run.title() if s["empty"] else clean_title(s["title"])
        out.append({"id": s["id"], "title": title or "New session", "ts": s["ts"], "cwd": s["cwd"],
                    "project": s["project"], "running": run is not None, "archived": s["id"] in archived})
    # a new chat has no file until its first step ends: it is listed from its turn
    for run in runs.values():
        out.append({"id": run.sid, "title": run.title(), "ts": run.started, "cwd": run.cwd,
                    "project": Path(run.cwd).name, "running": True})
    return sorted(out, key=lambda x: -x["ts"])


def archived_ids():
    with suppress(OSError, ValueError):
        return set(json.loads(ARCHIVED.read_text(encoding="utf-8")))
    return set()


def archive(sid, on):
    """Archive a chat (it leaves the list, nothing is deleted) or bring it back."""
    ids = archived_ids()
    (ids.add if on else ids.discard)(sid)
    ARCHIVED.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVED.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    return {"id": sid, "archived": bool(on)}


def search(query, limit=40):
    """The window's chats whose text has every word of the query (the CLI's /search index, all folders),
    one hit per chat with a snippet, best first."""
    terms = re.findall(r"\w+", str(query or ""), flags=re.UNICODE)
    if not terms or not util.SESSION_INDEX.exists():
        return []
    match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    try:
        with util._index_db() as db, db:
            rows = db.execute("SELECT session_id, snippet(session_search,3,'\u2039','\u203a',' … ',14) "
                              "FROM session_search WHERE session_search MATCH ? ORDER BY rank LIMIT 400", (match,)).fetchall()
    except Exception:          # sqlite3.Error, or an index from an older version: search is optional
        return []
    out, seen = [], set()
    for sid, snippet in rows:
        if sid in seen or not str(sid).startswith(APP_PREFIXES):
            continue
        seen.add(sid)
        out.append({"id": sid, "snippet": " ".join(str(snippet or "").split())})
        if len(out) >= limit:
            break
    return out


def transcript(sid, cwd):
    """A saved session as the page draws it: user text, assistant reasoning/text/tool calls, tool results.
    Service messages (system, <env>, memory, repo map, compaction) are left out, as in the TUI."""
    path = util.session_path(sid, cwd or None)
    data = json.loads(path.read_text(encoding="utf-8"))
    items, mode = [], None
    for m in (data.get("archive") or []) + (data.get("msgs") or []):
        role = m.get("role")
        if role == "user":
            found = re.search(r'<user_input mode="(\w+)"', str(m.get("content") or ""))
            if found:
                mode = "plan" if found.group(1) == "plan" else "auto"     # the chat goes on in its last mode
            text = util.visible_user_text(m.get("content"))
            if text:
                items.append({"role": "user", "text": text})
        elif role == "assistant":
            think, text = util.split_think(str(m.get("content") or ""))
            calls = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                      "args": (c.get("function") or {}).get("arguments") or "{}"} for c in m.get("tool_calls") or []]
            items.append({"role": "assistant", "reasoning": m.get("reasoning_content") or think,
                          "text": text.strip(), "calls": calls})
        elif role == "tool":
            content = m.get("content")
            if isinstance(content, list):
                content = "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            items.append({"role": "tool", "id": m.get("tool_call_id"), "content": str(content or "")[:TOOL_CHARS]})
    return {"id": data.get("id") or sid, "cwd": data.get("cwd") or cwd, "items": items, "mode": mode}


def rename(sid, cwd, title):
    """A chat's own name: the session file's title (what /history and the sidebar show)."""
    title = " ".join(title.split())[:120]
    if not title:
        raise ValueError("empty title")
    path = util.session_path(sid, cwd or None)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["title"] = data["name"] = title
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"id": sid, "title": title}


def fork(sid, cwd, turns, files=False):
    """A new chat with this one's history up to the end of its `turns`-th turn (the "fork from here" button).
    The prefix and the kept messages are the same, so the fork's next turn reuses the prompt cache.
    files=True also puts the files back as they were after that turn (the TUI's /rewind): every checkpoint of
    a later turn of this chat is undone."""
    path = util.session_path(sid, cwd or None)
    data = json.loads(path.read_text(encoding="utf-8"))
    msgs, kept, seen = data.get("msgs") or [], [], 0
    for m in msgs:
        if m.get("role") == "user" and util.visible_user_text(m.get("content")):
            seen += 1
            if seen > turns:
                break
        kept.append(m)
    new = new_session_id()
    title = (util.clean_title(data.get("title")) or util.session_title(msgs) or "New session")[:100]
    out = {**data, "id": new, "title": title + " (fork)", "name": "", "ts": time.time(), "msgs": kept}
    out.pop("queued", None)
    target = path.parent / f"{new}.json"
    target.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    restored = []
    if files:
        from ..tools import undo
        restored = undo.rewind_after([c.get("id") for m in kept for c in m.get("tool_calls") or []], sid)
    return {"id": new, "restored": restored}


def module(name, *args, body=None, cwd=None, timeout=60):
    """A maxedcode page module in a fresh process (its config is read when it is imported): its last JSON line."""
    try:
        r = subprocess.run([sys.executable, "-m", name, *args], input=json.dumps(body or {}), capture_output=True,
                           text=True, encoding="utf-8", cwd=str(cwd or Path.home()), env=agent_env(), timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:     # the page gets an answer instead of a request that never ends
        return {"error": f"{name} did not answer in {timeout} s"}
    line = (r.stdout.strip().splitlines() or [""])[-1]
    if line.startswith("{"):
        return json.loads(line)
    return {"error": (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]}


# Settings -> Models: one start or stop of a model server at a time (maxedcode.launch), owned by this server like
# a turn: loading a big model takes minutes, so the page polls /api/launch?job=1 instead of holding a request.
LAUNCH = {"job": None}
LAUNCH_LOCK = threading.Lock()


def launch_job():
    """What the page shows of the start / stop in progress (or the last one): kind, its lines, the result."""
    j = LAUNCH["job"]
    if not j:
        return None
    return {"id": j["id"], "do": j["do"], "kind": j["kind"], "model": j.get("model"), "ctx": j.get("ctx"),
            "lines": j["lines"][-1:], "pct": j.get("pct"), "result": j["result"],
            "secs": int((j.get("ended") or time.monotonic()) - j["started"]),
            "age": int(time.monotonic() - j["ended"]) if j.get("ended") else 0}


def launch_begin(data):
    """{"do": "start" | "stop" | "cancel", "kind", "model", "ctx", "script"} -> the job."""
    what = data.get("do")
    with LAUNCH_LOCK:
        old = LAUNCH["job"]
        if what == "cancel" or (old and old["result"] is None and what in ("start", "stop")):
            if old and old["result"] is None:
                kill_tree(old["proc"])
                old["result"] = {"ok": False, "error": "cancelled"}
                old["ended"] = time.monotonic()
            if what == "cancel":
                return launch_job()
        if what not in ("start", "stop"):
            return {"error": "do: start, stop or cancel"}
        body = {k: data.get(k) for k in ("kind", "model", "ctx", "script", "way", "opts", "base")}
        proc = subprocess.Popen([sys.executable, "-m", "maxedcode.launch", what], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", cwd=str(Path.home()), env=agent_env(), **POPEN_GROUP,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc.stdin.write(json.dumps(body))
        proc.stdin.close()
        job = {"id": secrets.token_hex(4), "do": what, "proc": proc, "lines": [], "result": None,
               "started": time.monotonic(), **body}
        LAUNCH["job"] = job

    def pump():
        for line in proc.stdout:
            with suppress(ValueError):
                obj = json.loads(line)
                if obj.get("done"):
                    job["result"] = obj
                elif obj.get("text"):
                    job["lines"].append(obj["text"])
                    job["pct"] = obj.get("pct")
        proc.wait()
        if job["result"] is None:
            job["result"] = {"ok": False, "error": f"exit {proc.returncode}"}
        job["ended"] = time.monotonic()
    threading.Thread(target=pump, daemon=True).start()
    return launch_job()


def settings(body=None):
    """Settings -> maxedcode (maxedcode.appsettings)."""
    return module("maxedcode.appsettings", "set" if body is not None else "get", body=body)


def cli(cwd, body=None):
    """Settings -> CLI commands (maxedcode.appcli), for the chat's folder: project rules, memory and skills are per folder."""
    where = folder(cwd)
    return module("maxedcode.appcli", "set" if body is not None else "get", where, body=body, cwd=where)


# The Explorer-style "Select Folder" dialog (IFileOpenDialog with FOS_PICKFOLDERS: address bar, search, Quick
# access). .NET Framework's FolderBrowserDialog only has the old "Browse For Folder" tree.
PICK_FOLDER_CS = r'''
using System; using System.Runtime.InteropServices;
public static class MxPick {
  [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")] class FileOpenDialogCo {}
  [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IFileDialog {
    [PreserveSig] int Show(IntPtr parent);
    void SetFileTypes(uint c, IntPtr f); void SetFileTypeIndex(uint i); void GetFileTypeIndex(out uint i);
    void Advise(IntPtr e, out uint c); void Unadvise(uint c); void SetOptions(uint fos); void GetOptions(out uint fos);
    void SetDefaultFolder(IShellItem si); void SetFolder(IShellItem si); void GetFolder(out IShellItem si);
    void GetCurrentSelection(out IShellItem si); void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string n);
    void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string n); void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string t);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string t); void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string t);
    void GetResult(out IShellItem si);
  }
  [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv); void GetParent(out IShellItem si);
    void GetDisplayName(uint sigdn, [MarshalAs(UnmanagedType.LPWStr)] out string name);
  }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, ref Guid riid, out IShellItem item);
  public static string Folder(string start, string title, IntPtr owner) {
    var d = (IFileDialog)new FileOpenDialogCo();
    uint o; d.GetOptions(out o); d.SetOptions(o | 0x20 | 0x40 | 0x800);   // PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST
    if (!String.IsNullOrEmpty(title)) d.SetTitle(title);
    try { var g = typeof(IShellItem).GUID; IShellItem si; SHCreateItemFromParsingName(start, IntPtr.Zero, ref g, out si); d.SetFolder(si); } catch {}
    if (d.Show(owner) != 0) return null;
    IShellItem r; d.GetResult(out r); string p; r.GetDisplayName(0x80058000, out p); return p;   // SIGDN_FILESYSPATH
  }
}'''


def pick_folder(start, title=""):
    """The Explorer "Select Folder" dialog, on top of the app window. -> the folder or None (cancelled).
    macOS / Linux: the desktop's own dialog (Finder, zenity, kdialog); the native window asks pywebview instead."""
    if os.name != "nt":
        return plat.pick_folder(folder(start), title)
    script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -TypeDefinition $env:MX_CS; "
              "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
              "$p = [MxPick]::Folder($env:MX_START, $env:MX_TITLE, $f.Handle); if ($p) { [Console]::Out.Write($p) }")
    env = {**os.environ, "MX_CS": PICK_FOLDER_CS, "MX_START": folder(start), "MX_TITLE": title or ""}
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command",
                        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script], capture_output=True,
                       text=True, encoding="utf-8", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip() or None


def _first(*paths):
    return next((str(p) for p in paths if p and Path(p).exists()), None)


def open_apps():
    """Programs the "Open in" menu offers: the ones found on this computer (File Explorer always)."""
    if os.name != "nt":
        return _open_apps_posix()
    local, pf, pf86 = Path(os.environ.get("LOCALAPPDATA", "")), Path(os.environ.get("ProgramFiles", "")), \
        Path(os.environ.get("ProgramFiles(x86)", ""))
    found = {
        "explorer": "explorer.exe",
        "vscode": _first(local / "Programs/Microsoft VS Code/Code.exe", pf / "Microsoft VS Code/Code.exe"),
        "insiders": _first(local / "Programs/Microsoft VS Code Insiders/Code - Insiders.exe"),
        "cursor": _first(local / "Programs/cursor/Cursor.exe"),
        "windsurf": _first(local / "Programs/Windsurf/Windsurf.exe"),
        "pycharm": _first(*sorted((pf / "JetBrains").glob("PyCharm*/bin/pycharm64.exe"), reverse=True)),
        "sublime": _first(pf / "Sublime Text/sublime_text.exe", pf / "Sublime Text 3/sublime_text.exe"),
        "github": _first(local / "GitHubDesktop/GitHubDesktop.exe"),
        "gitbash": _first(pf / "Git/git-bash.exe", pf86 / "Git/git-bash.exe"),
        "terminal": _first(local / "Microsoft/WindowsApps/wt.exe"),
    }
    return {k: v for k, v in found.items() if v}


def open_in(path, app):
    """Open the chat's folder in one of open_apps()."""
    target, exe = folder(path), open_apps().get(app or "explorer")
    if not exe:
        return False
    if os.name != "nt":
        return _open_in_posix(target, app or "explorer", exe)
    with suppress(OSError):
        if app == "gitbash":
            subprocess.Popen([exe, f"--cd={target}"])
        elif app == "terminal":
            subprocess.Popen([exe, "-d", target])
        elif app == "github":
            subprocess.Popen([exe, target])
        else:
            subprocess.Popen([exe, target])
        return True
    return False


# "Open in" on macOS / Linux: key -> how to find it. A .app bundle opens with `open -a`, a command gets the folder.
POSIX_APPS = {
    "vscode": ("code", "Visual Studio Code.app"),
    "insiders": ("code-insiders", "Visual Studio Code - Insiders.app"),
    "cursor": ("cursor", "Cursor.app"),
    "windsurf": ("windsurf", "Windsurf.app"),
    "pycharm": ("pycharm", "PyCharm.app"),
    "sublime": ("subl", "Sublime Text.app"),
    "github": ("github", "GitHub Desktop.app"),
}
LINUX_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "kgx", "xfce4-terminal", "tilix", "alacritty",
                   "kitty", "wezterm", "foot", "xterm")


def _open_apps_posix():
    found = {"explorer": "open" if sys.platform == "darwin" else (shutil.which("xdg-open") or "")}
    for key, (cmd, bundle) in POSIX_APPS.items():
        app = None
        if sys.platform == "darwin":
            app = next((str(p) for p in (Path("/Applications", bundle), Path.home() / "Applications" / bundle)
                        if p.exists()), None)
        found[key] = app or shutil.which(cmd)
    if sys.platform == "darwin":
        found["terminal"] = next((str(p) for p in (Path("/Applications/iTerm.app"),
                                                   Path("/System/Applications/Utilities/Terminal.app"),
                                                   Path("/Applications/Utilities/Terminal.app")) if p.exists()), None)
    else:
        found["terminal"] = next((shutil.which(t) for t in LINUX_TERMINALS if shutil.which(t)), None)
    return {k: v for k, v in found.items() if v}


def _open_in_posix(target, app, exe):
    if app == "explorer":
        return plat.open_path(target)
    if exe.endswith(".app"):
        return plat._detached(["open", "-a", exe, target])
    if app == "terminal":
        name = Path(exe).name
        if name in ("gnome-terminal", "xfce4-terminal", "tilix", "kgx"):
            return plat._detached([exe, f"--working-directory={target}"])
        if name == "konsole":
            return plat._detached([exe, "--workdir", target])
        return plat._detached([exe], cwd=target)     # x-terminal-emulator, xterm, kitty ...: the folder as cwd
    return plat._detached([exe, target])


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", "dist", "build"}


def list_dir(path, root, hidden=False):
    """One folder level for the file panel: folders first, then files (service folders and dot files only when
    `hidden` asks for them)."""
    base, p = Path(folder(root)), Path(str(path or root))
    if not p.is_absolute():
        p = base / p
    out = []
    with suppress(OSError):
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if not hidden and (e.name.startswith(".") or e.is_dir() and e.name in SKIP_DIRS):
                continue
            with suppress(OSError):
                st = e.stat()
                out.append({"name": e.name, "path": str(e), "dir": e.is_dir(),
                            "size": 0 if e.is_dir() else st.st_size, "mtime": st.st_mtime})
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "items": out[:2000]}


RAW_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}


def raw_image(path, limit=25_000_000):
    """An image for the file preview: (bytes, type) or None."""
    p = Path(str(path or ""))
    if p.suffix.lower() not in RAW_TYPES or not p.is_file() or p.stat().st_size > limit:
        return None
    return p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def recycle(path):
    """Delete to the Recycle Bin (the user can restore it), never for good (macOS / Linux: the trash)."""
    if os.name != "nt":
        return plat.trash(path)
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 16, 64, 1024
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) or op.fAnyOperationsAborted:
        raise OSError("could not move it to the Recycle Bin")


def fs_action(data):
    """The file panel's actions: new file / folder, rename, delete (Recycle Bin), open with Windows, show in
    Explorer. Names are plain names (no folders in them)."""
    act, p = data.get("do"), Path(str(data.get("path") or ""))
    if not p.is_absolute():
        raise ValueError("not an absolute path")
    name = str(data.get("name") or "").strip()
    if act in ("mkdir", "touch", "rename") and (not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in (".", "..")):
        raise ValueError("bad name")
    ensure_dir(p)                       # a chat's own folder removed while empty: back for a new file
    if act == "mkdir":
        (p / name).mkdir()
        return {"path": str(p / name)}
    if act == "touch":
        target = p / name
        if target.exists():
            raise ValueError("already exists")
        target.touch()
        return {"path": str(target)}
    if act == "rename":
        target = p.with_name(name)
        if target.exists() and target != p:
            raise ValueError("already exists")
        p.rename(target)
        return {"path": str(target)}
    if act == "delete":
        if not p.exists() or p.parent == p:
            raise ValueError("nothing there")
        recycle(p)
        return {"deleted": str(p)}
    if act == "start":
        if os.name != "nt":
            return {"ok": plat.open_path(p)}
        os.startfile(str(p))            # the file's own app, as a double click in Explorer
        return {"ok": True}
    if act == "reveal":
        if os.name != "nt":
            return {"ok": plat.reveal(p)}
        subprocess.Popen(["explorer.exe", "/select,", str(p)])
        return {"ok": True}
    raise ValueError("unknown action")


def read_text(path, limit=400_000):
    """A file for the preview: text only, cut at the limit."""
    p = Path(str(path or ""))
    if not p.is_file():
        return {"error": "not a file"}
    st = p.stat()
    with open(p, "rb") as f:        # only the shown part: a multi-GB model or log was read whole
        raw = f.read(limit)
    if b"\0" in raw[:8000]:
        return {"path": str(p), "binary": True, "size": st.st_size, "mtime": st.st_mtime}
    return {"path": str(p), "text": raw.decode("utf-8", "replace"), "cut": st.st_size > limit, "mtime": st.st_mtime}


def stat(path):
    """When a path last changed (the preview reloads when the agent edits the file) and whether it is a folder
    (a path clicked in an answer opens the file or the folder); mtime None: there is nothing there."""
    with suppress(OSError, ValueError):
        p = Path(str(path or ""))
        return {"mtime": p.stat().st_mtime, "dir": p.is_dir()}
    return {"mtime": None, "dir": False}


def find_files(root, query, limit=40):
    """Files under the chat's folder whose path contains the query (the "@" menu)."""
    base, q, out = Path(folder(root)), str(query or "").lower().replace("\\", "/"), []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
            rel = os.path.relpath(os.path.join(dirpath, name), base).replace("\\", "/")
            if not q or q in rel.lower():
                out.append({"rel": rel + ("/" if is_dir else ""), "path": os.path.join(dirpath, name), "dir": is_dir})
                if len(out) >= limit:
                    return out
    return out


def pick_file(start):
    """The Windows file dialog: files to mention with @ in the message (macOS / Linux: the desktop's own)."""
    if os.name != "nt":
        return plat.pick_files(folder(start))
    script = ("Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.Form; "
              "$f.TopMost = $true; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect = $true; "
              f"$d.InitialDirectory = '{folder(start).replace(chr(39), chr(39) * 2)}'; "
              "if ($d.ShowDialog($f) -eq 'OK') { [Console]::Out.Write(($d.FileNames -join \"`n\")) }")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                       text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return [x for x in r.stdout.splitlines() if x.strip()]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # a turn streams until the run ends, then the connection closes
    server_version = "maxedcode"

    def log_message(self, *args):
        pass

    # ── plumbing ──
    def authorized(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return COOKIE in jar and secrets.compare_digest(jar[COOKIE].value, TOKEN)

    def send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        return data if isinstance(data, dict) else {}

    # ── routes ──
    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path == "/" and query.get("t") and secrets.compare_digest(query["t"], TOKEN):
            return self.send(302, "", extra={"Location": "/",
                                             "Set-Cookie": f"{COOKIE}={TOKEN}; Path=/; HttpOnly; SameSite=Strict"})
        if not self.authorized():
            return self.send(401, "maxedcode app: open the link the launcher printed.", "text/plain; charset=utf-8")
        LAST_SEEN[0] = time.monotonic()
        if url.path in ("/", "/index.html"):
            QUIT_WHEN_IDLE.clear()           # a window opened on this server again
        if url.path == "/api/ping":
            return self.send(200, {"running": sorted(RUNS)})
        if url.path == "/api/pty-list":
            with RUNS_LOCK:
                return self.send(200, [{"id": k, "cwd": v["cwd"]} for k, v in PTYS.items()])
        if url.path == "/api/pty-read":
            return self.pty_read(query.get("id"), int(query.get("from") or 0))
        if url.path == "/api/watch":
            return self.watch(query.get("session"), query.get("from") or 0)
        if url.path == "/api/sessions":
            return self.send(200, sessions())
        if url.path == "/api/session":
            if not valid_session(query.get("id")):
                return self.send(400, {"error": "bad id"})
            with RUNS_LOCK:
                run = RUNS.get(query["id"])
            if run:          # the page comes back to a running turn: the chat before it, then /api/watch
                return self.send(200, {**run.before, "running": True, "prompt": run.prompt, "started": run.started})
            try:
                return self.send(200, {**transcript(query["id"], query.get("cwd")), "times": load_times(query["id"])})
            except (OSError, ValueError) as error:
                return self.send(404, {"error": str(error)})
        if url.path == "/api/settings":
            return self.send(200, settings())
        if url.path == "/api/cloud":         # Settings -> Cloud: the same accounts and keys as /cloud in the CLI
            return self.send(200, module("maxedcode.cloud", "get"))
        if url.path == "/api/update":        # auto-update: this version, a downloaded newer one, a fresh update
            from .. import update
            return self.send(200, {**update.status(), "notice": update.take_notice()})
        if url.path == "/api/servers":       # model servers running / installed on this PC (first-start picker)
            if query.get("lan"):             # ... or on the other PCs of the own network (5-15 s)
                return self.send(200, module("maxedcode.servers", "lan", timeout=90))
            return self.send(200, module("maxedcode.servers", "scan"))
        if url.path == "/api/launch":        # Settings -> Models: servers, their models; ?job=1 only the job
            if query.get("job"):
                return self.send(200, {"job": launch_job()})
            if query.get("find"):            # "Find programs": the disks are looked through (up to 30 s)
                return self.send(200, {**module("maxedcode.launch", "find", timeout=90), "job": launch_job()})
            return self.send(200, {**module("maxedcode.launch", "get", timeout=90), "job": launch_job()})
        if url.path == "/api/cli":
            return self.send(200, cli(query.get("cwd")))
        if url.path == "/api/browser":
            return self.send(200, module("maxedcode.chrome", "status"))
        if url.path == "/api/info":
            shared = shared_folder()
            with suppress(OSError):
                shared.mkdir(parents=True, exist_ok=True)
            gone = [str(OLD_SHARED)] if not OLD_SHARED.is_dir() else []     # the page forgets the old shared folder
            return self.send(200, {"home": str(Path.home()), "agent": str(AGENT), "workspace": str(shared), "gone": gone})
        if url.path == "/api/apps":
            return self.send(200, sorted(open_apps()))
        if url.path == "/api/ls":
            return self.send(200, list_dir(query.get("path"), query.get("root"), query.get("hidden") == "1"))
        if url.path == "/api/raw":
            with suppress(OSError):
                img = raw_image(query.get("path"))
                if img:
                    return self.send(200, img[0], img[1])
            return self.send(404, {"error": "not an image"})
        if url.path == "/api/file":
            try:
                return self.send(200, read_text(query.get("path")))
            except OSError as error:
                return self.send(404, {"error": str(error)})
        if url.path == "/api/find":
            return self.send(200, find_files(query.get("root"), query.get("q")))
        if url.path == "/api/mtime":
            return self.send(200, stat(query.get("path")))
        if url.path == "/api/search":
            return self.send(200, search(query.get("q")))
        return self.static(url.path)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/api/quit":
            # the launcher (it has the token, not the cookie): its window closed
            if not secrets.compare_digest((parse_qs(url.query).get("t") or [""])[-1], TOKEN):
                return self.send(401, {"error": "unauthorized"})
            QUIT_WHEN_IDLE.set()
            if parse_qs(url.query).get("browser"):
                QUIT_STICKY.set()
            return self.send(200, {"running": len(RUNS)})
        if not self.authorized():
            return self.send(401, {"error": "unauthorized"})
        LAST_SEEN[0] = time.monotonic()
        if path == "/api/upload":
            return self.upload()
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if path == "/api/turn":
            return self.turn(data)
        if path == "/api/stop":
            with RUNS_LOCK:
                run = RUNS.get(data.get("session"))
            if run:
                kill_tree(run.proc)
            return self.send(200, {"stopped": bool(run)})
        if path == "/api/btw":
            return self.btw(data)
        if path == "/api/delete":
            sid = data.get("session")
            if not valid_session(sid) or sid in RUNS:
                return self.send(400, {"error": "bad or running session"})
            util.delete_session(util.session_path(sid, data.get("cwd") or None))
            with suppress(OSError):
                times_file(sid).unlink()
            return self.send(200, {"deleted": sid})
        if path == "/api/settings":
            return self.send(200, settings(data))
        if path == "/api/cloud":            # {"do": add|remove|default|refresh|note|options|test, ...}; a key only on stdin
            return self.send(200, module("maxedcode.cloud", "set", body=data))
        if path == "/api/update":           # {"check": true}: look at GitHub now and download a newer release
            from .. import update
            if data.get("check"):
                update.check(force=True, manual=True)
            return self.send(200, update.status())
        if path == "/api/servers":          # start an installed server: {"start": kind} -> {"base": url | null}
            # servers.start waits up to 180 s for a model to load
            return self.send(200, module("maxedcode.servers", "start", str(data.get("start") or ""), timeout=200))
        if path == "/api/launch":           # start / stop / cancel a model server: {"do", "kind", "way", "model", "ctx"}
            if data.get("do") in ("way-save", "way-remove", "path"):   # own ways, a program's file: the catalog after it
                return self.send(200, {**module("maxedcode.launch", "set", body=data, timeout=90), "job": launch_job()})
            return self.send(200, {"job": launch_begin(data)})
        if path == "/api/cli":
            return self.send(200, cli(data.get("cwd"), data))
        if path == "/api/expand":
            where = folder(data.get("cwd"))
            return self.send(200, module("maxedcode.appcli", "expand", where, body=data, cwd=where))
        if path == "/api/archive":
            if not valid_session(data.get("session")):
                return self.send(400, {"error": "bad id"})
            return self.send(200, archive(data["session"], bool(data.get("on", True))))
        if path in ("/api/rename", "/api/fork"):
            sid = data.get("session")
            if not valid_session(sid) or sid in RUNS:
                return self.send(400, {"error": "bad or running session"})
            try:
                if path == "/api/rename":
                    return self.send(200, rename(sid, data.get("cwd"), str(data.get("title") or "")))
                return self.send(200, fork(sid, data.get("cwd"), int(data.get("turns") or 0), bool(data.get("files"))))
            except (OSError, ValueError) as error:
                return self.send(400, {"error": str(error)})
        if path == "/api/fs":
            try:
                return self.send(200, fs_action(data))
            except (OSError, ValueError) as error:
                return self.send(400, {"error": str(error)})
        if path == "/api/new-project":
            try:
                return self.send(200, {"path": new_project(data.get("name"))})
            except (OSError, ValueError) as error:
                return self.send(400, {"error": str(error)})
        if path == "/api/workspace":
            try:
                return self.send(200, {"workspace": set_shared_folder(data.get("path"))})
            except (OSError, ValueError) as error:
                return self.send(400, {"error": str(error)})
        if path == "/api/lang":
            lang = str(data.get("lang") or "")
            if lang not in LANGS:
                return self.send(400, {"error": "bad language"})
            with suppress(OSError):
                if not LANG_FILE.exists() or LANG_FILE.read_text(encoding="utf-8").strip() != lang:
                    LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
                    LANG_FILE.write_text(lang, encoding="utf-8")
            return self.send(200, {"lang": lang})
        if path == "/api/browser":
            do = str(data.get("do") or "")
            if do not in ("on", "off", "guide", "reveal"):
                return self.send(400, {"error": "bad request"})
            return self.send(200, module("maxedcode.chrome", do))
        if path == "/api/pick-folder":
            return self.send(200, {"path": pick_folder(data.get("start"), str(data.get("title") or "")[:120])})
        if path == "/api/open":
            return self.send(200, {"opened": open_in(ensure_dir(data.get("path")), data.get("app"))})
        if path == "/api/pick-file":
            return self.send(200, {"paths": pick_file(data.get("start"))})
        if path == "/api/approve":
            # the window's answer to a running turn's approval request (headless waits for this file)
            rid, decision = str(data.get("id") or ""), data.get("decision")
            if not re.fullmatch(r"a\d{1,30}", rid) or decision not in ("allow", "deny", "always"):
                return self.send(400, {"error": "bad approval"})
            APPROVALS.mkdir(parents=True, exist_ok=True)
            (APPROVALS / (rid + ".json")).write_text(json.dumps({"decision": decision}), encoding="utf-8")
            return self.send(200, {"ok": True})
        if path == "/api/pty-open":
            return self.send(200, pty_open(data))
        if path in ("/api/pty-write", "/api/pty-resize", "/api/pty-close"):
            with RUNS_LOCK:
                term = PTYS.get(data.get("id"))
            if not term:
                return self.send(404, {"error": "no such terminal"})
            if path == "/api/pty-write":
                term["pty"].write(str(data.get("data") or ""))
            elif path == "/api/pty-resize":
                term["pty"].resize(int(data.get("cols") or 80), int(data.get("rows") or 24))
            else:
                with RUNS_LOCK:
                    PTYS.pop(data.get("id"), None)
                term["pty"].close()
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self.send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self.send(200, f.read_bytes(), ctype)

    def run_agent(self, sid, cwd, prompt, mode, effort, btw=False):
        args = [sys.executable, str(AGENT), "--print", "--output-format", "stream-json", "--session", sid]
        if btw:
            args.insert(3, "--btw")
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=agent_env(AGENT_HEADLESS_STREAM="1", AGENT_MODE=mode, AGENT_EFFORT=effort,
                                              AGENT_APPROVAL_DIR=str(APPROVALS)),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **POPEN_GROUP)
        proc.stdin.write(prompt.encode("utf-8"))      # through stdin: no command-line quoting
        proc.stdin.close()
        return proc

    def turn(self, data):
        """Start one turn and stream its JSON lines to the page (NDJSON). The turn goes on if the page leaves."""
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return self.send(400, {"error": "empty prompt"})
        sid = data.get("session") or None
        if sid is not None and not valid_session(sid):
            return self.send(400, {"error": "bad id"})
        sid = sid or new_session_id()
        cwd = work_dir(data.get("cwd"), sid)
        home = str(Path.home()).lower()
        if data.get("cwd") and cwd.lower() == home and str(Path(str(data["cwd"])).expanduser()).rstrip("\\/").lower() != home:
            # a project folder that was deleted or renamed: not the home folder instead (Auto can write there)
            return self.send(409, {"error": f"the folder is gone: {data.get('cwd')}"})
        mode = data.get("mode") if data.get("mode") in MODES else "plan"
        effort = data.get("effort") if data.get("effort") in EFFORTS else None
        before = {"items": [], "mode": None}
        if sid is not None:
            with suppress(OSError, ValueError):
                before = transcript(sid, cwd)
        with RUNS_LOCK:
            if sid in RUNS:
                return self.send(409, {"error": "this chat is already running a turn"})
            before.update(id=sid, cwd=cwd)
            run = RUNS[sid] = Run(sid, cwd, prompt, self.run_agent(sid, cwd, prompt, mode, effort), before)
        self.stream(run, 0)

    def watch(self, sid, start):
        """The page comes back to a running turn: its lines from `start` on, then the new ones."""
        with RUNS_LOCK:
            run = RUNS.get(sid)
        if not run:
            return self.send(404, {"error": "not running"})
        try:
            start = int(start)
        except ValueError:
            start = 0
        self.stream(run, start)

    def stream(self, run, start):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for line in run.follow(start):
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                          # the page left; the turn goes on and the page may come back

    def upload(self):
        """A file dropped or pasted into the input: saved under ~/.maxedcode-app/attachments/<sha256>/<name>,
        as the old window did, so the message can point the model at a read-only copy."""
        from urllib.parse import unquote
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= UPLOAD_LIMIT:      # an empty file is a file too
            return self.send(413, {"error": f"files up to {UPLOAD_LIMIT // 1_000_000} MB"})
        raw = self.rfile.read(n)
        name = Path(unquote(self.headers.get("X-File-Name") or "file")).name or "file"
        sha = hashlib.sha256(raw).hexdigest()
        target = ATTACHMENTS / sha[:2] / sha / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
        return self.send(200, {"path": str(target), "name": name, "size": n, "sha": sha})

    def pty_read(self, tid, offset):
        """A terminal's output from `offset` on, streamed as it comes (NDJSON {"at", "data"}; {"exit"} at the end)."""
        with RUNS_LOCK:
            term = PTYS.get(tid)
        if not term:
            return self.send(404, {"error": "no such terminal"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                offset, text, code = term["pty"].since(offset)
                if text:
                    self.line({"at": offset, "data": text})
                if code is not None:
                    return self.line({"exit": code})
                if tid not in PTYS:
                    return
                if not text:
                    self.line({"ping": 1})      # a dead connection shows up here and ends the loop
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def line(self, obj):
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        self.wfile.flush()

    def btw(self, data):
        """A side question after the chat's history; maxedcode answers and saves nothing."""
        sid, question = data.get("session"), str(data.get("question") or "").strip()
        if not valid_session(sid) or not question:
            return self.send(400, {"error": "a chat and a question are needed"})
        if sid in RUNS:
            return self.send(409, {"error": "busy"})   # one GPU: a side request would compete with the turn
        effort = data.get("effort") if data.get("effort") in EFFORTS else None
        # the chat's own folder (made again if a turn left it empty): the prefix-cache key and memory go by folder
        cwd = work_dir(data.get("cwd"), sid)
        proc = self.run_agent(sid, cwd, question, None, effort, btw=True)
        try:
            out, err = proc.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            return self.send(504, {"error": "no answer in 15 minutes"})
        finally:
            prune_chat_dir(cwd)
        for raw in out.decode("utf-8", "replace").splitlines():
            with suppress(ValueError):
                ev = json.loads(raw)
                if ev.get("type") == "result":
                    return self.send(200, {"text": str(ev.get("result") or "")})
        return self.send(500, {"error": (err.decode("utf-8", "replace").strip().splitlines() or ["no answer"])[-1]})


def pty_open(data):
    """A new terminal in the chat's folder: the user's shell on a pseudo console."""
    cwd = folder(ensure_dir(data.get("cwd")))
    tid = "t" + secrets.token_hex(4)
    term = Pty(cwd, int(data.get("cols") or 100), int(data.get("rows") or 30))
    with RUNS_LOCK:
        PTYS[tid] = {"pty": term, "cwd": cwd}
    return {"id": tid, "cwd": cwd}


def quit_when_idle(httpd):
    """The window closed: the server stays while a turn runs (reopening the window shows it, as the old
    window's host kept running), then exits once no page has come back for IDLE_GRACE seconds."""
    while True:
        time.sleep(2)
        if (QUIT_WHEN_IDLE.is_set() or QUIT_STICKY.is_set()) and not RUNS and \
                time.monotonic() - LAST_SEEN[0] > IDLE_GRACE:
            httpd.shutdown()
            return


def main(argv):
    if os.name != "nt" and "--serve" not in argv:
        # macOS / Linux have no exe to start the server and show the page: --app does both (webapp/native.py)
        from .native import launch
        sys.exit(launch(argv))
    port = 3077
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    if os.name == "nt":                  # the old layouts were Windows-only (Documents\maxedcode ...)
        with suppress(Exception):
            move_old_workspace()
        with suppress(Exception):
            move_shared_chats()
    print(f"maxedcode app: http://127.0.0.1:{port}/?t={TOKEN}", flush=True)
    with suppress(Exception):            # the daily look for a newer release, after the URL (the window waits for it)
        from .. import update
        update.check_async()
    try:
        threading.Thread(target=quit_when_idle, args=(httpd,), daemon=True).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with RUNS_LOCK:
            for run in list(RUNS.values()):
                kill_tree(run.proc)
            for term in list(PTYS.values()):
                term["pty"].close()


if __name__ == "__main__":
    main(sys.argv)
