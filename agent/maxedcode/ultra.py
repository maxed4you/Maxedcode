"""Ultra effort: a staged run for big multi-part tasks, driven by code, not by the model.

plan (read-only child) -> each part in its own write-enabled child with a fresh working history ->
check command -> independent read-only review -> fix rounds. Local inference is serialized, so
children run one at a time: the gain is a clean context per part and an outside check, not speed.
The result is a report that rides inside the user's message (added before the first request, so
the cache rule holds); the main agent then verifies it and finishes the turn at high effort.
"""
import os
from contextlib import suppress
from . import config, subagent
from .shelltool import NAME as SHELL_TOOL
from .util import repair_json


def _env_int(name, default, low, high):
    with suppress(TypeError, ValueError):
        return max(low, min(high, int(os.environ.get(name, default))))
    return default


ROUNDS = _env_int("AGENT_ULTRA_ROUNDS", 2, 0, 5)          # review -> fix rounds after the parts
PART_STEPS = _env_int("AGENT_ULTRA_STEPS", 24, 4, 100)     # model steps of one part (subagent default is 12)
MAX_PARTS = 6
EARLIER = 700        # characters of each earlier part's report shown to the next parts
CHECK_TAIL = 2000    # characters of the check command's output kept for the review

PLAN = """Plan the request below for a staged run. Investigate the code only as much as planning needs (read-only), then reply with ONLY one JSON object, no other text:
{{"split": true, "parts": ["...", "..."], "check": "..."}}
- split=false (and no parts) when the request is a question, small, or one tightly coupled change: it is then done as one normal turn. Several wishes about the same file (one window, one script, one config) are one change, however many they are: split=false.
- Otherwise 2-{max} ordered parts. Each part is done by a separate agent that sees only its text and the overall request, so name the files, functions and exact expected result in it, in 1-3 sentences, with paths relative to the working folder. Keep coupled changes (a signature and all its callers) in one part.
- check: one shell command that verifies the whole result (tests, a build, a script run), or "" when there is none.

Request:
{task}"""

PART = """Overall request (context only: other agents do the other parts):
{task}

Plan:
{plan}
{earlier}
Your part ({k} of {n}): {part}

Do this part completely and keep changes scoped to it. Run a targeted check when it is useful. Report changed paths, what changed and the checks you ran."""

REVIEW = """Independently check whether the request below is fully done. Do not trust the reports: read the changed code and look for what is missing, broken or only claimed. Reply with ONLY one JSON object, no other text:
{{"done": true, "problems": ["concrete problem: path, what is wrong or missing"]}}

Request:
{task}

Reports:
{reports}
{check}"""

FIX = """Overall request:
{task}

An independent review found these problems in the current work:
{problems}

Fix them completely, keep unrelated code as it is, and report changed paths and the checks you ran."""


TRUNCATED = "[report truncated]"     # subagent._report's marker


def _json(text):
    """The JSON object a child replied with, or None. A reply cut by the report cap is closed by repair_json."""
    s = str(text or "").replace(TRUNCATED, "").rstrip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0:
        return None
    obj = repair_json(s[start:end + 1]) if start < end else None
    if not isinstance(obj, dict):
        obj = repair_json(s[start:])
    return obj if isinstance(obj, dict) else None


def _clip(text, n):
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + " …"


def plan_of(report):
    """(parts, check) from the planner's reply; parts == [] means "do it as one normal turn"."""
    obj = _json(report)
    if obj is None:
        return None, ""
    parts = [str(p).strip() for p in obj.get("parts") or [] if str(p).strip()][:MAX_PARTS]
    if TRUNCATED in str(report) and parts:
        parts.pop()          # the last part was cut mid-sentence: a half task is worse than none
    check = str(obj.get("check") or "").strip()
    if obj.get("split") is False or len(parts) < 2:
        return [], check
    return parts, check


async def _check(command, tools):
    """Run the plan's check through the subagent gate (hooks, deny/ask rules, dangerous commands)."""
    if not command:
        return ""
    out = await subagent.exec(SHELL_TOOL, {"command": command}, allow_write=True, tool_specs=tools)
    out = str(out or "").strip()
    return out if len(out) <= CHECK_TAIL else "…" + out[-CHECK_TAIL:]


async def _plan(task, common, activity=None):
    """The planner runs without a tab or a saved session: most requests are not split, and then the
    staged run must leave no trace in the chat (no notes, no "Plan the request" tab)."""
    result = await subagent.run(PLAN.format(task=task, max=MAX_PARTS), **common, report_chars=10000,
                                force_readonly=True, activity=activity)
    return subagent.limit_report(result, 10000)


async def run(task, parent_sid, prefix, tools, model=None, progress=None):
    """Run the staged work for `task`; return the report for the main agent, or None for a normal turn."""
    async def say(text, note=True):
        # note=False: only the busy line shows it, the chat transcript does not
        if progress:
            await progress(text, note)

    def live(stage):
        """Each subagent step goes to the busy line only (`stage · step N · tool …`), never to the chat."""
        async def tell(text):
            await say(f"ultra · {stage} · {text}", note=False)
        return tell

    # ctx: a part works for up to PART_STEPS steps; trimming its history at the small subagent budget
    # rewrote cached messages again and again (each trim re-reads the child's history) and dropped
    # files it had read. It gets the room the window leaves after the shared prefix instead.
    common = {"parent_sid": parent_sid, "prefix": prefix, "tools": tools, "model": model,
              "ctx": config.CTX_WINDOW}
    await say("ultra · planning", note=False)
    parts, check = plan_of(await _plan(task, common, live("planning")))
    if not parts:          # a question, a small task or an unusable plan: a normal turn, silently
        return None

    plan = "\n".join(f"{i}. {p}" for i, p in enumerate(parts, 1))
    await say(f"ultra · plan, {len(parts)} parts:\n{plan}" + (f"\ncheck: {check}" if check else ""))
    reports = []
    for k, part in enumerate(parts, 1):
        await say(f"ultra · part {k}/{len(parts)}: {_clip(part, 120)}")
        earlier = "".join(f"\nPart {i} report: {_clip(r, EARLIER)}" for i, r in enumerate(reports, 1))
        prompt = PART.format(task=task, plan=plan, earlier=earlier + ("\n" if earlier else ""),
                             k=k, n=len(parts), part=part)
        reports.append(await subagent.run_child(prompt, **common, allow_write=True, report_chars=2500,
                                                index=k, max_steps=PART_STEPS,
                                                activity=live(f"part {k}/{len(parts)}")))

    problems, checked, index = [], "", len(parts) + 1
    for rnd in range(ROUNDS + 1):
        if check:
            await say(f"ultra · check: {check}")
            checked = await _check(check, tools)
        await say("ultra · independent review")
        listing = "\n".join(f"[{i}] {_clip(r, 1200)}" for i, r in enumerate(reports, 1))
        verdict = _json(await subagent.run_child(
            REVIEW.format(task=task, reports=listing,
                          check=f"\nCheck `{check}` output:\n{checked}" if check else ""),
            **common, report_chars=3000, index=index, activity=live("independent review")))
        index += 1
        if verdict is None:
            problems = ["the review did not return a verdict"]
            break
        problems = [str(p).strip() for p in verdict.get("problems") or [] if str(p).strip()]
        if verdict.get("done") is True and not problems:
            break
        problems = problems or ["the review says the request is not done yet"]
        if rnd == ROUNDS:
            break
        await say(f"ultra · fix round {rnd + 1}: {len(problems)} problem(s)")
        fix = await subagent.run_child(FIX.format(task=task, problems="\n".join(f"- {p}" for p in problems)),
                                       **common, allow_write=True, report_chars=2500, index=index,
                                       max_steps=PART_STEPS, activity=live(f"fix round {rnd + 1}"))
        reports.append(fix)
        index += 1

    await say("ultra · review: done" if not problems else f"ultra · review: {len(problems)} open problem(s)")
    lines = ["<ultra_report>",
             "A staged run (ultra effort) already worked on this request: a plan, each part in its own subagent, "
             "a check and an independent review.", "Plan:", plan, "Reports:"]
    lines += [f"[{i}] {_clip(r, 1500)}" for i, r in enumerate(reports, 1)]
    if check:
        lines += [f"Check `{check}`:", checked or "(no output)"]
    lines.append("Review: done" if not problems else "Review, open problems:\n" + "\n".join(f"- {p}" for p in problems))
    lines.append("Finish the request: briefly verify the key results yourself, fix what is still open, then give "
                 "the final answer. Do not redo finished parts.")
    lines.append("</ultra_report>")
    return "\n".join(lines)


def enabled(effort, mode):
    """Ultra stages only real work: plan mode never writes, so it stays a normal (high-effort) turn."""
    return str(effort or "").lower() == "ultra" and mode != "plan"


def attach(message, report):
    """Add the report to the user message that has not been sent yet (cache-safe: no new message)."""
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = [*content, {"type": "text", "text": "\n\n" + report}]
    else:
        message["content"] = f"{content or ''}\n\n{report}"
