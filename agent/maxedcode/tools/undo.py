"""Checkpoints for /undo: before edit_file/write_file the previous file content is saved,
/undo restores every change of the last turn. Stored in HOME/checkpoints/<sid>/."""
import hashlib
import json
import shutil
import time
import uuid
from contextlib import suppress
from pathlib import Path
from ..config import HOME

CKPT = HOME / "checkpoints"
MAX = 50          # how many checkpoints to keep per session
KEEP_DAYS = 14    # checkpoint folders of sessions untouched for longer are removed
sid = "default"   # headless never calls set_sid; an empty sid wrote straight into CKPT
_CURRENT_MESSAGE_ID = None
_CURRENT_TURN = None

def set_sid(s):
    global sid
    sid = s or "default"


def set_message_id(message_id):
    """Attach the active model tool-call id to any checkpoint created in this dispatch."""
    global _CURRENT_MESSAGE_ID
    _CURRENT_MESSAGE_ID = str(message_id) if message_id else None


def set_turn(turn_id):
    """Group every checkpoint of one user turn: /undo reverts the whole step, not one edit of it."""
    global _CURRENT_TURN
    _CURRENT_TURN = str(turn_id) if turn_id else None

def _dir():
    d = CKPT / sid
    d.mkdir(parents=True, exist_ok=True)
    return d

def snap(path, old, new=False, message_id=None, turn=None):
    """old — the file content BEFORE the change (str, or bytes to keep CRLF and encoding exactly).
    new=True: the file didn't exist before — on /undo and /rewind it is deleted."""
    d = _dir()
    name = None
    if not new:
        h = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
        # a random part too: time_ns() ticks every ~15 ms on some Windows clocks, and two snaps of one file in
        # one tick overwrote the older copy (only the newest /undo step could be taken back)
        name = f"{h}.{time.time_ns()}.{uuid.uuid4().hex[:8]}.bak"
        with suppress(Exception):
            (d / name).write_bytes(old if isinstance(old, bytes) else (old or "").encode("utf-8"))
    idx = d / "index.jsonl"
    with open(idx, "a", encoding="utf-8") as f:
        f.write(json.dumps({"path": str(path), "bak": name, "new": bool(new), "ts": time.time(),
                            "message_id": str(message_id or _CURRENT_MESSAGE_ID or ""),
                            "turn": str(turn or _CURRENT_TURN or "")}, ensure_ascii=False) + "\n")
    with suppress(Exception):
        lines = idx.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX:
            for l in lines[:-MAX]:
                with suppress(Exception):
                    b = json.loads(l).get("bak")
                    if b:
                        (d / b).unlink()
            idx.write_text("\n".join(lines[-MAX:]) + "\n", encoding="utf-8")

def _restore(rec, d):
    """Restore one record: put the old text back, or delete a file that the agent created."""
    p = Path(rec["path"])
    if rec.get("new"):
        if p.exists():
            p.unlink()
        return True
    bak = d / rec["bak"]
    if not bak.exists():
        return False
    p.write_bytes(bak.read_bytes())
    with suppress(Exception):
        bak.unlink()
    return True

def undo_last():
    """Restores the last checkpoint. Returns the file path or None."""
    d = CKPT / sid
    lines = []
    with suppress(Exception):
        lines = (d / "index.jsonl").read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    rec = json.loads(lines[-1])
    if not _restore(rec, d):
        return None
    with suppress(Exception):
        (d / "index.jsonl").write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""), encoding="utf-8")
    return rec["path"]

def _records():
    with suppress(Exception):
        return [json.loads(line) for line in (CKPT / sid / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    return []


def _group_start(recs):
    """Index of the first record of the latest step: same turn, else same tool call, else the last record."""
    last = recs[-1]
    for key in ("turn", "message_id"):
        if last.get(key):
            i = len(recs) - 1
            while i > 0 and recs[i - 1].get(key) == last[key]:
                i -= 1
            return i
    return len(recs) - 1


def last_group():
    """Records the next /undo reverts, oldest first."""
    recs = _records()
    return recs[_group_start(recs):] if recs else []


def undo_step():
    """Revert every file change of the latest turn, newest first. Returns the restored paths."""
    recs = _records()
    if not recs:
        return []
    start, d, done = _group_start(recs), CKPT / sid, []
    for rec in reversed(recs[start:]):
        with suppress(Exception):
            if _restore(rec, d) and rec["path"] not in done:
                done.append(rec["path"])
    with suppress(Exception):
        (d / "index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs[:start]),
                                       encoding="utf-8")
    return done[::-1]


def prune(keep=()):
    """Drop checkpoint folders untouched for KEEP_DAYS, and stray files the unnamed headless sid left in CKPT."""
    cutoff = time.time() - KEEP_DAYS * 86400
    with suppress(OSError):
        for d in CKPT.iterdir():
            with suppress(OSError):
                if d.is_file():
                    d.unlink()
                elif d.name not in keep and max(d.stat().st_mtime, (d / "index.jsonl").stat().st_mtime
                                                if (d / "index.jsonl").exists() else 0) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)


def checkpoint_latest_changes_count():
    """Number of path changes in the latest logical tool-call checkpoint group."""
    d = CKPT / sid
    with suppress(Exception):
        recs = [json.loads(line) for line in (d / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        if not recs:
            return 0
        latest_id = recs[-1].get("message_id")
        if latest_id:
            return sum(1 for rec in recs if rec.get("message_id") == latest_id)
        latest_ts = recs[-1].get("ts")
        return sum(1 for rec in recs if rec.get("ts") == latest_ts)
    return 0


def rewind_after(call_ids, session):
    """Undo, newest first, every checkpoint of `session` made after the last one whose tool call is in
    `call_ids` (all of them when none is): the window's "fork from here" with files, as the TUI's /rewind.
    Records without a call id (a /redo) are cut by position. -> the restored paths."""
    d, recs = CKPT / (session or "default"), []
    with suppress(Exception):
        recs = [json.loads(line) for line in (d / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    kept = {str(x) for x in call_ids if x}
    boundary = max((i for i, rec in enumerate(recs) if str(rec.get("message_id") or "") in kept), default=-1)
    done = []
    for rec in reversed(recs[boundary + 1:]):
        with suppress(Exception):
            if _restore(rec, d) and rec["path"] not in done:
                done.append(rec["path"])
    if boundary + 1 < len(recs):
        with suppress(Exception):     # consumed: a second rewind or /undo must not replay them
            (d / "index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs[:boundary + 1]),
                                           encoding="utf-8")
    return done[::-1]


def rewind(ts):
    """Restore checkpoints after a timestamp or after a model-message boundary.

    Numeric arguments retain the historical timestamp behavior. A string is treated as a
    message id; the matching record stays applied and later records are undone in reverse order.
    """
    d = CKPT / sid
    recs = []
    with suppress(Exception):
        recs = [json.loads(l) for l in (d / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    if not recs:
        return 0
    if isinstance(ts, str):
        matches = [i for i, rec in enumerate(recs) if str(rec.get("message_id") or "") == ts]
        if not matches:
            return 0
        # A single model tool call may write several files and therefore create several records.
        # Keep the entire checkpoint group, not only its first record.
        boundary = matches[-1]
        rollback = recs[boundary + 1:]
        keep = recs[:boundary + 1]
    else:
        try:
            cutoff = float(ts)
        except (TypeError, ValueError):
            return 0
        rollback = [r for r in recs if float(r.get("ts", 0)) > cutoff]
        keep = [r for r in recs if float(r.get("ts", 0)) <= cutoff]
    n = 0
    for rec in reversed(rollback):
        with suppress(Exception):
            n += bool(_restore(rec, d))
    # Consume reverted records: a second /rewind must not replay them.
    with suppress(Exception):
        (d / "index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    return n
