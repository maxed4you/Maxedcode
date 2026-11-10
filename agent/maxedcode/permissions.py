"""Permission rules (like the permission system in Claude Code): settings.json `permissions` + the HOME/permissions.json file.

Rule: tool or tool(pattern) — pattern is a regex over the main argument (command/code/path).
Read and write are checked separately: read_file/grep = read, edit_file/write_file = write,
powershell (bash on Linux/macOS)/py_run = run. Protected paths are denied for both read and write even without a rule.
Commands: /allow, /deny, /ask, /permissions.
"""
import json
import os
import re
from pathlib import Path
from contextlib import suppress
from .config import PERMS_FILE, PROTECTED, SETTINGS
from . import shelltool

READ = ("read_file", "grep")
WRITE = ("edit_file", "write_file", "patch", "apply_diff")

# Commands that need an explicit human OK even in AUTO mode / are refused in headless mode.
DANGEROUS = [
    (r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(sh|bash|zsh|iex|Invoke-Expression|pwsh|powershell|python\d*)\b",
     "downloads and executes remote code"),
    (r"\b(iex|Invoke-Expression)\b[^\n]*(curl|wget|iwr|irm|DownloadString|Invoke-WebRequest)", "executes downloaded code"),
    (r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*(;|&&|\n)\s*(sh|bash|zsh|pwsh|powershell|python\d*|iex)\b", "downloads and then runs something"),
    (r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease|\s-f\b)", "force push"),
    (r"\bgit\s+update-ref\b[^\n]*\s-d\b", "deletes a Git reference"),
    (r"\bgit\s+reset\s+--hard\b", "discards uncommitted work"),
    (r"\bgit\s+clean\b[^\n]*\s-\w*f", "deletes untracked files"),
    (r"\brm\s+-\w*[rf]\w*\s|\bRemove-Item\b[^\n]*-Recurse|\brmdir\s+/s|\bdel\s+/[sq]|\brd\s+/s", "recursive or forced delete"),
    (r"\b(?:Remove-Item|del|erase|rm|rmdir)\b", "deletes existing files"),
    (r"\b(Format-Volume|diskpart|mkfs)\b|\bdd\s+if=", "disk-level operation"),
]
DANGEROUS_DELETE = r"\b(?:Remove-Item|ri|del|erase|rm|rmdir|rd)\b"
# Linux/macOS only (the bash tool), checked before DANGEROUS so `sudo rm x` reports sudo, not a plain delete.
POSIX_DANGEROUS = [
    (r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&[^}]*\}\s*;?\s*:", "fork bomb"),
    # Command position only: `grep sudo log.txt` or `echo reboot` is not one.
    (r"(?:^|[;&|(\n'\"`]|\b(?:xargs|nohup|exec|time|then|do))\s*(?:sudo|doas|pkexec)\b|(?:^|[;&|(\n'\"`])\s*su\s+(?:-\w*\s+)*-c\b",
     "runs a command as root"),
    (r"(?:^|[;&|(\n'\"`]|\b(?:sudo|doas|then|do))\s*(?:shutdown|reboot|halt|poweroff)\b|(?:^|[;&|(\n'\"`]|\bsudo)\s*init\s+[06]\b"
     r"|\bsystemctl\s+(?:poweroff|reboot|halt|suspend|hibernate)\b", "shuts down or restarts the computer"),
    (r"\bdd\b[^\n]*\bof=/dev/|>\s*/dev/(?:sd[a-z]|nvme\d|hd[a-z]|disk\d|mmcblk\d|vd[a-z])|\b(?:mkfs(?:\.\w+)?|fdisk|sfdisk|parted|wipefs|diskutil\s+(?:erase\w*|partition\w*|zero\w*))\b",
     "disk-level operation"),
]
POSIX_SERIOUS = {"fork bomb", "runs a command as root", "shuts down or restarts the computer",
                 "changes permissions of a system folder"}
# Reasons that still confirm in AUTO mode: irreversible damage or work outside the project.
# Ordinary work (writing an output file, editing a file) runs silently in AUTO.
# Ordinary deletes ("recursive or forced delete", "deletes existing files", "deletes or renames files from
# Python") are NOT here on purpose: AUTO mode runs them without asking. MANUAL mode still confirms them.
# Only wiping a drive root / home / system folder keeps confirming even in AUTO.
CATASTROPHIC = "deletes a drive root, home or system folder"
SERIOUS = {
    "downloads and executes remote code", "executes downloaded code", "downloads and then runs something",
    "force push", "deletes a Git reference", "discards uncommitted work", "deletes untracked files",
    "disk-level operation", CATASTROPHIC,
} | (POSIX_SERIOUS if shelltool.POSIX else set())

# Plain-language wording for the confirmation dialog: what the call actually does.
PLAIN = {
    "downloads and executes remote code": "downloads code from the internet and runs it",
    "executes downloaded code": "runs code downloaded from the internet",
    "downloads and then runs something": "downloads something and then runs it",
    "force push": "rewrites the history on GitHub (force push)",
    "deletes a Git reference": "deletes a Git reference and may leave the repository without its branch HEAD",
    "discards uncommitted work": "throws away uncommitted changes",
    "deletes untracked files": "deletes files that git does not track",
    "recursive or forced delete": "deletes files and folders",
    "deletes existing files": "deletes existing files",
    "disk-level operation": "works with the disk itself",
    "deletes or renames files from Python": "deletes or renames files",
    CATASTROPHIC: "deletes a whole drive, your home folder or a system folder",
    "writes files from Python": "writes a file",
    "fork bomb": "starts processes until the computer freezes (fork bomb)",
    "runs a command as root": "runs a command as root (sudo)",
    "shuts down or restarts the computer": "shuts down or restarts the computer",
    "changes permissions of a system folder": "changes owner or permissions of a whole system or home folder",
}

def explain(name, key):
    """Plain sentence for the confirmation, not the raw command: what this call does."""
    why = dangerous(name, key) or (dangerous_python(key) if name == "py_run" else None)
    if why:
        return PLAIN.get(why, why)
    if name in WRITE:
        return "changes a file in the project"
    if name in READ:
        return "only reads"
    if name == "browser":
        return "works in your own browser, where you are logged in: it can open pages, click and type"
    if name.startswith("mcp__"):
        return "calls an external MCP tool, which can do things outside this project"
    return "runs a command"

_VALUE_FLAGS = r"-(?:ErrorAction|EA|WarningAction|WA|InformationAction|InfA)"


def _without_cache_deletes(command):
    """The command without its statements that only delete Python caches, or None if nothing is cache-only.

    "Remove-Item __pycache__ -Recurse -Force; python -m unittest" was refused in headless and cost a step.
    Every statement that deletes must be cache-only; the rest is checked by the caller as usual.
    """
    statements = re.split(r"[;\n\r]+", command or "")
    deleting = [s for s in statements if re.search(DANGEROUS_DELETE, s, re.I)]
    if not deleting or not all(_deletes_only_cache(s) for s in deleting):
        return None
    return ";".join(s for s in statements if s not in deleting)


def _deletes_only_cache(command):
    """Allow only an isolated delete whose every target is a cache or .pyc file."""
    source = (command or "").strip()
    # A cache mention in a compound command must never excuse another deletion.
    if not source or re.search(r"[|;&<>`\n\r]", source):
        return False
    if re.match(r"(?i)^Remove-Item\b", source):
        rest = re.sub(r"(?i)^Remove-Item\b", "", source).strip()
    elif re.match(r"(?i)^(?:rm|rmdir)\b", source):
        rest = re.sub(r"(?i)^(?:rm|rmdir)\b", "", source).strip()
    elif re.match(r"(?i)^del\b", source):
        rest = re.sub(r"(?i)^del\b", "", source).strip()
    else:
        return False
    # Remove ordinary option flags; the remaining words are the actual targets. The value of a common
    # parameter ("-ErrorAction SilentlyContinue") is not a target either.
    words = [w.strip().strip('"\'') for w in re.split(r"\s+", rest) if w.strip()]
    words = [w for i, w in enumerate(words) if not (i and re.fullmatch(_VALUE_FLAGS, words[i - 1], re.I))]
    targets = [w for w in words if not w.startswith("-") and not re.fullmatch(r"/[A-Za-z]+", w)]
    if not targets:
        return False
    cache_target = re.compile(r"(?i)^(?:.*[\\/])?(?:__pycache__|\.pytest_cache)(?:[\\/].*)?$|^(?:.*[\\/])?[^\\/]+\.pyc$")
    return all(cache_target.fullmatch(t) for t in targets)


_DEL_CMD = r"(?:remove-item|ri|rm|del|erase|rmdir|rd)"
_CATA_TARGETS = {"c:\\windows", "c:\\users", "c:\\program files", "c:\\program files (x86)", "c:\\programdata",
                 "~", "$home", "$env:userprofile", "%userprofile%", "$env:systemroot", "%systemroot%"}

def _norm_target(w):
    w = w.strip().strip("\"'").replace("/", "\\").lower()
    w = re.sub(r"[\\*]+$", "", w) if re.search(r"[\\*]$", w) else w
    return w

def _is_cata_target(w):
    w = _norm_target(w)
    if not w:
        return False
    if re.fullmatch(r"[a-z]:", w) or w in _CATA_TARGETS:
        return True
    with suppress(Exception):
        if w == _norm_target(str(Path.home())):
            return True
    return False

def catastrophic_delete(command):
    """True when a delete command targets a drive root, the home folder or a system folder."""
    src = command or ""
    for seg in re.split(r"[;&\n\r]+", src):
        parts = [p for p in re.split(r"\|", seg)]
        for i, part in enumerate(parts):
            words = part.strip().split()
            if not words or not re.fullmatch(_DEL_CMD, words[0], re.I):
                continue
            targets = [w for w in words[1:] if not w.startswith("-") and not re.fullmatch(r"/[A-Za-z]+", w)]
            if any(_is_cata_target(w) for w in targets):
                return True
            # "Get-ChildItem C:\ -Recurse | Remove-Item": a delete fed by a listing of a root/home
            if i > 0 and not targets and any(_is_cata_target(w) for w in parts[i - 1].split()[1:] if not w.startswith("-")):
                return True
    return False

# Linux/macOS: the root, the home folder and the system folders (macOS ones included).
_POSIX_CATA = {"/", "~", "$home", "${home}", "/root", "/home", "/users", "/etc", "/usr", "/bin", "/sbin", "/lib",
               "/lib64", "/boot", "/var", "/opt", "/dev", "/proc", "/sys", "/system", "/library", "/applications",
               "/private", "/private/etc", "/private/var", "/volumes", "/usr/bin", "/usr/lib", "/usr/local", "/srv", "/mnt"}
_POSIX_PREFIX = {"sudo", "doas", "command", "builtin", "nohup", "nice", "time", "exec", "env"}


def _posix_cata_target(w):
    w = w.strip().replace('"', "").replace("'", "").lower()
    if not w or w.startswith("-"):
        return False
    # "/*", "~/", "$HOME/*", "/etc/." name the folder itself
    w = re.sub(r"(?:/\.)+$", "", w.rstrip("*")).rstrip("/") or "/"
    if w in _POSIX_CATA:
        return True
    with suppress(Exception):
        home = str(Path.home()).lower().rstrip("/")
        if home and w == home:
            return True
    # /home/<user>, /Users/<user>: someone's whole home folder
    return bool(re.fullmatch(r"/(?:home|users)/[^/\s]+/?", w))


def posix_catastrophic(command):
    """Reason when a bash command wipes or re-permissions the root, a home folder or a system folder."""
    for seg in re.split(r"[;&\n\r]+|\|\|", command or ""):
        parts = seg.split("|")
        for i, part in enumerate(parts):
            words = part.strip().split()
            while words and words[0].lower() in _POSIX_PREFIX:
                words = words[1:]
            if not words:
                continue
            head = words[0].rsplit("/", 1)[-1].lower()
            args = [w for w in words[1:] if not w.startswith("-")]
            flags = "".join(w for w in words[1:] if w.startswith("-") and not w.startswith("--"))
            if head in ("rm", "rmdir", "unlink", "shred") and any(_posix_cata_target(w) for w in args):
                return CATASTROPHIC
            # "echo / | xargs rm -rf": a delete fed by the previous command's root/home/system argument
            feeds_rm = (head == "rm" and not args) or (head == "xargs" and any(w.rsplit("/", 1)[-1] == "rm" for w in args))
            if i > 0 and feeds_rm and any(_posix_cata_target(w) for w in parts[i - 1].split()[1:] if not w.startswith("-")):
                return CATASTROPHIC
            if head == "find" and args and _posix_cata_target(args[0]) and re.search(r"\s-(?:delete|exec)\b", part):
                return CATASTROPHIC
            if head in ("chmod", "chown", "chgrp") and "R" in flags.upper() and any(_posix_cata_target(w) for w in args):
                return "changes permissions of a system folder"
    return None


def dangerous(name, command):
    """Reason string if a shell-tool command is risky, else None."""
    if name != shelltool.NAME or not command:
        return None
    if catastrophic_delete(command):
        return CATASTROPHIC
    if shelltool.POSIX:
        why = posix_catastrophic(command) or next(
            (why for pat, why in POSIX_DANGEROUS if re.search(pat, command, re.I)), None)
        if why:
            return why
    rest = _without_cache_deletes(command)
    if rest is not None:
        return dangerous(name, rest) if rest.strip() else None
    for pat, why in DANGEROUS:
        if re.search(pat, command, re.I):
            if why in ("recursive or forced delete", "deletes existing files") and _deletes_only_cache(command):
                continue
            return why
    return None


# Scratch Python snippets are often used for data analysis, but the runner executes them with the
# user's filesystem permissions. Deleting/renaming always needs a human OK; plain output files are
# normal work, so in AUTO mode a write-only snippet runs without confirmation.
_PYTHON_DESTRUCTIVE = re.compile(
    # Path.replace(target) takes one argument; str.replace(old, new) — in nearly every snippet — takes two.
    r"(?is)(?:\.unlink\s*\(|\.rmdir\s*\(|\.rename\s*\(|\.replace\s*\(\s*[^,()]*\)"
    r"|\b(?-i:os|Path)\.(?:remove|unlink|rmdir|rename|replace)\s*\("      # case-sensitive: not a `path` variable
    r"|\bshutil\.(?:rmtree|move)\s*\("
    r")"
)
_PYTHON_WRITES = re.compile(
    r"(?is)(?:\.write_text\s*\(|\.write_bytes\s*\(|\.touch\s*\(|\.mkdir\s*\("
    r"|\.open\s*\(\s*['\"][^'\"]*[wax+][^'\"]*['\"]"
    r"|\b(?:os|Path)\.(?:mkdir|makedirs|chmod)\s*\("
    r"|\bshutil\.(?:copy|copyfile|copy2|copytree)\s*\("
    r"|\bopen\s*\([^,]+,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+][^'\"]*['\"]"
    r"|\.(?:to_csv|to_excel|to_json|to_pickle|to_parquet|to_feather)\s*\("
    r"|\b(?:numpy|np)\.(?:save|savez|savetxt)\s*\("
    r")"
)

_PY_CATASTROPHIC = re.compile(
    r"(?is)(?:rmtree|remove|unlink|rmdir)\s*\(\s*(?:r?['\"](?:[A-Za-z]:[\\/]*|~[\\/]*|[A-Za-z]:[\\/]+(?:Windows|Users|Program Files)[\\/]*)['\"]"
    r"|(?:(?:pathlib\.)?Path\.home\(\)|os\.path\.expanduser\(\s*['\"]~['\"]\s*\)|os\.environ\[\s*['\"]USERPROFILE['\"]\s*\])\s*[,)])")

# Linux/macOS: shutil.rmtree("/"), os.remove("/etc") and the like.
_PY_CATASTROPHIC_POSIX = re.compile(
    r"(?is)(?:rmtree|remove|unlink|rmdir)\s*\(\s*r?['\"](?:/|~/?|/(?:home|Users|etc|usr|bin|sbin|var|opt|lib|boot|System|Library|Applications)/?)['\"]")


def dangerous_python(code, auto=False):
    """Reason string when a py_run snippet appears to write, rename, or delete files.
    With auto=True (AUTO mode) only deletion/renaming is a reason to confirm."""
    source = code or ""
    if _PYTHON_DESTRUCTIVE.search(source) and _PY_CATASTROPHIC.search(source):
        return CATASTROPHIC
    if shelltool.POSIX and _PYTHON_DESTRUCTIVE.search(source) and _PY_CATASTROPHIC_POSIX.search(source):
        return CATASTROPHIC
    if _PYTHON_DESTRUCTIVE.search(source):
        return "deletes or renames files from Python"
    # Catch risky commands embedded in a Python snippet instead of allowing py_run to bypass the
    # PowerShell command guard. Ordinary subprocess-based test invocations remain allowed.
    if dangerous(shelltool.NAME, source):
        return dangerous(shelltool.NAME, source)
    if auto:
        return None
    if _PYTHON_WRITES.search(source):
        return "writes files from Python"
    return None

def kind(name):
    """What the tool does: read / write / run."""
    if name in WRITE:
        return "write"
    if name in READ:
        return "read"
    return "run"

def protected(key, read=False):
    """Path protected for writing (.env, .ssh, AppData, protected_paths from settings).

    read=True (read_file, grep, glob, folder listings): never protected. The model is local, so a read
    does not leave the PC; refusing it only sent the model round the rule (real links run: grep into
    Textual's site-packages under AppData was refused, and it read the library through 14 py_run
    scripts instead). Writing to these paths stays refused."""
    if read:
        return None
    keys = [key or ""]
    if ".." in keys[0]:      # Temp\..\..\Roaming\... got past the AppData rule
        keys.append(os.path.normpath(keys[0]))
    for p in PROTECTED:
        if any(re.search(str(p), k, re.I) for k in keys):
            return str(p)
    return None

def load():
    r = {"allow": [], "deny": [], "ask": []}
    d = SETTINGS.get("permissions") or {}
    for k in r:
        r[k] = [str(x) for x in (d.get(k) or [])]
    with suppress(Exception):
        f = json.loads(PERMS_FILE.read_text(encoding="utf-8"))
        for k in r:
            r[k] += [str(x) for x in (f.get(k) or [])]
    return r

def save(rules):
    with suppress(Exception):
        PERMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERMS_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

def match(rule, name, key):
    tool, _, pat = rule.partition("(")
    if shelltool.canon(tool.strip()) not in (name, "*"):
        return False
    # only the rule's own closing bracket: rstrip(")") also ate the one of an escaped "\)" at the end of a
    # command, left a lone backslash, and every later check of that tool raised re.error
    pat = pat[:-1] if pat.endswith(")") else pat
    try:
        return not pat or bool(re.search(pat, key or "", re.I))
    except re.error:
        return False

def _command_tokens(command):
    """Tokenize conservatively for executable-prefix policy rules, without shell evaluation."""
    import shlex
    source = str(command or "").strip()
    if not source:
        return []
    try:
        return shlex.split(source, posix=False if __import__("os").name == "nt" else True)
    except ValueError:
        return source.split()


def exec_policy_decision(name, key, policy=None):
    """Evaluate optional exact-prefix/regex execution rules; returns (decision, rule).

    Rules are data in settings.json, not executable code. Supported entries are
    {"prefix": ["git", "status"], "decision": "allow"|"ask"|"deny"} or
    {"pattern": "regex", "decision": ...}. Allow-prefix rules are opt-in through
    `honor_allow_prefixes`; otherwise an allow proposal cannot silently grant authority.
    """
    if name not in (shelltool.NAME, "py_run"):
        return None, None
    config = policy if isinstance(policy, dict) else SETTINGS.get("exec_policy", {})
    if not isinstance(config, dict):
        return None, None
    source = str(key or "").strip()
    tokens = _command_tokens(source)
    rules = config.get("rules", [])
    if not isinstance(rules, list):
        return None, None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        decision = str(rule.get("decision", "ask")).lower()
        if decision not in {"allow", "ask", "deny"}:
            continue
        matched = False
        prefix = rule.get("prefix")
        if isinstance(prefix, list) and prefix and all(isinstance(x, str) for x in prefix):
            matched = len(tokens) >= len(prefix) and [x.lower() for x in tokens[:len(prefix)]] == [x.lower() for x in prefix]
            # Prefix allow is only honored when explicitly enabled. Ask/deny always apply.
            if matched and decision == "allow" and not config.get("honor_allow_prefixes", False):
                continue
        pattern = rule.get("pattern")
        if not matched and isinstance(pattern, str) and pattern:
            try:
                matched = bool(re.search(pattern, source, re.I))
            except re.error:
                matched = False
        if matched:
            return decision, rule
    return None, None


def requested_exec_policy_amendment(name, command):
    """Produce a reviewable policy proposal; never persist or honor it automatically."""
    if name not in (shelltool.NAME, "py_run") or not str(command or "").strip():
        return None
    tokens = _command_tokens(command)
    if not tokens:
        return None
    return {"decision": "allow", "prefix": tokens[:min(4, len(tokens))],
            "tool": name, "requested_amendment": True,
            "note": "Review and add manually; this proposal does not change permissions."}


def check(name, key, k=None):
    """Return deny > ask > allow > no matching rule; protected paths are always denied.
    A rule like tool(pattern) may match either the tool name or its read/write/run kind."""
    k = k or kind(name)
    if protected(key, read=k == "read" or name in ("glob", "py_source")):
        return "deny"
    r = load()
    for rule in r["deny"]:
        if match(rule, name, key) or match(rule, k, key):
            return "deny"
    exec_decision, _exec_rule = exec_policy_decision(name, key)
    if exec_decision == "deny":
        return "deny"
    # deny > ask > allow: a broad allow must not silently override an explicit
    # confirmation rule for a narrower command or path.
    for rule in r["ask"]:
        if match(rule, name, key) or match(rule, k, key):
            return "ask"
    if exec_decision == "ask":
        return "ask"
    for rule in r["allow"]:
        if match(rule, name, key) or match(rule, k, key):
            return "allow"
    if exec_decision == "allow":
        return "allow"
    return None

def deny_message(name, key):
    """Denial text for the model: a protected path and a /deny rule are different reasons and are worked around differently."""
    p = protected(key)
    if p:
        m = re.search(p, key or "", re.I)
        return (f"Error: protected path ({m.group(0) if m and m.group(0) else p}): tools never change it (reading is "
                "allowed). Do not write it through another tool; put the change elsewhere or ask the user.")
    return f"Error: blocked by a permission rule (/deny) for {name}."

def add(kind_, rule):
    r = load()
    if rule not in r.setdefault(kind_, []):
        r[kind_].append(rule)
    save(r)
    return r
