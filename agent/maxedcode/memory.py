"""Memory. Two levels: global (the whole machine) and per project.

- the `MEMORY.md` index is short: 200 lines / 25 KB, details go to topic files next to it;
- `AGENTS.md` / `CLAUDE.md` / `CLAUDE.local.md` — alternative index names; read
  from the drive root down to cwd (as in Claude Code), `CLAUDE.local.md` after `CLAUDE.md`;
- notes of subfolders are loaded when the agent touches a file in that subfolder (docs_for);
- an `@path` line imports a memory file (only inside memory files), depth up to 5, cycle-safe;
- notes: user / feedback / project / reference — a topic file + one pointer line in the index;
- path-scoped rules: `.maxedcode/rules/*.md` with `paths:` frontmatter — added as a separate message
  when the agent touches a matching file (the prefix cache is left intact).

Memory is a separate message after the system prompt (message()), not part of SYSTEM.
"""
import os
import re
from contextlib import suppress
from pathlib import Path
from .config import HOME, MEM_DIR
from .util import frontmatter, hash_cwd

MAX_LINES, MAX_CHARS, MAX_DEPTH = 200, 25_000, 5
README_LINES = 40                    # README is a pointer, not an instruction file: only its head is loaded
DOC_CHARS = 8000                     # a subfolder's instruction file is injected into a tool result: keep it bounded
# Bundled third-party code: its AGENTS.md / README is for that project's contributors, not for this task.
VENDOR_DIRS = {"third_party", "third-party", "thirdparty", "vendor", "vendored", "external", "extern", "deps",
               "node_modules", "site-packages", ".venv", "venv"}
TYPES = ("user", "feedback", "project", "reference")
NAMES = ("MEMORY.md", "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")

MEMORY_CHARS = int(os.environ.get("AGENT_MEMORY_CHARS", "30000"))   # all startup instruction files together
SCOPE_NOTE = ("Instruction files below go from broad to specific. A project file applies to work under its folder; "
              "a deeper folder's file takes precedence over broader ones. None of them overrides the system prompt "
              "or the user's direct instructions.")


def instruction_file(d):
    """THE instruction file of one folder (where a new note goes): .maxedcode/memory.md, then MEMORY.md /
    AGENTS.md / CLAUDE.md / CLAUDE.local.md."""
    files = instruction_files(d)
    return files[0] if files else None


def instruction_files(d):
    """Every instruction file of one folder, in that order; a file with the same text as an earlier one, or
    one that only imports an earlier one (CLAUDE.md = "@AGENTS.md"), is left out.

    Only the first was read: with both AGENTS.md and CLAUDE.md in a folder, CLAUDE.md was silently lost
    (DeepSeek Harness loads every candidate and merges identical ones)."""
    out, texts = [], set()
    for f in [d / ".maxedcode" / "memory.md"] + [d / n for n in NAMES]:
        if not f.is_file():
            continue
        with suppress(OSError):
            raw = strip_comments(f.read_text(encoding="utf-8", errors="replace")).strip()
            imports = [m.group(1) for m in (re.match(r"^\s*@(\S+)\s*$", x) for x in raw.splitlines() if x.strip()) if m]
            only_imports = raw and len(imports) == len([x for x in raw.splitlines() if x.strip()])
            if raw in texts or (only_imports and all(os.path.normcase(os.path.normpath(f.parent / i))
                                                     in {os.path.normcase(str(o)) for o in out} for i in imports)):
                continue
            texts.add(raw)
            out.append(f)
    return out


def project_root(cwd=None):
    """The nearest folder with .git at or above cwd, else cwd: project rules and skills live there, so they
    are found when the agent is started in a subfolder (CLAUDE.md files were, rules and skills were not)."""
    cwd = Path(cwd or os.getcwd())
    return next((d for d in [cwd] + list(cwd.parents) if (d / ".git").exists()), cwd)


def project_files(cwd=None):
    """Instruction files from the topmost folder down to cwd (the deeper file wins)."""
    cwd = Path(cwd or os.getcwd())
    out = [f for d in reversed([cwd] + list(cwd.parents)) for f in instruction_files(d)]
    return out or [MEM_DIR / hash_cwd(cwd) / "MEMORY.md"]

def index_files(cwd=None):
    """[global, project…]. Project files read later, so they are worth more."""
    return [MEM_DIR / "MEMORY.md"] + project_files(cwd)

def note_file(cwd=None):
    """Where a new note is written: the instruction file of cwd, else the memory folder of this project."""
    f = instruction_file(Path(cwd or os.getcwd()))
    return f or MEM_DIR / hash_cwd(Path(cwd or os.getcwd())) / "MEMORY.md"

def topics(f):
    """Topic files lie next to the index, but only in memory folders: not in the project root (README.md ...)."""
    if f.parent.name != ".maxedcode" and MEM_DIR not in f.parents and f.parent != MEM_DIR:
        return []
    out = sorted(x.name for x in f.parent.glob("*.md") if x != f and x.name not in NAMES)
    out += [f"notes/{x.name}" for x in sorted((f.parent / "notes").glob("*.md"))]
    return out

def extra_files(cwd=None):
    """[(label, file)] loaded after the main indexes: the old ~/.maxedcode/CLAUDE.md (global) and
    CLAUDE.local.md next to the project (gitignored, personal). Both used to be read before this module existed."""
    cwd = Path(cwd or os.getcwd())
    return [("global", HOME / "CLAUDE.md"), ("project", cwd / "CLAUDE.local.md")]

def strip_comments(raw):
    """HTML comments are notes for humans, not instructions: single-line ones are cut, code fences stay."""
    out, fence = [], False
    for line in raw.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        out.append(line if fence else re.sub(r"<!--.*?-->", "", line))
    return "\n".join(out)

def clip(raw, lines=MAX_LINES):
    n = len(raw.splitlines())
    if n > lines or len(raw) > MAX_CHARS:
        return "\n".join(raw.splitlines()[:lines])[:MAX_CHARS] + f"\n…[cut: {n} lines / {len(raw)} chars over the limit]"
    return raw

def render(raw, base, depth=0, seen=None):
    """@path on its own line → include that memory file. Depth ≤ 5, no cycles."""
    seen = seen if seen is not None else set()
    out = []
    for line in raw.splitlines():
        m = re.match(r"^\s*@(\S+)", line)
        if not m:
            out.append(line)
            continue
        p = Path(m.group(1)).expanduser()
        if not p.is_absolute():
            p = base / p
        p = Path(os.path.normpath(p))
        key = str(p).lower()
        if depth >= MAX_DEPTH or key in seen:
            out.append(line + f"  [import skipped: depth limit {MAX_DEPTH} or cycle]")
        elif not p.is_file():
            out.append(line + "  [import skipped: no file]")
        else:
            seen.add(key)
            out.append(line)
            out.append(render(p.read_text(encoding="utf-8", errors="replace"), p.parent, depth + 1, seen))
    return "\n".join(out)

def index(f):
    """Index text: the limit applies to the file itself, then to the text with imports."""
    if not f.exists():
        return ""
    raw = clip(strip_comments(f.read_text(encoding="utf-8", errors="replace").strip()))
    if not raw:
        return ""
    body = render(raw, f.parent, 0, {str(f).lower()})
    n = len(body.splitlines())
    if n > MAX_LINES or len(body) > MAX_CHARS:
        body = "\n".join(body.splitlines()[:MAX_LINES])[:MAX_CHARS] + \
            f"\n…[index cut: {n} lines / {len(body)} chars over the limit — read the topic files in {f.parent}]"
    return body

def startup_files(cwd=None):
    """Files really injected at startup, in load order: global, the old global CLAUDE.md, project files."""
    cwd = Path(cwd or os.getcwd())
    return [("global", MEM_DIR / "MEMORY.md")] + list(extra_files(cwd)) + \
           [("project", f) for f in project_files(cwd)]

def text(cwd=None):
    found, done = [], set()
    for label, f in startup_files(cwd):
        key = str(f).lower()
        if key in done:                      # CLAUDE.local.md may already be the project index
            continue
        done.add(key)
        b = index(f)
        if b:
            found.append((label, f, b))
    if not found:
        return ""
    # One budget for all files: each could be 25k characters, so a deep tree took a big part of a small
    # window without a word. The deepest (most specific) files are kept first; a cut is said in place.
    room, bodies = MEMORY_CHARS, {}
    for i in range(len(found) - 1, -1, -1):
        b = found[i][2]
        if len(b) > room:
            b = (b[:room] + f"\n…[memory budget {MEMORY_CHARS} characters: cut from {len(b)} to {room}; "
                 f"read_file {found[i][1]} for the rest]") if room > 200 else \
                f"[omitted: memory budget {MEMORY_CHARS} characters is used up; read_file {found[i][1]} if needed]"
        room = max(0, room - len(b))
        bodies[i] = b
    parts = []
    for i, (label, f, _) in enumerate(found):
        top = topics(f)
        scope = f"; applies to work under {f.parent}" if label == "project" and MEM_DIR not in f.parents else ""
        parts.append(f"## Memory ({label}: {f}{scope})"
                     + (f"\nTopic files: {', '.join(top)}" if top else "") + f"\n{bodies[i]}")
    return SCOPE_NOTE + "\n\n" + "\n\n".join(parts)

def message(cwd=None):
    """Memory as its own message after the system prompt, so the cached prefix stays stable."""
    t = text(cwd)
    return {"role": "system", "content": "# Memory\n" + t} if t else None

def loaded(cwd=None):
    """[(label, file, lines)] really injected — for /context."""
    out, done = [], set()
    for label, f in startup_files(cwd):
        key = str(f).lower()
        if key in done:
            continue
        done.add(key)
        n = len(index(f).splitlines())
        if n:
            out.append((label, f, n))
    return out

def slug(text, type_):
    s = re.sub(r"[^\w-]+", "-", (text or "").strip().lower()).strip("-")[:48].strip("-")
    return f"{type_}-{s or 'note'}"

def note(text, type_="project", why="", apply="", scope="project", cwd=None):
    """One topic file + one pointer line in the index (the agent writes memory itself)."""
    if type_ not in TYPES:
        return None, f"unknown memory type: {type_} ({', '.join(TYPES)})"
    f = note_file(cwd)
    f.parent.mkdir(parents=True, exist_ok=True)
    name = slug(text, type_)
    tf = f.parent / "notes" / f"{name}.md"
    tf.parent.mkdir(parents=True, exist_ok=True)
    body = (text or "").strip()
    if why:
        body += f"\n\nWhy: {why}"
    if apply:
        body += f"\n\nHow to apply: {apply}"
    if tf.exists():
        tf.write_text(tf.read_text(encoding="utf-8", errors="replace").rstrip() + "\n\n" + body + "\n", encoding="utf-8")
    else:
        tf.write_text(f"---\ntype: {type_}\nname: {(text or '').strip()[:60]}\n---\n{body}\n", encoding="utf-8")
    line = f"- [{type_}] {(text or '').strip()[:120]} → notes/{name}.md"
    if not f.exists() or line not in f.read_text(encoding="utf-8", errors="replace").splitlines():
        with open(f, "a", encoding="utf-8") as h:
            h.write(line + "\n")
    return tf, line

def docs_for(path, cwd=None):
    """Instruction files of the folders between cwd and the file being touched (loaded once per folder).
    A folder without an instruction file gives the head of its README as a pointer."""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = Path(os.path.abspath(str(p)))
    cwd = Path(cwd or os.getcwd())
    try:
        parts = p.relative_to(cwd).parts[:-1]
    except ValueError:
        return []
    out = []
    for i in range(len(parts) + 1):
        if any(x.lower() in VENDOR_DIRS for x in parts[:i]):
            break
        d = cwd.joinpath(*parts[:i])
        files = instruction_files(d)
        for f in files:
            b = index(f)
            if len(b) > DOC_CHARS:
                b = b[:DOC_CHARS] + f"\n…[cut at {DOC_CHARS} chars; read_file {f} for the rest]"
            if b:
                out.append((f, b))
        if not files:
            r = d / "README.md"
            b = clip(strip_comments(r.read_text(encoding="utf-8", errors="replace").strip()), README_LINES) if r.is_file() else ""
            if b:
                out.append((r, b))
    return out


def doc_block(f, body):
    """A folder doc as it goes into a tool result: which work it applies to, then its text."""
    return (f"<project doc {f}>\nApplies to work under {Path(f).parent}; more specific than the memory above.\n"
            f"{body}\n</project doc>")


def updated_block(path):
    """The new text of an instruction file the agent just changed, to use instead of the loaded one.

    Memory is read once per session: after "add a rule to CLAUDE.md" the model kept the old version until
    the end of the session (DeepSeek Harness sends "Updated instructions … use the following content")."""
    f = Path(str(path))
    if not f.is_file():
        return f"<updated_instructions {f}>\nThis instruction file was removed: its earlier instructions no longer apply.\n</updated_instructions>"
    if f.parent.name == "rules":
        body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))[1]
    else:
        body = index(f)
    if len(body) > DOC_CHARS:
        body = body[:DOC_CHARS] + f"\n…[cut at {DOC_CHARS} chars; read_file {f} for the rest]"
    return (f"<updated_instructions {f}>\nThis file changed after it was loaded: use this content instead of the "
            f"earlier one.\n{body}\n</updated_instructions>")


def split_saved(msgs):
    """A saved transcript -> (prefix, history). prefix = [memory message?, env block] when the transcript
    starts with them (headless saves the whole list); the system prompt itself is never part of it."""
    msgs, i, prefix = list(msgs or []), 0, []
    while i < len(msgs) and msgs[i].get("role") == "system" and not str(msgs[i].get("content") or "").startswith("<hook>"):
        if str(msgs[i].get("content") or "").startswith("# Memory"):
            prefix.append(msgs[i])
        i += 1
    if i < len(msgs) and msgs[i].get("role") == "user" and str(msgs[i].get("content") or "").startswith("<env>"):
        prefix.append(msgs[i])
        i += 1
    history = [m for m in msgs[i:] if not (m.get("role") == "user" and str(m.get("content") or "").startswith("<env>"))]
    return prefix, history


_SECTION = re.compile(r"\n\n(?=## Memory \(|# Skills\n)")


def _sections(text):
    return {p.split("\n", 1)[0]: p for p in _SECTION.split(str(text or "").strip()) if p}


def changed_sections(old, new):
    """The sections of the memory message that are new or changed, and the headers of removed ones."""
    o, n = _sections(old), _sections(new)
    out = [n[k] for k in n if o.get(k) != n[k]]
    gone = [k for k in o if k not in n]
    if gone:
        out.append("Removed: " + "; ".join(gone))
    return "\n\n".join(out)


def resume_prefix(old, new, history):
    """Which prefix a reopened session sends: (prefix, notice for its next user message).

    The memory message and the env block (with today's date) were rebuilt on every reopen: a new day or
    an edited CLAUDE.md changed the second message, and the server re-read the whole history after it.
    Now the saved prefix is sent as it was and only what changed rides in the next user message (new,
    so cache-safe). When that notice would be longer than the history itself, rebuilding is cheaper."""
    if not old or old == new:
        return new, ""
    def part(msgs, role, start):
        return next((str(m.get("content") or "") for m in msgs if m.get("role") == role
                     and str(m.get("content") or "").startswith(start)), "")
    notes = []
    old_env, new_env = part(old, "user", "<env>"), part(new, "user", "<env>")
    if old_env != new_env:
        fresh = [x for x in new_env.splitlines() if x not in old_env.splitlines() and not x.startswith("<")]
        if fresh:
            notes.append("<env_update>\n" + "\n".join(fresh) + "\n</env_update>")
    old_mem, new_mem = part(old, "system", "# Memory"), part(new, "system", "# Memory")
    if old_mem != new_mem:
        diff = changed_sections(old_mem, new_mem)
        if diff:
            notes.append("<updated_instructions>Instructions changed since this session started; use these "
                         f"versions instead of the earlier ones:\n{diff}\n</updated_instructions>")
    note = "\n".join(notes)
    size = sum(len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or "")) for m in history)
    if not note or len(note) >= size:
        return new, ""
    return old, note + "\n"


def is_instruction(path):
    """A file whose text the model gets as instructions: an instruction file name or a path rule."""
    p = Path(str(path or ""))
    norm = str(p).replace("\\", "/").lower()
    return p.name in NAMES or norm.endswith("/.maxedcode/memory.md") or "/.maxedcode/rules/" in norm \
        or "/memory/rules/" in norm

def rules(cwd=None):
    """path-scoped rules: .maxedcode/rules/*.md and HOME/memory/rules/*.md → (patterns, body, file)."""
    out = []
    here = Path(cwd or os.getcwd())
    dirs = dict.fromkeys([here / ".maxedcode" / "rules", project_root(here) / ".maxedcode" / "rules", MEM_DIR / "rules"])
    for d in dirs:
        for f in sorted(d.glob("*.md")):
            with suppress(Exception):
                fm, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                p = fm.get("paths") or "*"
                out.append(([p] if isinstance(p, str) else p, body, f))
    return out

def rules_for(path, cwd=None):
    """Rules that match the file the agent is touching (patterns use /, Windows paths normalized)."""
    key = str(path).lower().replace("\\", "/")
    hit = []
    for pats, body, f in rules(cwd):
        for p in pats:
            if p == "*" or re.search(str(p).lower().replace("*", ".*").replace("{", "").replace("}", ""), key):
                hit.append((f, body))
                break
    return hit

def stats(cwd=None):
    return [(f, len(index(f).splitlines())) for f in index_files(cwd)]
