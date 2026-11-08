"""Skills, custom slash commands and subagent definitions.

`.maxedcode/skills/<name>/SKILL.md`, `.maxedcode/commands/<name>.md`, `.maxedcode/agents/<name>.md`
(frontmatter + body). The project wins over the user (`~/.maxedcode`), the user wins over the skills shipped
with the agent (`maxedcode/bundled_skills`): a skill with the same name replaces the bundled one. Progressive disclosure: only a
short list (listing()) goes into the context, the full body is read on demand by the `skill` tool or
the /skill command. `$ARGUMENTS` (and `$1`, `$2`) are substituted, `!`command`` is dynamic context.
"""
import os
import re
import subprocess
from contextlib import suppress
from pathlib import Path
from .config import HOME
from .util import frontmatter

TIMEOUT = int(os.environ.get("AGENT_SKILL_TIMEOUT", "30"))
BUNDLED = Path(__file__).parent / "bundled_skills"

DESC_MAX = 500    # one catalog description at most (DeepSeek Harness: 500): the catalog rides in every request


def roots(kind):
    from .memory import project_root
    # The project root (.git), not only cwd: started in a subfolder, the project's skills were not found.
    here = [Path.cwd() / ".maxedcode" / kind, project_root() / ".maxedcode" / kind]
    out = list(dict.fromkeys(here)) + [HOME / kind]
    return out + [BUNDLED] if kind == "skills" else out

def skills():
    out, seen = [], set()
    for d in roots("skills"):
        for f in sorted(d.glob("*/SKILL.md")):
            with suppress(Exception):
                fm, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                name = fm.get("name") or f.parent.name
                if name.lower() in seen:      # an earlier root (project > user > bundled) already has it
                    continue
                seen.add(name.lower())
                out.append({"name": name, "description": fm.get("description") or "",
                            "model": fm.get("model") or "", "tools": fm.get("allowed-tools") or "",
                            "path": f, "body": body})
    return out

def listing():
    """Short list for the memory message: name + description only."""
    s = skills()
    if not s:
        return ""
    def desc(x):
        d = " ".join(str(x["description"]).split())
        return d if len(d) <= DESC_MAX else d[:DESC_MAX - 1] + "…"
    return "\n\n# Skills\n" + "\n".join(f"- {x['name']}: {desc(x)}" for x in s) + \
        ("\nThis catalog has summaries only. If the user names a skill, or the task clearly matches a skill's "
         "description, load it with the skill tool before taking task actions, and do not follow a skill you "
         "have not loaded. A skill the user already loaded (<skill> in the conversation) is not loaded again.")

def render(body, args=""):
    """$ARGUMENTS / $1.. and !`command` (dynamic context)."""
    parts = body.split("```")
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"!\`([^\`]*)\`", lambda m: run(m.group(1)), parts[i])
    body = "```".join(parts)
    body = body.replace("$ARGUMENTS", args)
    for i, a in enumerate(args.split()[:9], 1):
        body = body.replace(f"${i}", a)
    return body

def run(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (p.stdout or p.stderr).strip()
    except Exception as e:
        return f"[dynamic context failed: {type(e).__name__}]"

def skill(name, args=""):
    for x in skills():
        if x["name"] == name:
            return x, render(x["body"], args)
    return None, None

def commands():
    out = {}
    for d in roots("commands"):
        for f in sorted(d.glob("*.md")):
            with suppress(Exception):
                fm, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                out.setdefault(f.stem, (fm, body, f))
    return out

def command_text(name, args=""):
    c = commands().get(name)
    return (c[0], render(c[1], args)) if c else (None, None)

def agents():
    out = {}
    for d in roots("agents"):
        for f in sorted(d.glob("*.md")):
            with suppress(Exception):
                fm, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                t = fm.get("tools") or ""
                t = t if isinstance(t, list) else [x.strip() for x in re.split(r"[,\s]+", t) if x.strip()]
                out.setdefault(fm.get("name") or f.stem, {"name": fm.get("name") or f.stem,
                                                          "description": fm.get("description") or "",
                                                          "model": fm.get("model") or "", "tools": t,
                                                          "body": body, "path": f})
    return out
