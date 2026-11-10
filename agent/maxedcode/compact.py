"""Shared context-compression rules for the TUI loop and the headless run.

Both loops must follow the same rules, otherwise a long headless session either dies on context
overflow or rebuilds the cached prefix on every request:

  * the cached prefix (system + tool schemas + memory message) is never rewritten;
  * old tool results are never trimmed in place: they are in the server's KV cache, and any change
    in the middle of the prompt re-reads everything after it. Only the summary at COMPACT_AT compacts;
  * recompaction updates the existing <compact id="..."> anchor instead of stacking summaries;
  * fill % is the larger of the estimate and the last real measurement. The server number
    only describes the request that produced it: everything appended afterwards (tool results,
    steer messages) is not counted there, so a bar built from it alone lags behind reality and
    COMPACT_AT fires one request too late.

Constants taken from opencode (d2c4c38bbe), `packages/core/src/session/compaction.ts`: the
summarizer keeps its own reserve, so while the whole history fits inside `window - reserve` it is
sent as it is (the request prefix stays byte-identical and the provider cache still hits); only
when it does not fit does the text get flattened, shrinking step by step.
"""
import json
import os
import re
from contextlib import suppress

from . import config
from .shelltool import NAME as SHELL_TOOL
from .util import sent_reasoning, split_think, tok

ANCHOR_RE = re.compile(r'<compact id="([^"]+)"')

# opencode compaction thresholds: the summarizer's own room in the window.
RESERVE_SHARE = float(getattr(config, "RESERVE_SHARE", 0.15))
RESERVE_MIN = int(getattr(config, "RESERVE_MIN", 16_000))
SHRINK_STEPS = tuple(getattr(config, "SHRINK_STEPS", (0.7, 0.5, 0.35)))
TOOL_OUTPUT_MAX_CHARS = int(getattr(config, "TOOL_OUTPUT_MAX_CHARS", 1_250))

# Section template the summarizer is asked for (state.COMPACT_PROMPT). A weak/quantized model
# often answers with prose instead of sections; that summary cannot drive continuation, so the
# result is checked, and one nudge is given before the compaction is called a failure.
SUMMARY_HEADINGS = ("# Objective", "# Important Details", "# Work State", "# Next Move", "# Relevant Files")
LEGACY_HEADING = "## Additional Context"
NUDGE = ("The previous summary did not follow the section template. Rewrite it as those sections "
         "only, covering only what the user and the assistant said and did: no preamble, no setup "
         "instructions, no continuation of the task, no tool calls.")
MIDDLE_MARK = "\n…[middle omitted — the summarizer does not see it]…\n"

# A long autonomous turn fills the window again and again, so there is no fixed number of
# compactions per turn (two per turn used to be the cap: the third fill overflowed and the run died).
# Only a runaway turn is bounded; a failed summary is retried a few steps later instead of ending it.
try:
    MAX_PER_TURN = max(1, int(config._get("AGENT_COMPACT_MAX", "compact_max", 24)))
except (AttributeError, TypeError, ValueError):
    MAX_PER_TURN = 24
RETRY_STEPS = 2      # steps to wait after a failed summary (doubles with each failure in a row)


class Gate:
    """When auto-compaction may run inside one turn (TUI and headless share it).

    due() is asked once per model step. After a compaction at least one step goes by before the
    next one (the kept tail alone may stay above the threshold: compacting it again frees nothing);
    after a failure the wait grows, so a broken summarizer costs a few attempts, not one per step.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.done, self.failures, self.wait = 0, 0, 0

    def due(self, pct, at=None):
        if self.wait > 0:
            self.wait -= 1
            return False
        return pct >= (threshold() if at is None else at) and self.done < MAX_PER_TURN

    def ok(self):
        self.done += 1
        self.failures, self.wait = 0, 1

    def failed(self):
        """Record a failed attempt; returns how many failed in a row."""
        self.failures += 1
        self.wait = RETRY_STEPS * 2 ** min(self.failures - 1, 4)
        return self.failures


def reserve(window=None):
    """Tokens the summarizer request keeps for itself (its own prompt plus the summary output)."""
    w = int(window or config.CTX_WINDOW)
    # RESERVE_MIN (16k, opencode's) is sized for large cloud windows. On a local 16-32k window it left the
    # transcript ~1000 tokens after the prefix (16k: 3000 characters), so the summary lost the task's
    # details. A quarter of the window still covers the summary request (prompt, thinking, summary).
    r = max(min(RESERVE_MIN, w // 4), int(w * RESERVE_SHARE))
    return max(1000, min(r, w - 1000))


def filled(text):
    """True when the summary really contains section headings, not just prose."""
    t = str(text or "")
    return any(h in t for h in SUMMARY_HEADINGS) or LEGACY_HEADING in t


def fit_output(text, budget_chars):
    """Head/tail fit: the beginning and the end of a transcript matter, the middle is cut."""
    text = text or ""
    if len(text) <= budget_chars:
        return text
    head = min(3000, budget_chars // 3)
    tail = budget_chars - head - len(MIDDLE_MARK)
    if tail <= 0:
        return text[:max(0, budget_chars)]
    return text[:head] + MIDDLE_MARK + text[-tail:]


def media_bytes(msg):
    """Base64 payload size of one message's images (the server counts media, not text)."""
    c = msg.get("content")
    if not isinstance(c, list):
        return 0
    return sum(len((p.get("image_url") or {}).get("url") or "") for p in c if p.get("type") == "image_url")


def bound_images(msgs, head, trigger=None, target=None):
    """Return messages with the oldest images replaced by a stub, only when media bytes pass TRIGGER,
    cutting down to TARGET (opencode boundImages). Non-mutating: history stays append-only.

    A cut that moves on every request changes the prompt bytes and loses the cache hit. Keeping the
    cut inside the (trigger - target) band means the same images are dropped for many requests in a
    row, so the sent prefix stays byte-identical between them.
    """
    trigger = int(trigger or config.IMAGE_BYTES_TRIGGER)
    target = int(target or config.IMAGE_BYTES_TARGET)
    total = sum(media_bytes(m) for m in msgs[head:])
    if total <= trigger:
        return list(msgs), 0
    out, dropped = [], 0
    for m in msgs:
        if len(out) < head:
            out.append(m)
            continue
        c = m.get("content")
        if not isinstance(c, list) or total <= target:
            out.append(m)
            continue
        kept, dropped_here = [], False
        for p in c:
            if p.get("type") == "image_url" and total > target:
                total -= len((p.get("image_url") or {}).get("url") or "")
                dropped += 1
                dropped_here = True
                kept.append({"type": "text", "text": "[image dropped: over the media budget]"})
            else:
                kept.append(p)
        out.append({**m, "content": kept} if dropped_here else m)
    return out, dropped


def unsupported_parts(msgs, head, vision=True):
    """Media is replaced by a text stub for models that cannot read it (opencode unsupportedParts).

    Sending an image to a non-vision model costs tokens and produces a server error or garbage;
    a stable stub text keeps the prefix bytes unchanged from request to request.
    """
    if vision:
        return list(msgs), 0
    out, replaced = [], 0
    for i, m in enumerate(msgs):
        c = m.get("content")
        if i < head or not isinstance(c, list):
            out.append(m)
            continue
        kept, changed = [], False
        for p in c:
            if p.get("type") == "image_url":
                replaced += 1
                changed = True
                kept.append({"type": "text", "text": "[image omitted: this model cannot read media]"})
            else:
                kept.append(p)
        out.append({**m, "content": kept} if changed else m)
    return out, replaced


def usable(text):
    """Is the summary fit for continuation? Returns (ok, reason).

    A quantized model often keeps the headings but loses exactly the identifiers the template asks it
    to keep verbatim, so the check is not only "are there headings": a summary without a path or
    without Next Move cannot drive the next step.
    """
    t = str(text or "")
    if not filled(t):
        return False, "no section headings (prose instead of the template)"
    if "# Next Move" not in t:
        return False, "no # Next Move section"
    relevant = t.split("# Relevant Files", 1)[1] if "# Relevant Files" in t else ""
    if not re.search(r"[\w./\\-]+\.(py|md|json|txt|ts|js|csv|toml|yml|yaml|bat)", relevant):
        return False, "no path survived in # Relevant Files"
    return True, ""


def anchor(msgs):
    """Existing summary anchor, or None while the session has no <compact> message yet."""
    for m in msgs:
        if m.get("role") == "user":
            match = ANCHOR_RE.search(str(m.get("content") or ""))
            if match:
                return match.group(1)
    return None


def summary_text(m):
    """Readable summary of a <compact> anchor message (without the tags), "" for any other message."""
    c = m.get("content") if isinstance(m, dict) and m.get("role") == "user" else None
    if not isinstance(c, str) or not ANCHOR_RE.match(c):
        return ""
    return re.sub(r'^<compact id="[^"]*">\s*|\s*</compact>\s*$', "", c).strip()


def prefix_cost(system, tools, mem):
    """Tokens of the cached prefix: system + tool schemas + the memory message."""
    return tok(system) + tok(json.dumps(tools, ensure_ascii=False)) \
        + sum(tok(m.get("content") or "") for m in mem) + 40


def est_tokens(msgs, head, prefix):
    """Approximate size of the history part of the prompt."""
    n = prefix
    for m in msgs[head:]:
        c = m.get("content") or ""
        if isinstance(c, list):
            n += 1024 * sum(1 for p in c if p.get("type") == "image_url")
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        n += tok(c) + tok(sent_reasoning(m))
        n += sum(tok(t["function"].get("arguments", "")) for t in m.get("tool_calls", []))
        n += 4 + 2 * len(m.get("tool_calls", []))
    return n


def used_tokens(est, server_used, saved_tok, est_sent=0):
    """Tokens the next request will carry: the server's count for the last request plus what was
    appended since (the reply, tool results), corrected for what was freed afterwards.

    est_sent is the estimate of that last request. The character estimate falls behind the real
    count as a turn grows (X1 live run: 77% of the server number at 19k tokens: reasoning, escaped
    paths and template tokens), so max(est, server) alone was a whole step late and the request after
    it overflowed. The growth since the measurement is scaled by the measured/estimated ratio."""
    if not server_used:
        return est
    used = server_used - saved_tok
    if est_sent and est > est_sent:
        ratio = min(2.0, max(1.0, server_used / est_sent))
        used += int((est - est_sent) * ratio)
    return max(est, used)


def fill_pct(est, server_used, saved_tok, window, est_sent=0):
    """Fill as a percentage of the context window."""
    used = used_tokens(est, server_used, saved_tok, est_sent)
    return min(100, used * 100 // window) if window else 0


def cut_index(msgs, head, keep=None, budget=None):
    """Choose a compaction boundary, preserving system + memory and tool-call/result pairs."""
    budget = budget or config.COMPACT_KEEP_TOKENS
    if keep is None:
        used, i = 0, len(msgs)
        while i > head:
            i -= 1
            m = msgs[i]
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(x.get("text", "") for x in content if x.get("type") == "text")
            used += tok(content) + tok(sent_reasoning(m))
            used += sum(tok(c.get("function", {}).get("arguments", "")) for c in m.get("tool_calls", []))
            if used >= budget:
                break
        if used < budget:
            return None
    else:
        i = max(head, len(msgs) - max(1, int(keep)))
    while i > head and msgs[i].get("role") == "tool":
        i -= 1
    return i if i > head else None


def auto_cut(msgs, head, keep=None, budget=None):
    """cut_index, or the start of the last step when the whole history is smaller than the kept-tail
    budget. On a small window (16k: an 8k prefix leaves ~5k of history) the window was full while the
    history was still under COMPACT_KEEP_TOKENS, so there was "nothing to compress" and every attempt
    failed until the request had grown far past the window."""
    cut = cut_index(msgs, head, keep, budget)
    if cut is None and keep is None:
        last = step_cut(msgs, head + 1)
        cut = last if last > head + 1 else None
    return cut


def step_cut(msgs, cut):
    """Boundary after an in-place summary: only the last step (the last assistant message and what
    follows it) stays verbatim. That summarizer saw the whole history, the tail included, so a longer
    tail only repeats the summary, and every kept token is read again on the next request: the
    rebuilt history differs from the cached one right after the prefix. Never moves cut backwards."""
    last = next((i for i in range(len(msgs) - 1, cut - 1, -1) if msgs[i].get("role") == "assistant"), cut)
    return max(cut, last)


def without_tail(msgs, head):
    """History as it is, without the kept tail (the part the summarizer must not be shown twice)."""
    return msgs[head:len(msgs) - config.COMPACT_KEEP]


PATH_KEYS = ("path", "pattern", "command")
_PATH_RE = re.compile(r"""(?:[A-Za-z]:[\\/]|/|\b[\w.-]+/)[^\s:<>|]+\.(?:py|md|json|txt|ts|js|csv|toml|yml|yaml|bat|ps1|sh|log)""", re.I)


def _line_cut(text, cap):
    """Cut on line boundaries while keeping the leading context and a relevant path line."""
    text = str(text or "")
    if len(text) <= cap:
        return text
    lines = text.splitlines()
    if not lines:
        return text[:cap]

    selected = []
    used = 0
    # The first line frequently contains the file path, tool name, or operation summary.
    first = lines[0]
    if len(first) > cap and not (_PATH_RE.search(first) or re.search(r"\b(?:path|file|filename)\s*[:=]", first, re.I)):
        return first[:cap] + f"…[line cut: {len(text)} chars]"
    selected.append((0, first))
    used += len(first) + 1
    # Keep the first line that explicitly identifies a path/file, even if it is in the middle.
    path_idx = next((i for i, line in enumerate(lines[1:], 1)
                     if _PATH_RE.search(line) or re.search(r"\b(?:path|file|filename)\s*[:=]", line, re.I)), None)
    if path_idx is not None and used + len(lines[path_idx]) + 1 <= max(cap, len(lines[0]) + len(lines[path_idx]) + 2):
        selected.append((path_idx, lines[path_idx]))
        used += len(lines[path_idx]) + 1
    # Then keep the earliest remaining lines that fit.
    for i, line in enumerate(lines[1:], 1):
        if any(j == i for j, _ in selected):
            continue
        if used + len(line) + 1 > cap - 48:
            break
        selected.append((i, line))
        used += len(line) + 1
    selected.sort(key=lambda pair: pair[0])
    omitted = len(lines) - len(selected)
    out = "\n".join(line for _, line in selected)
    if omitted:
        out += f"\n…[{omitted} line(s) omitted; {len(text)} chars total]"
    return out


def _args_cap(name, args, cap):
    """Only path/pattern/command arguments receive the larger transcript budget."""
    try:
        parsed = json.loads(args) if isinstance(args, str) else args
    except (ValueError, TypeError):
        parsed = {}
    if isinstance(parsed, dict) and any(k in parsed for k in PATH_KEYS):
        return max(cap, int(TOOL_OUTPUT_MAX_CHARS))
    return cap


def _edge_cut(text, cap):
    """Head and tail of the output: for pytest, tracebacks and builds the key part (error, result) is at the end."""
    text = str(text or "")
    if len(text) <= cap:
        return text
    lines = text.splitlines()
    tail, used = [], 0
    for line in reversed(lines[1:]):
        if used + len(line) + 1 > cap // 2:
            break
        tail.append(line)
        used += len(line) + 1
    if not tail:
        return _line_cut(text, cap)
    tail.reverse()
    return _line_cut("\n".join(lines[:len(lines) - len(tail)]), cap - used) + "\n" + "\n".join(tail)


# Verbatim user requests: a weak model loses exactly these in a summary (constraints, "don't touch X").
REQUESTS_RE = re.compile(r"<user_requests>\n?(.*?)\n?</user_requests>", re.S)
_SERVICE_USER = re.compile(r"<(env|repo_map|direct_shell|compact|pending_work|btw|finish_tool_feedback|hook|"
                           r"thinking_cut|injected|tool_response)\b")
REQUEST_CAP, REQUESTS_CAP = 1200, 4000


def user_requests(msgs):
    """Real user messages from the compacted part, plus those already stored in the old anchor."""
    out = []
    for m in msgs:
        c = m.get("content") or ""
        if m.get("role") != "user":
            continue
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        c = str(c)
        if c.startswith("<compact"):
            prev = REQUESTS_RE.search(c)
            out += [x for x in prev.group(1).removeprefix("- ").split("\n- ") if x.strip()] if prev else []
            continue
        c = re.sub(r"<(edit_strategy|mode_notice)\b[^>]*>.*?</\1>", "", c, flags=re.S)
        c = re.sub(r"</?user_input[^>]*>", "", c).strip()
        if c and not _SERVICE_USER.match(c):
            out.append(c if len(c) <= REQUEST_CAP else c[:REQUEST_CAP] + "…")
    out = [x.replace("\n- ", "\n  ") for x in out]
    # The first request (the original goal) always stays; after that the most recent ones, while they fit.
    keep, used = [], 0
    for x in ([out[0]] + out[:0:-1]) if out else []:
        if used + len(x) > REQUESTS_CAP and keep:
            break
        keep.append(x)
        used += len(x)
    return keep[:1] + keep[:0:-1]


FILES_RE = re.compile(r"<files_touched>\n?(.*?)\n?</files_touched>", re.S)
_CHANGED = {"edit_file", "write_file", "patch", "apply_diff"}
FILES_CAP = 40


def _call_args(call):
    with suppress(Exception):
        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
        return args if isinstance(args, dict) else {}
    return {}


def files_touched(msgs):
    """Files read and changed in the compacted part, taken from the tool calls (and the previous anchor).

    The summarizer wrote "# Relevant Files" from memory; a small model left out files it had edited,
    then read them again or did not check them (Cline adds this list from the calls themselves)."""
    seen = {}
    for m in msgs:
        if m.get("role") == "user" and str(m.get("content") or "").startswith("<compact"):
            prev = FILES_RE.search(str(m["content"]))
            for line in (prev.group(1).splitlines() if prev else []):
                kind, _, path = line.partition(": ")
                if path:
                    seen[path] = "changed" if "changed" in (kind, seen.get(path)) else kind
        for call in m.get("tool_calls") or [] if m.get("role") == "assistant" else []:
            name = (call.get("function") or {}).get("name")
            path = _call_args(call).get("path")
            if not isinstance(path, str) or not path or name not in _CHANGED | {"read_file"}:
                continue
            with suppress(ValueError):
                rel = os.path.relpath(path)
                path = rel if not rel.startswith("..") else path
            kind = "changed" if name in _CHANGED or seen.get(path) == "changed" else "read"
            seen.pop(path, None)          # most recent last: the cap keeps the latest files
            seen[path] = kind
    items = list(seen.items())[-FILES_CAP:]
    return "\n".join(f"{kind}: {path}" for path, kind in items)


def anchor_body(summary, msgs):
    """Anchor text: summary + verbatim user requests (carried over from the previous anchor) + the files
    read/changed according to the tool calls."""
    summary = FILES_RE.sub("", REQUESTS_RE.sub("", str(summary or ""))).strip()
    reqs = user_requests(msgs)
    files = files_touched(msgs)
    if reqs:
        summary += "\n\n# User Requests (verbatim, oldest first)\n<user_requests>\n- " + "\n- ".join(reqs) + "\n</user_requests>"
    if files:
        summary += "\n\n# Files (from the tool calls)\n<files_touched>\n" + files + "\n</files_touched>"
    return summary


def basic_summary(msgs, keep_replies=3, reply_cap=1500):
    """A summary built without the model, when the summarizer failed and the context is over the limit.

    Recovery must not depend on another successful request to the same overloaded server (Cline's basic
    compaction): the user requests and files come from anchor_body, here the commands run and the last
    replies verbatim."""
    commands, replies = [], []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            cmd = _call_args(call).get("command") if name == SHELL_TOOL else None
            if isinstance(cmd, str) and cmd.strip():
                commands.append(" ".join(cmd.split())[:200])
        text = re.sub(r"(?s)<think>.*?</think>", "", str(m.get("content") or "")).strip()
        if text:
            replies.append(text if len(text) <= reply_cap else text[:reply_cap] + " …")
    parts = ["# Objective\nThe automatic summary failed; this record was built from the history itself. "
             "The user requests below are the task.",
             "# Work State\nCommands run (latest last):\n" + ("\n".join(f"- {c}" for c in commands[-12:]) or "(none)"),
             "# Important Details\nYour last replies, verbatim:\n" + ("\n\n".join(replies[-keep_replies:]) or "(none)"),
             "# Next Move\nCheck the files listed below if needed, then continue the task from the last reply.",
             "# Relevant Files\nSee the list below."]
    return "\n\n".join(parts)


# In-place summary: the same messages the conversation already sent + one request at the end. The
# server reuses its KV cache, so only the request and the summary are computed, and the summarizer
# sees the whole history instead of a 20k-character transcript with the middle cut out.
INPLACE_ROOM = max(1500, int(getattr(config, "COMPACT_INPLACE_ROOM", 3000)))   # request + thinking + summary
INPLACE_THINK = 1024


def inplace_fits(est, window=None):
    return est + INPLACE_ROOM <= int(window or config.CTX_WINDOW)


def threshold(window=None):
    """Fill % at which auto-compact starts: COMPACT_AT, or earlier on a small window, so that the
    in-place summary still fits. That summary is the cheap one (a KV-cache hit that sees the whole
    history and keeps only the last step); on a 16-32k window it never fitted at 90%, the transcript
    fallback kept ~6000 tokens of tail, and the server re-read them after every compaction (X1 at a
    16k window: 87 s for one request)."""
    w = int(window or config.CTX_WINDOW)
    if w <= 0:
        return config.COMPACT_AT
    room = (w - INPLACE_ROOM - 512) * 100 // w
    return max(50, min(config.COMPACT_AT, room))


def inplace_request(prompt):
    return {"role": "user", "content": "<compact_request>\n" + prompt +
            "\n\nDo not call any tools and do not continue the task: reply with the summary only.\n</compact_request>"}


def transcript_request(prompt, tr):
    """Transcript summary asked after the conversation's own prefix (system + memory + env).

    A standalone summarizer request (its own system prompt, effort "low") shares nothing with the
    conversation, and the server keeps one conversation: it drops the session's checkpoints, so the
    first request after the compaction reads system + tools + memory again from token 0. Asked after
    the same prefix, with the same tools and effort, the summary reuses it and leaves it cached.
    """
    return inplace_request(prompt + "\n\nConversation so far (transcript):\n<transcript>\n" + tr + "\n</transcript>")


def summary_body(extra, head):
    """Request body of a summary: the conversation's own body (effort is rendered into the system turn,
    so it must not change) with a small thinking budget, and the end of the cached prefix pinned.
    The rebuilt history (prefix + anchor + kept tail) starts exactly there, so the request after the
    compaction reads only the anchor and the tail instead of memory and the environment block too."""
    from .cachekey import pin
    return pin({**extra, "reasoning_budget_tokens": INPLACE_THINK}, head)


def transcript(msgs, window=None, reserve_tok=None, shrink=1.0):
    """Plain-text transcript for the summarizer, capped so the summarizer request itself fits.

    The cache-safe prefix is untouched. Long messages are cut by lines, and path-bearing tool
    arguments get a larger cap so the summarizer can retain file and command identifiers.
    Tool output keeps its head and tail; an earlier summary is passed whole, otherwise each
    recompaction would cut what the previous ones kept.
    """
    parts = []
    cap_line = max(200, int(900 * shrink))
    cap_args = max(200, int(TOOL_OUTPUT_MAX_CHARS * shrink))
    for m in msgs:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        _, c = split_think(c)
        calls = "; ".join(t["function"]["name"] + "(" + _line_cut(
            t["function"].get("arguments", ""),
            _args_cap(t["function"]["name"], t["function"].get("arguments", ""), cap_args)) + ")"
            for t in m.get("tool_calls", []))
        body = c.strip()
        if m.get("role") == "user" and body.startswith("<compact"):
            keep = body[:max(cap_line, int(8000 * shrink))]
        elif m.get("role") == "tool":
            keep = _edge_cut(body, cap_line)
        else:
            keep = _line_cut(body, cap_line)
        line = f"{m['role']}: {keep}" + (f" -> {calls}" if calls else "")
        if line.strip():
            parts.append(line)
    # The earlier summary goes before the head/tail fit, otherwise fit_output keeps only its first 3000 chars.
    lead = "\n".join(p for p in parts if p.startswith("user: <compact"))
    text = "\n".join(p for p in parts if not p.startswith("user: <compact"))
    w = int(window or config.CTX_WINDOW)
    room = max(1000, w - int(reserve_tok or 0))
    budget = min(config.COMPACT_MAX_CHARS, max(3000, room * 3))
    for step in (1.0,) + SHRINK_STEPS:
        cut = fit_output(text, max(1000, int(budget * step) - len(lead)))
        cut = lead + "\n" + cut if lead else cut
        if tok(cut) <= max(1, int(room * step)) or step == SHRINK_STEPS[-1]:
            return cut
    return cut
