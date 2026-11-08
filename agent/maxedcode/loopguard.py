"""Cuts wasted tool calls WITHOUT capping productive work.

There is no limit on the number of calls. A call is only stopped or flagged when it is provably
useless: an exact repeat of a read that nothing has invalidated, a repeated successful command, or a
streak of calls whose output is identical to something the model has already seen.
Any edit/write, or any command that may change state, resets the streak.
"""
import hashlib
import json
import os
import re

CACHEABLE = {"read_file", "grep", "web_search", "fetch_url", "py_source"}   # pure reads
MUTATING = {"edit_file", "write_file", "patch", "apply_diff"}
RUN = {"powershell", "py_run"}
NEUTRAL = {"todo_write", "ask_user", "exit_plan_mode", "finish", "tool_search"}                      # never counted, never blocked
NUDGE_AT = 3      # consecutive calls with nothing new -> tell the model to stop searching
RUN_NUDGE_AT = 2   # failed execution retries should stop sooner
BLOCK_AT = 6      # ...and keep refusing further non-mutating calls until something changes
MAX_EMPTY_SEARCHES = 2  # one initial search + at most one narrow retry when nothing matches
EMPTY_SEARCH_TOOLS = {"grep", "web_search"}
# Soft hint only: it never refuses a call (a weak or quantized model may genuinely need many reads).
EXPLORE = {"read_file", "grep", "py_source"}   # local reading; web research is not counted
EXPLORE_NUDGE_AT = 8    # reads/searches in a row without a successful edit -> one short reminder
EXPLORE_REPEAT = 6      # ...then again every N more
# Soft hint only: web research that does not stop once the answer is in hand (real winapi_const run: 13
# searches and pages for three constants the first Microsoft page already had, 445 s). Local reads in
# between do not end the streak; writing or running something does.
WEB = {"web_search", "fetch_url"}
WEB_NUDGE_AT = 5
WEB_REPEAT = 4
# The same error from failed commands with different text around it (char position, probe file name):
# the experiments are not narrowing the cause. Reads stay allowed — reading the failing code is the way out.
ERR_NUDGE_AT = 3   # same error N times -> stop probing, read the code on the failing path
ERR_BLOCK_AT = 5   # ...then refuse further commands until a real edit; reads and edits stay allowed
# Soft hint only: many commands in a row without an edit (diagnosis by trial and error).
RUN_STREAK_NUDGE_AT = 15
RUN_STREAK_REPEAT = 10
# Soft hint only: checking work the model itself wrote in this turn, again and again, without changing it (real LCB
# run: the solution was final at step 2, then 12 runs of re-sent brute-force checks, 430 s). Sooner than the
# diagnosis streak above, because the code is the model's own and the examples already decide it.
VERIFY_NUDGE_AT = 4
VERIFY_REPEAT = 3
# ...and the hard stop: this many commands after the model's own edit without changing anything, and further
# commands are refused until an edit (reads and edits stay allowed). Real session: a one-constant change
# (FRAME_T 3 -> 1) followed by 7 checks and relaunches, 10 steps for a 2-step job.
VERIFY_BLOCK_AT = 8
# Soft hint only: steps that each read or searched ONE thing. Every step is a full model pass (16-19 s on the
# local model); real runs: 49 of 50 steps with a single call, 25 single reads before the first edit.
SERIAL_NUDGE_AT = 3
SERIAL_REPEAT = 5
# Soft hint only: a program the script needs is not installed, and the model keeps hunting for it (real S8 run:
# 6 web searches, 6 pages, then the missing compiler swapped for another one inside build.ps1).
# The CategoryInfo line is not translated, so it works on a Windows in any display language.
MISSING_RE = re.compile(r"The term '([^']+)' is not recognized as the name of a cmdlet"
                        r"|'([^']+)' is not recognized as an internal or external command"
                        r"|Имя \"([^\"]+)\" не распознано"
                        r"|ObjectNotFound: \(([^:()]+):String\) \[\], CommandNotFoundException")
MISSING_NUDGE_AT = 4    # lookups after the "not recognized" error -> stop looking, report it
MISSING_REPEAT = 4
# A real program name; mangled batch text ("'01' is not recognized" from a UTF-8 .bat) is not one.
PROGRAM_RE = re.compile(r"[\w.+-]*[A-Za-z][\w.+-]*", re.A)
# A script file is not a program to install: "'start.bat' is not recognized" means a wrong folder or a missing
# .\ prefix (real T1 run: the hint fired on the very start.bat being edited and repeated for 20 calls).
SCRIPT_RE = re.compile(r".+\.(?:bat|cmd|ps1|psm1|py|pyw|sh|vbs|js)", re.I)
# A loop: the same probe (digits ignored, so zz20.bat and zz21.bat are one probe) giving the same
# result again and again. Real session: 60 runs alternating two probes; each run succeeded, so the
# stale streak never grew and nothing stopped it. The repeats are cut from the history (a small model
# copies its own last steps), the probe is refused from then on, and every other call stays allowed.
LOOP_WINDOW = 12   # recent calls checked
LOOP_AT = 3        # the same probe this many times with one result -> loop
# Repeats are cut only from the last few steps. A hybrid model (Qwen3-Next on Strata) resumes only from a saved
# checkpoint, and the server keeps 4-6 of them, at its latest requests: cutting an older step re-reads the whole
# history from the system prompt (live LCB 3794: 42k tokens, 285 s for one step). The probe is refused anyway.
COLLAPSE_RECENT = 3
LOOP_TOOLS = RUN | CACHEABLE | {"code_mode"}
_WAITING = re.compile(r"Start-Sleep|\bsleep\b|Wait-(?:Process|Job|Event)|timeout\s+/t", re.I)   # polling is not a loop
_DIGITS = re.compile(r"\d+")
_NOOP = re.compile(r"nothing changed on disk|byte-for-byte the same as before")   # an edit that changed nothing
_ERR_WORD = re.compile(r"error|exception|cannot|failed|not found|denied|unexpected|missing", re.I)
_EXC_LINE = re.compile(r"^[\w.]*(?:Error|Exception)\b")
# Successful, fully visible read_file outputs contain this line-range header. Oversized outputs
# are saved to temp files and shown as previews, so those must not count as visible coverage.
READ_RANGE_RE = re.compile(r"\[.*?: lines (\d+)-(\d+) of (\d+)\.", re.S)


def _visible_output_limit():
    # Use the same effective setting as normalize_tool_output, including config-file overrides.
    try:
        from .config import TOOL_OUTPUT_LIMIT
        return max(2000, int(TOOL_OUTPUT_LIMIT))
    except (ImportError, TypeError, ValueError):
        try:
            return max(2000, int(os.environ.get("AGENT_TOOL_OUTPUT_LIMIT", "12000")))
        except (TypeError, ValueError):
            return 12000


def _path_key(value):
    return os.path.normcase(str(value or "").replace("\\", "/"))


def _requested_read_range(args, total=None):
    """Calculate the effective line range using the same defaults as the read_file tool."""
    try:
        if args.get("start_line") is not None:
            start = max(1, int(args["start_line"]))
        elif args.get("offset") is not None:
            start = max(1, int(args["offset"]) + 1)
        else:
            start = 1
        limit = max(1, min(2000, int(args.get("limit") or 2000)))
        end = start + limit - 1
        if args.get("end_line") is not None:
            end = min(end, int(args["end_line"]))
        if total is not None:
            end = min(end, total)
        return (start, max(start, end))
    except (TypeError, ValueError):
        return None


def _empty_search_result(name, output):
    """Detect the tool's explicit no-match response, not arbitrary prose mentioning no results."""
    text = str(output or "").strip().lower()
    if name == "grep":
        return text == "no matches." or text.startswith("error: path not found:")
    if name == "web_search":
        return text in {"nothing found.", "no results.", "no results found."} or text.startswith("nothing found.")
    return False


def _error_sig(out):
    """Error key without noise (paths, positions, numbers): the same failure from different commands gives one key."""
    lines = [x.strip() for x in str(out or "").splitlines() if x.strip()]
    if any(x.startswith("Traceback") for x in lines):
        lines = [x for x in reversed(lines) if _EXC_LINE.match(x)] or lines
    for s in lines:
        if s.startswith(("Error: PowerShell command exited", "Error: Python process exited", "At ", "+ ", "[error origin]")):
            continue
        if s.startswith(("CategoryInfo", "FullyQualifiedErrorId")) or not _ERR_WORD.search(s):
            continue
        s = re.sub(r"^.*\.(?:ps1|psm1|py)\s+:\s+", "", s)        # "D:\x\run.ps1 : Cannot ..." -> "Cannot ..."
        s = re.sub(r"[A-Za-z]:[\\/][^'\"]*?(?=['\"\s,:]|$)", "<path>", s)
        return re.sub(r"\d+", "N", s)[:200].lower()
    return ""


def _stamp(path):
    """What a file is on disk now (mtime, size): a read stays valid while this does not change."""
    try:
        st = os.stat(str(path or ""))
        return (st.st_mtime_ns, st.st_size)
    except (OSError, ValueError):
        return None


def _assignment(prog, a, out):
    """'OK = ...' without $ is a PowerShell typo for a variable, not a missing program (real session: the
    missing-program hint fired on it). The failing line is in the command and echoed as '+ OK = ...'."""
    pattern = rf"^\s*\+?\s*{re.escape(prog)}\s*(?:\[[^\]\n]*\])?\s*[-+*/]?=(?!=)"
    return bool(re.search(pattern, str((a or {}).get("command") or ""), re.M)
                or re.search(pattern, str(out or ""), re.M))


def _canon(a):
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(a)


def _fp(out):
    return hashlib.sha1((out or "")[:4000].encode("utf-8", "replace")).hexdigest()


def shape(name, a):
    """Call key; for commands numbers and spacing are ignored: probes that differ only by a counter are one probe.
    Reads keep their numbers (f1.py and f2.py are different files)."""
    key = " ".join(_canon(a).split())
    return name + ":" + (_DIGITS.sub("N", key) if name in RUN or name == "code_mode" else key)


def _may_change_state(name, a):
    if name == "py_run":
        return True
    if name != "powershell":
        return False
    from .tools import ps_readonly
    return not ps_readonly(str(a.get("command") or ""))


def _call_shape(call):
    fn = call.get("function") or {}
    try:
        a = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        a = fn.get("arguments")
    return shape(fn.get("name"), a)


def collapse(msgs, shapes):
    """Cut the repeats of looped probes from the current turn, keeping the first and the last one of each.

    Only whole steps go: an assistant message whose every call is such a repeat, with its tool results.
    Results carrying side-channel context (folder docs, rules) stay. Only the last COLLAPSE_RECENT steps are
    cut (the server's cache can resume there cheaply); older repeats stay. Returns the removed indices.
    """
    if not shapes:
        return []
    start = next((i + 1 for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), 0)
    recent = {i for i in range(start, len(msgs)) if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")}
    recent = set(sorted(recent)[-COLLAPSE_RECENT:])
    steps = {}   # shape -> [(assistant index, [its tool result indices])]
    for i in range(start, len(msgs)):
        m = msgs[i]
        calls = m.get("tool_calls") or []
        if m.get("role") != "assistant" or not calls:
            continue
        keys = {_call_shape(c) for c in calls}
        if len(keys) != 1 or not keys <= shapes:
            continue
        ids = {c.get("id") for c in calls}
        results = [j for j in range(i + 1, len(msgs)) if msgs[j].get("role") == "tool" and msgs[j].get("tool_call_id") in ids]
        if len(results) != len(ids) or any("<injected>" in str(msgs[j].get("content")) for j in results):
            continue
        steps.setdefault(keys.pop(), []).append((i, results))
    removed = set()
    for found in steps.values():
        for i, results in found[1:-1]:
            if i in recent:
                removed.add(i)
                removed.update(results)
    removed = sorted(removed)
    for i in reversed(removed):
        del msgs[i]
    return removed


class LoopGuard:
    def __init__(self):
        self.reset()

    def reset(self):
        self.n = 0
        self.seen = {}          # (name, args) -> call number; valid until state may have changed
        self.fps = set()        # fingerprints of outputs already shown to the model
        self.last = None        # (key, ok) of the previous run-type call
        self.stale = 0          # consecutive calls that returned nothing new
        self.empty_searches = 0 # consecutive searches with an explicit no-match result
        self.explore = 0        # reads/searches since the last successful edit
        self.web = 0            # web searches/pages since the last write or run
        # path -> (total file lines, ranges fully shown to the model, the file's (mtime, size) then)
        self.read_ranges = {}
        self.batch = 1          # calls in the current step (set by the caller before running them)
        self.serial = 0         # steps in a row that read or searched one thing
        self.errors = {}        # error signature -> times seen since the last real edit
        self.blocked_err = ""   # signature that reached ERR_BLOCK_AT: commands are refused until an edit
        self.runs = 0           # commands since the last successful edit
        self.wrote = False      # the model wrote or edited a file in this turn: later runs check its own work
        self._new_file = False  # write_file target did not exist: a scratch/probe file, not a fix
        self.missing = ""       # program from the last "is not recognized" error
        self.hunt = 0           # calls since then while it is still missing
        self.missing_edited = False
        self.recent = []        # (probe shape, result signature or None for a refused duplicate), newest last
        self.banned = {}        # probe shape -> times it ran with one result; refused until a real edit
        self.collapse = set()   # looped shapes whose repeats the caller cuts from the history (collapse())

    def _repeat(self, name, a, out, probe=False):
        """Record a probe; on a loop return the notice (and mark it for collapse), else ''."""
        if not (probe or name in LOOP_TOOLS) or (name in RUN and _WAITING.search(str(a.get("command") or a.get("code") or ""))):
            return ""
        key = shape(name, a)
        sig = None if out is None else _fp(str(out))
        self.recent = (self.recent + [(key, sig)])[-LOOP_WINDOW:]
        hits = [s for k, s in self.recent if k == key]
        if len(hits) == 1 and out is not None and _may_change_state(name, a):
            self.banned.clear()     # a new command that may change state: the refused probes may answer differently now
        if len(hits) < LOOP_AT or len({s for s in hits if s is not None}) > 1 or key in self.banned:
            return ""
        self.banned[key] = len(hits)
        self.collapse.add(key)
        what = str(a.get("command") or a.get("path") or a.get("query") or a.get("code") or _canon(a))
        what = " ".join(what.split())
        return (f"\n[CLI] Loop: this probe (`{what[:160]}`) has now given the same result {len(hits)} times "
                "(numbers in it ignored). The repeats were removed from the history and it will be refused from now "
                "on: running it again cannot tell you anything new. Do not stop - continue the task another way: "
                "use what this result already shows, read the code on the failing path, make the edit, or answer. "
                "If the user wrote to you meanwhile, answer them first.")

    def forget_reads(self):
        """The history was compacted: what the model read before is gone from its context, so it may read again."""
        self.read_ranges.clear()
        self.seen.clear()
        self.fps.clear()
        self.stale = 0

    def _tail(self, name=None):
        threshold = RUN_NUDGE_AT if name in RUN else NUDGE_AT
        if self.stale >= threshold:
            return (f"\n[CLI] {self.stale} consecutive calls returned nothing new. Answer now from what you already have; "
                    "call another tool only if the answer is impossible without it, or ask the user.")
        return ""

    def before(self, name, a):
        """Return a replacement result if the call must not run, else None."""
        if name in MUTATING:
            self._new_file = name == "write_file" and not os.path.exists(str(a.get("path") or ""))
        if name in NEUTRAL or name in MUTATING:
            return None

        if shape(name, a) in self.banned:
            self.collapse.add(shape(name, a))   # refused repeats are cut from the history too
            return (f"Blocked: this probe already gave the same result {self.banned[shape(name, a)]} times (numbers in it "
                    "ignored); it is refused until a real edit. Other calls are allowed: continue the task another way, "
                    "or answer with what the results already show.")

        if name in RUN and self.wrote and self.runs >= VERIFY_BLOCK_AT - 1:
            return (f"Blocked: {self.runs} commands since your last change, and nothing was changed. The checks are "
                    "done: answer now with what they showed (and what is still unverified). If a check showed a real "
                    "problem, fix it with an edit first; reads and edits stay allowed.")

        if name in RUN and self.blocked_err:
            return (f"Blocked: the same error came back {self.errors.get(self.blocked_err, 0)} times "
                    f"(\"{self.blocked_err[:120]}\"); more runs will not explain it. Either read the code that produces it "
                    "(the script and every file it imports or dot-sources) and fix it, or answer the user now: "
                    "the exact error, what you ruled out, and the most likely cause.")

        # After one alternate narrow search also returns no results, stop broadening blindly.
        if name in EMPTY_SEARCH_TOOLS and self.empty_searches >= MAX_EMPTY_SEARCHES:
            return (f"Blocked: {self.empty_searches} consecutive {name} searches returned no matches. "
                    "Stop searching; report that the symbol/page was not found or ask the user for a more specific target.")

        # Block before duplicate handling; otherwise the duplicate branch can bypass BLOCK_AT.
        if self.stale >= BLOCK_AT:
            return (f"Blocked: {self.stale} consecutive calls returned nothing new. Change the approach: "
                    "act on the evidence already available, edit something, answer, or ask the user.")

        # Block a narrower read only when its complete requested range was returned visibly
        # before. Large/truncated results are deliberately not recorded as visible coverage.
        if name == "read_file":
            path = _path_key(a.get("path"))
            known = self.read_ranges.get(path)
            if known and known[2] != _stamp(a.get("path")):   # changed on disk since (any tool, any program)
                self.read_ranges.pop(path, None)
                known = None
            if known:
                total, ranges, _ = known
                wanted = _requested_read_range(a, total)
                if wanted and any(left <= wanted[0] and right >= wanted[1] for left, right in ranges):
                    self.stale += 1
                    notice = (f"Already read: {a.get('path')} lines {wanted[0]}-{wanted[1]} were included in a prior visible "
                              "result, and the file has not changed on disk since. Use the content already in the "
                              "conversation.")
                    if self.stale >= BLOCK_AT:
                        return (f"Blocked: {self.stale} consecutive calls returned nothing new. "
                                "Use the evidence already available, make the change, or answer.")
                    return notice + self._tail(name)

        key = (name, _canon(a))
        if name in CACHEABLE and key in self.seen:
            self.stale += 1
            notice = (f"Duplicate call: the identical {name} already ran (call #{self.seen[key]}) and nothing "
                      "has changed since. Use that result.")
        elif name in RUN and self.last and self.last[0] == key and self.last[1]:
            self.stale += 1
            notice = (f"Duplicate call: this exact {name} just ran successfully and nothing has changed since. "
                      "Use its result.")
        else:
            return None
        loop = self._repeat(name, a, None)      # a refused duplicate is one more turn of the same loop
        if loop:
            return notice + loop
        # A duplicate is itself one of the wasted calls, so it must not answer with a notice once the
        # streak reaches BLOCK_AT; otherwise the duplicate branch bypasses the limit by one call.
        if self.stale >= BLOCK_AT:
            return (f"Blocked: {self.stale} consecutive calls returned nothing new. Change the approach: "
                    "edit something, answer with what you have, or ask the user.")
        return notice + self._tail(name)

    def after(self, name, a, out, ok=True):
        """Record a finished call. Returns text to append to its result ('' if nothing)."""
        if name in MUTATING and ok and _NOOP.search(str(out or "")):
            # Rewriting a file with the content it already has changes nothing: it must not lift a block or
            # reset a streak (real session: 45 rounds of "same error -> same write -> same run").
            self._new_file = False
            return self._repeat(name, a, out, probe=True)
        tail = self._after(name, a, out, ok)
        if name in NEUTRAL:
            return tail
        tail += self._missing(name, a, out, ok)
        tail += self._repeat(name, a, out)
        if name in MUTATING and ok:
            self.explore = 0
            self.runs = 0
            self.wrote = True
            if not (name == "write_file" and self._new_file):   # a real fix: the old error may be gone now
                self.errors.clear()
                self.blocked_err = ""
                self.recent.clear()
                self.banned.clear()
        if name in MUTATING or name in RUN:
            self.web = 0
        if name in MUTATING:
            self._new_file = False
        elif name in RUN:
            tail += self._run_streak(out, ok)
        elif name in WEB:
            self.web += 1
            over = self.web - WEB_NUDGE_AT
            if over >= 0 and over % WEB_REPEAT == 0:
                tail += (f"\n[CLI] {self.web} web searches/pages in a row without writing or running anything. If the "
                         "results already give what the task needs (values, an API, a command), stop researching and use "
                         "them now, citing the page you rely on. Search again only for one specific fact that is still "
                         "missing: name it in one line first.")
        elif name in EXPLORE and ok:
            self.explore += 1
            over = self.explore - EXPLORE_NUDGE_AT
            if over >= 0 and over % EXPLORE_REPEAT == 0:
                tail += (f"\n[CLI] {self.explore} reads/searches in a row without an edit. If you can already say what to "
                         "change and where, do it now. If something specific is still missing, name it in one line and "
                         "continue; there is no limit.")
        serial = self._serial(name, ok)
        return tail + ("" if "reads/searches in a row" in tail else serial)   # one reminder at a time

    def _serial(self, name, ok):
        """One read or search per step, again and again: remind once that independent reads go together."""
        if name not in EXPLORE or not ok or self.batch > 1:
            self.serial = 0
            return ""
        self.serial += 1
        over = self.serial - SERIAL_NUDGE_AT
        if over >= 0 and over % SERIAL_REPEAT == 0:
            return (f"\n[CLI] {self.serial} steps in a row each read or searched one thing, and every step is a full "
                    "model pass. When you already know several files, ranges or patterns you will need, request them "
                    "together in one reply: reads and searches run in parallel.")
        return ""

    def _missing(self, name, a, out, ok):
        """A missing program is evidence, not a puzzle: after a few lookups, report it instead of hunting."""
        m = MISSING_RE.search(str(out or "")) if name in RUN else None
        if m:
            prog = next(g for g in m.groups() if g)
            if not PROGRAM_RE.fullmatch(prog) or SCRIPT_RE.fullmatch(prog) or _assignment(prog, a, out):
                return ""
            if prog != self.missing:
                self.missing, self.hunt, self.missing_edited = prog, 0, False
            return ""
        if not self.missing:
            return ""
        if name in MUTATING:
            self.missing_edited = self.missing_edited or ok
            return ""
        if name in RUN and ok and self.missing_edited:     # fixed (a typo in the name): it runs now
            self.missing, self.hunt, self.missing_edited = "", 0, False
            return ""
        self.hunt += 1
        over = self.hunt - MISSING_NUDGE_AT
        if over >= 0 and over % MISSING_REPEAT == 0:
            return (f"\n[CLI] `{self.missing}` is still not installed after {self.hunt} lookups. Stop looking: answer that it "
                    "is missing and how to get it (an install command or link you saw). Do not edit the project to use "
                    "another program in its place, unless the name is a plain typo of a command that exists: that "
                    "choice is the user's.")
        return ""

    def _run_streak(self, out, ok):
        """The same error repeated and a long run of commands without an edit — a hint, then commands are blocked."""
        self.runs += 1
        # A script error can come back with exit code 0 ("& x.ps1; 'done'"): the error record still counts.
        sig = _error_sig(out) if not ok or "FullyQualifiedErrorId" in str(out or "") else ""
        if sig:
            seen = self.errors[sig] = self.errors.get(sig, 0) + 1
            if seen >= ERR_BLOCK_AT:
                self.blocked_err = sig
                return (f"\n[CLI] This error has now come back {seen} times. Further commands are refused until a real "
                        "edit. Read the code that produces it, or answer the user with the exact error, what you ruled out "
                        "and the most likely cause.")
            if seen >= ERR_NUDGE_AT:
                return (f"\n[CLI] The same error came back {seen} times in different experiments: they are not narrowing "
                        "the cause. Stop probing. Read the code on the failing path (the script and every file it imports or "
                        "dot-sources) and look there for what produces this error; then fix it or report it to the user.")
        over = self.runs - VERIFY_NUDGE_AT
        if self.wrote and ok and self.runs < RUN_STREAK_NUDGE_AT and over >= 0 and over % VERIFY_REPEAT == 0:
            return (f"\n[CLI] {self.runs} checks in a row since your last change, and nothing was changed. If the given "
                    "examples or tests already pass, stop checking and answer now. Check again only for one specific "
                    "doubt you can name in one line; a check you repeat is re-run from its saved file, not re-sent.")
        over = self.runs - RUN_STREAK_NUDGE_AT
        if over >= 0 and over % RUN_STREAK_REPEAT == 0:
            return (f"\n[CLI] {self.runs} commands in a row without an edit. If the user asked a question, answer it now "
                    "with the evidence you have (what you ran, what you saw, the likely cause). Run another command only "
                    "for a specific missing fact you can name.")
        return ""

    def _after(self, name, a, out, ok=True):
        if name in NEUTRAL:
            return ""
        self.n += 1
        if name in EMPTY_SEARCH_TOOLS:
            if _empty_search_result(name, out):
                self.empty_searches += 1
                if self.empty_searches >= MAX_EMPTY_SEARCHES:
                    return ("\n[CLI] This was the second search with no matches. One narrow retry was already used; "
                            "stop searching and report that the target was not found, or ask the user.")
                return "\n[CLI] No results. You may make one narrow retry with a different term; do not broaden to parent folders."
            self.empty_searches = 0
        else:
            self.empty_searches = 0
        key = (name, _canon(a))
        # These tool paths may perform edits internally, so old read coverage is no longer safe.
        if name in {"code_mode", "subagent"}:
            self.read_ranges.clear()
        # read_ranges is not cleared by edits or commands: each entry carries the file's (mtime, size), and
        # before() drops it as soon as the file changes on disk. Clearing on every command let the model
        # re-read unchanged files after each run (real session: start.bat read 5 times in one turn).
        if name in MUTATING:
            if ok:                                   # state changed: old reads and outputs may differ now
                self.seen.clear()
                self.fps.clear()
                self.read_ranges.pop(_path_key(a.get("path")), None)   # its own target, even within one mtime tick
                self.stale = 0
                self.last = None
            return ""
        if name in RUN:
            # A successful command may have changed files/state even if its stdout is empty or
            # identical to another command. Do not classify productive commands as stale merely
            # because they print the same text. Exact immediate successful repeats are handled
            # by before(); failed commands still contribute to the no-progress streak.
            self.seen.clear()
            self.last = (key, ok)
            if ok:
                self.fps.clear()
                self.stale = 0
                return ""
        elif ok:
            self.seen[key] = self.n
        if name == "read_file" and ok and len(str(out or "")) <= max(1000, _visible_output_limit() - 800):
            match = READ_RANGE_RE.search(str(out or ""))
            if match:
                start, end, total = (int(match.group(i)) for i in (1, 2, 3))
                path = _path_key(a.get("path"))
                previous = self.read_ranges.get(path)
                stamp = _stamp(a.get("path"))
                ranges = list(previous[1]) if previous and previous[0] == total and previous[2] == stamp else []
                ranges.append((start, end))
                # Merge adjacent/overlapping intervals so the ledger is minimal.
                merged = []
                for left, right in sorted(ranges):
                    if merged and left <= merged[-1][1] + 1:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                    else:
                        merged.append((left, right))
                self.read_ranges[path] = (total, merged, stamp)
        fp = _fp(out)
        if fp in self.fps:
            self.stale += 1
        else:
            self.fps.add(fp)
            self.stale = 0
        return self._tail(name)


# A reply that announces the next step and stops: "Now let me fix the test:" with no tool call.
# Small models do this often (Cline and gemini-cli both nudge for it); the turn ended with nothing
# done and the user had to type "continue". English and Russian forms of the announced action; in any
# language, a last line that ends with a colon opens something the reply never delivers.
_PROMISE = re.compile(
    r"^(?:(?:now|next|first|then|ok(?:ay)?|so|great|good)[,.!]?\s+)*"
    r"(?:let\s+me|let's|i'll|i\s+will|i\s+am\s+going\s+to|i'm\s+going\s+to|i\s+need\s+to)\s+(?!know\b)\w+"
    r"|^(?:(?:сейчас|теперь|далее|затем|дальше|итак|хорошо|отлично|так)[,.!]?\s+)*(?:я\s+|давай(?:те)?\s+(?:я\s+)?)?"
    r"(?:исправлю|сделаю|проверю|посмотрю|запущу|прочитаю|открою|изменю|добавлю|обновлю|напишу|создам|удалю|"
    r"поправлю|перепишу|найду|займусь|приступаю|начну|посмотрим|проверим|исправим|запустим|внесу|применю)\b",
    re.I)
_OFFER = re.compile(r"\b(?:if\s+you|would\s+you|want\s+me|let\s+me\s+know|если\s+(?:хотите|хочешь|нужно|надо)|могу)\b", re.I)
PROMISE_MAX = 400   # a stalled reply is short; a long final report may end with a plan for later


def stalled_promise(text):
    """The reply's last line announces an action it never took (no tool call in the reply)."""
    lines = [x.strip(" *_#>-\t") for x in str(text or "").strip().splitlines() if x.strip()]
    if not lines:
        return False
    last = re.split(r"(?<=[.!])\s+", lines[-1])[-1]   # the last sentence: "Found it. Now let me run it:"
    if "?" in last or "？" in last or _OFFER.search(last):
        return False
    if last.endswith((":", "：")):
        return True
    if not _PROMISE.search(last):
        return False
    return last.endswith(("…", "...")) or len(str(text).strip()) <= PROMISE_MAX


PROMISE_NUDGE = ("<continue_feedback>Your reply announced a next step but made no tool call, so nothing was done. "
                 "If work remains, do that step now with a tool call. If the task is already complete, "
                 "give the final answer instead.</continue_feedback>")

# Extra protection on top of the two checks above, for any language and any wording: the model itself is asked
# whether its short final reply only announced a step. Runs only when the turn used tools, the reply is short,
# and the cheap checks did not fire. One request after the same prefix (cache hit), a tiny thinking budget
# (Strata does not render it into the prompt, so the cache stays), answer YES/NO. Off: config.PROMISE_CHECK.
# Live, IQ3_XXS on Strata, 8 replies in 4 languages x3: budget 64 -> 22/24 right (cut before the verdict),
# 160 -> 24/24, 1.3-3.8 s per check. No false YES on a finished reply in any run.
JUDGE_THINK = 160         # thinking tokens for a yes/no about its own reply
JUDGE_MAX_TOKENS = 700    # that budget plus Strata's 512-token answer reserve; the answer itself is one word
JUDGE_PROMPT = ("<self_check>Question about your previous reply: does it only announce a next step (for example "
                "\"now I will fix it\", in any language) that it did not do, while the task still needs that step? "
                "A report of finished work or a question to the user is NO. Reply with one word: YES or NO."
                "</self_check>")


def needs_judge(text):
    """A short non-empty final reply that does not end with a question to the user (any script)."""
    visible = str(text or "").strip()
    if not visible or len(visible) > PROMISE_MAX:
        return False
    last = visible.splitlines()[-1]
    return not any(q in last for q in ("?", "？", "؟", ";"))


def judge_said_yes(reply, reasoning=""):
    """The judge's verdict: the first word of its answer is YES. With an empty answer (live run: the thinking
    ended on "So the answer is YES." and no answer followed) the last YES/NO of the reasoning decides.
    Anything else (NO, nothing) keeps the turn ended."""
    words = re.findall(r"[A-Za-z]+", str(reply or ""))
    if words:
        return words[0].upper() == "YES"
    verdicts = re.findall(r"\b(YES|NO)\b", str(reasoning or ""))
    return bool(verdicts) and verdicts[-1] == "YES"


async def judge_stalled(client, api_messages, **request):
    """Ask the model whether its last reply only announced a step. Errors count as NO: never a false nudge."""
    from .util import split_think
    extra = dict(request.pop("extra_body", None) or {})
    if "reasoning_budget_tokens" not in request.pop("disabled_fields", ()):
        extra["reasoning_budget_tokens"] = JUDGE_THINK
    try:
        r = await client.chat.completions.create(
            messages=list(api_messages) + [{"role": "user", "content": JUDGE_PROMPT}],
            extra_body=extra, max_tokens=JUDGE_MAX_TOKENS, stream=False, **request)
        m = r.choices[0].message
        if m.tool_calls:
            # Asked whether it is done, the model went straight back to work (live runs: "So YES. I need to read
            # the file" and a tool call). The call is not executed here; the nudge lets it make it for real.
            return True
        think, answer = split_think(str(m.content or ""))
        reasoning = getattr(m, "reasoning_content", None) or (getattr(m, "model_extra", None) or {}).get("reasoning_content")
        return judge_said_yes(answer, reasoning or think)
    except Exception:
        return False
