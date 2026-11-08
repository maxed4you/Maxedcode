"""Ultra effort: level names, wire mapping, staged run (plan -> parts -> check -> review -> fix).

Run from the agent folder: python -m unittest tests.test_ultra -v
"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_TEST_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-tests-")
os.environ["AGENT_HOME"] = _TEST_HOME.name

from maxedcode import config, state, subagent, ultra


class EffortLevelTests(unittest.TestCase):
    def test_levels_have_no_default_or_none(self):
        self.assertEqual(state.EFFORTS, ("off", "low", "medium", "high", "ultra"))

    def test_legacy_values_map_to_new_levels(self):
        self.assertEqual(config.norm_effort("none"), "off")
        self.assertEqual(config.norm_effort("default"), "medium")
        self.assertEqual(config.norm_effort(None), "medium")
        self.assertEqual(config.norm_effort(" ULTRA "), "ultra")

    def test_wire_values(self):
        self.assertEqual(config.effort_body("off")["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(config.effort_body("ultra"), {"reasoning_effort": "high"})
        self.assertEqual(config.think_budget(0, "ultra"), config.think_budget(0, "high"))
        self.assertEqual(config.think_budget(0, "off"), 0)
        self.assertEqual(config.summary_effort("ultra"), "low")
        self.assertEqual(config.summary_effort("off"), "none")

    def test_enabled_only_outside_plan_mode(self):
        self.assertTrue(ultra.enabled("ultra", "act"))
        self.assertFalse(ultra.enabled("ultra", "plan"))
        self.assertFalse(ultra.enabled("high", "act"))


class PlanParsingTests(unittest.TestCase):
    def test_plan_with_text_around_json(self):
        parts, check = ultra.plan_of('Here: {"split": true, "parts": ["a", "b", " "], "check": "pytest"} done')
        self.assertEqual((parts, check), (["a", "b"], "pytest"))

    def test_small_task_is_not_split(self):
        self.assertEqual(ultra.plan_of('{"split": false, "parts": []}'), ([], ""))
        self.assertEqual(ultra.plan_of('{"split": true, "parts": ["only one"]}'), ([], ""))

    def test_plan_cut_by_the_report_cap_keeps_whole_parts(self):
        # Live run: a long plan hit the report cap mid-string and the whole plan was thrown away.
        cut = '{"split": true, "parts": ["part one", "part two", "part three is cut he\n[report truncated]'
        self.assertEqual(ultra.plan_of(cut), (["part one", "part two"], ""))

    def test_invalid_plan(self):
        self.assertEqual(ultra.plan_of("no json here"), (None, ""))

    def test_attach_keeps_one_message(self):
        m = {"role": "user", "content": "task"}
        ultra.attach(m, "<ultra_report>x</ultra_report>")
        self.assertTrue(m["content"].startswith("task") and m["content"].endswith("</ultra_report>"))
        m = {"role": "user", "content": [{"type": "text", "text": "task"}]}
        ultra.attach(m, "R")
        self.assertEqual(m["content"][-1]["text"].strip(), "R")


class StagedRunTests(unittest.TestCase):
    def run_with(self, replies, check_out="ok"):
        calls, lines = [], []

        async def child(prompt, **kw):
            calls.append((prompt, kw))
            return replies.pop(0)

        async def check(name, a, allow_write=False, tool_specs=None):
            calls.append(("CHECK " + a["command"], {"allow_write": allow_write}))
            return check_out

        async def planner(prompt, **kw):
            calls.append((prompt, {**kw, "planner": True}))
            return replies.pop(0)

        async def progress(line, note=True):
            lines.append((line, note))

        with mock.patch.object(subagent, "run_child", child), mock.patch.object(subagent, "run", planner), \
                mock.patch.object(subagent, "exec", check):
            report = asyncio.run(ultra.run("do X", "sid", [{"role": "system", "content": "s"}], [], progress=progress))
        return report, calls, [line for line, _ in lines], [line for line, note in lines if note]

    def test_small_task_returns_none(self):
        report, calls, _, notes = self.run_with(['{"split": false}'])
        self.assertIsNone(report)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["planner"])               # no tab: subagent.run, not run_child
        self.assertTrue(calls[0][1]["force_readonly"])        # the planner is read-only
        self.assertFalse(calls[0][1].get("allow_write"))
        self.assertEqual(notes, [])                           # a normal turn leaves nothing in the chat

    def test_planner_steps_reach_the_busy_line_only(self):
        lines = []

        async def planner(prompt, **kw):
            await kw["activity"]("step 1 · grep modal")
            return '{"split": false}'

        async def progress(line, note=True):
            lines.append((line, note))

        with mock.patch.object(subagent, "run", planner):
            asyncio.run(ultra.run("do X", "sid", [{"role": "system", "content": "s"}], [], progress=progress))
        self.assertIn(("ultra · planning · step 1 · grep modal", False), lines)
        self.assertFalse(any(note for _, note in lines))

    def test_call_brief_is_short(self):
        self.assertEqual(subagent._call_brief("read_file", {"path": r"C:\Modal.tsx"}), "read_file Modal.tsx")
        self.assertEqual(subagent._call_brief("grep", {"pattern": "x" * 60}), "grep " + "x" * 40 + "…")
        self.assertEqual(subagent._call_brief("finish", {}), "finish")

    def test_invalid_plan_is_silent(self):
        report, _, lines, notes = self.run_with(["no json"])
        self.assertIsNone(report)
        self.assertEqual(lines, ["ultra · planning"])         # busy line only
        self.assertEqual(notes, [])

    def test_parts_are_numbered_from_one(self):
        _, calls, _, notes = self.run_with([
            '{"split": true, "parts": ["a", "b"]}', "a", "b", '{"done": true, "problems": []}'])
        self.assertEqual([c[1]["index"] for c in calls[1:]], [1, 2, 3])   # A1, A2, then the reviewer
        self.assertTrue(notes[0].startswith("ultra · plan, 2 parts"))

    def test_parts_then_review_done(self):
        report, calls, _, _ = self.run_with([
            '{"split": true, "parts": ["part A", "part B"], "check": "python -m pytest"}',
            "A done", "B done", '{"done": true, "problems": []}'])
        prompts = [c[0] for c in calls]
        self.assertIn("Your part (1 of 2): part A", prompts[1])
        self.assertIn("Part 1 report: A done", prompts[2])           # later parts see earlier reports
        self.assertTrue(calls[1][1]["allow_write"] and calls[2][1]["allow_write"])
        self.assertEqual(calls[1][1]["max_steps"], ultra.PART_STEPS)
        self.assertEqual(prompts[3], "CHECK python -m pytest")
        self.assertFalse(calls[4][1].get("allow_write"))             # the reviewer is read-only
        self.assertIn("Review: done", report)
        self.assertIn("[2] B done", report)

    def test_review_problems_go_to_a_fix_round(self):
        report, calls, lines, _ = self.run_with([
            '{"split": true, "parts": ["a", "b"], "check": ""}', "a ok", "b ok",
            '{"done": false, "problems": ["b.py: missing test"]}', "fixed",
            '{"done": true, "problems": []}'])
        fix = calls[4]
        self.assertIn("- b.py: missing test", fix[0])
        self.assertTrue(fix[1]["allow_write"])
        self.assertIn("[3] fixed", report)
        self.assertIn("Review: done", report)
        self.assertTrue(any("fix round 1" in x for x in lines))

    def test_rounds_are_bounded(self):
        bad = '{"done": false, "problems": ["still broken"]}'
        replies = ['{"split": true, "parts": ["a", "b"]}', "a", "b"]
        replies += [bad, "fix"] * ultra.ROUNDS + [bad]
        report, calls, _, _ = self.run_with(replies)
        self.assertEqual(len(calls), 3 + 2 * ultra.ROUNDS + 1)
        self.assertIn("open problems", report)
        self.assertIn("- still broken", report)


class UltraCacheTests(unittest.TestCase):
    """The staged run must keep the server's prefix cache: same fork point as the turn, no trims, budgets."""

    def test_children_get_the_window_as_history_budget(self):
        seen = []

        async def planner(prompt, **kw):
            seen.append(kw)
            return '{"split": false}'

        with mock.patch.object(subagent, "run", planner):
            asyncio.run(ultra.run("do X", "sid", [{"role": "system", "content": "s"}], []))
        self.assertEqual(seen[0]["ctx"], config.CTX_WINDOW)

    def test_fork_at_the_turn_message_keeps_the_whole_history(self):
        system = {"role": "system", "content": config.SYSTEM}
        history = [system, {"role": "user", "content": "first"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "text"},
                   {"role": "assistant", "content": "the answer"},
                   {"role": "user", "content": "the new request"}]
        tools = [t for t in subagent.BUILTIN if t["function"]["name"] == "read_file"]
        whole = subagent.fork_prefix(history, ["base"], tools, end=5)
        self.assertEqual([m["content"] for m in whole], [m["content"] for m in history[:5]])
        # without end (the subagent tool) it still forks at the last delegating call
        self.assertEqual(len(subagent.fork_prefix(history, ["base"], tools)), 2)

    def test_trim_keeps_room_for_the_reply(self):
        prefix = [{"role": "system", "content": "s"}]
        msgs = [*prefix, {"role": "user", "content": "task"}]
        msgs += [{"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000} for i in range(40)]
        with mock.patch.object(config, "CTX_WINDOW", 20000):
            subagent._trim_history(msgs, 1, [], prefix, config.CTX_WINDOW)
            fixed = subagent._fixed_cost([], prefix)
            used = subagent.compact.est_tokens(msgs, 1, fixed)
        self.assertLessEqual(used, 20000 - config.answer_room("high") - config.THINK_CAP)


class _Stream:
    """A streamed reply: reasoning chunks, then content."""

    def __init__(self, think, text, usage=None):
        chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason=None, delta=SimpleNamespace(model_extra={"reasoning_content": t}, content=None, tool_calls=None))])
            for t in think]
        chunks.append(SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason="stop", delta=SimpleNamespace(model_extra={}, content=text, tool_calls=None))]))
        chunks.append(SimpleNamespace(usage=usage, choices=[]))
        self.chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield c


class ChildStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state.CHILD_SESSIONS.clear()
        subagent.LAST_REQUEST.clear()
        self.prefix = [{"role": "system", "content": "# Memory\nm"}, {"role": "user", "content": "<env>e</env>"}]
        self.tools = [t for t in subagent.BUILTIN if t["function"]["name"] == "read_file"]

    async def run_child(self, create):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(subagent, "AsyncOpenAI", return_value=client):
            return await subagent.run_child("do the part", parent_sid="sid", prefix=self.prefix, tools=self.tools,
                                            model="m", index=1)

    async def test_streamed_reasoning_budget_cache_note_and_last_request(self):
        sent, live = [], []
        usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 12400,
                                                   "prompt_tokens_details": {"cached_tokens": 12100}})

        async def create(**kw):
            sent.append(kw)
            return _Stream(["Read ", "the file."], "done: nothing to change", usage)

        real = subagent.state.upsert_child_session

        def spy(sid, **fields):
            if fields.get("live_think"):
                live.append(fields["live_think"])
            return real(sid, **fields)

        with mock.patch.object(subagent.state, "upsert_child_session", spy):
            out = await self.run_child(create)
        self.assertIn("done", out)
        self.assertTrue(sent[0]["stream"])
        self.assertIn("reasoning_budget_tokens", sent[0]["extra_body"])
        self.assertEqual(live[-1], "Read the file.")                       # the tab saw the reasoning grow
        sid = next(iter(subagent.LAST_REQUEST))
        row = state.CHILD_SESSIONS[sid]
        self.assertEqual(row["cache"], "12.1k/12.4k cached")
        self.assertEqual(row["live_think"], "")                            # saved with the step, live view cleared
        self.assertEqual(subagent.LAST_REQUEST[sid]["messages"], sent[0]["messages"])   # /btw forks from it

    async def test_rejected_budget_is_dropped_and_the_step_retried(self):
        sent = []

        async def create(**kw):
            sent.append(kw)
            if "reasoning_budget_tokens" in kw["extra_body"]:
                raise Rejected("reasoning_budget is not supported")
            return _Stream([], "done")

        class Rejected(Exception):
            pass

        with mock.patch.object(subagent, "BadRequestError", Rejected):
            out = await self.run_child(create)
        self.assertEqual(out, "done")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("reasoning_budget_tokens", sent[1]["extra_body"])

    async def test_looped_reasoning_repeats_the_step_with_a_short_think(self):
        sent = []

        async def create(**kw):
            sent.append(kw)
            if len(sent) == 1:
                return _Stream(["Let me check again. " * 40], "")
            return _Stream(["ok"], "done")

        with mock.patch("maxedcode.streaming.think_loop", side_effect=lambda r: len(sent) == 1):
            out = await self.run_child(create)
        self.assertEqual(out, "done")
        self.assertEqual(sent[1]["extra_body"]["reasoning_budget_tokens"], config.LOOP_RETRY_THINK)
        self.assertEqual(sent[0]["messages"], sent[1]["messages"])          # the looped reply was not kept


if __name__ == "__main__":
    unittest.main()
