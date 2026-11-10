"""Long autonomous turns: compaction has no fixed count, the checklist outlives a summary, a child
that is cut off still reports what it did, and CLI feedback rides inside the last tool result."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-autonomy-tests-")
os.environ.setdefault("AGENT_HOME", _HOME.name)

from maxedcode import compact, config, subagent  # noqa: E402
from maxedcode.util import LATE_OPEN  # noqa: E402
from maxedcode.shelltool import NAME as SHELL  # noqa: E402  (powershell on Windows, bash on Linux/macOS)

GOOD = "# Objective\nx\n# Next Move\ny\n# Relevant Files\na.py"


class GateTests(unittest.TestCase):
    def test_no_fixed_number_of_compactions(self):
        gate = compact.Gate()
        done = 0
        for _ in range(30):                     # a long turn: the window fills again and again
            if gate.due(95):
                gate.ok()
                done += 1
        self.assertGreater(done, 2)             # the old cap was two per turn
        self.assertLessEqual(done, compact.MAX_PER_TURN)

    def test_one_step_between_compactions(self):
        gate = compact.Gate()
        self.assertTrue(gate.due(95))
        gate.ok()
        self.assertFalse(gate.due(95))          # the kept tail alone may still be above the threshold
        self.assertTrue(gate.due(95))

    def test_below_threshold_never_due(self):
        self.assertFalse(compact.Gate().due(compact.threshold() - 1))

    def test_failures_back_off_but_never_stop_for_good(self):
        gate = compact.Gate()
        waits = []
        for _ in range(3):
            self.assertTrue(gate.due(95))
            gate.failed()
            n = 0
            while not gate.due(95):
                n += 1
            waits.append(n)
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])
        gate.ok()
        self.assertEqual(gate.failures, 0)

    def test_runaway_turn_is_bounded(self):
        gate = compact.Gate()
        gate.done = compact.MAX_PER_TURN
        self.assertFalse(gate.due(99))


class FillTests(unittest.TestCase):
    def test_growth_after_the_measurement_is_added(self):
        # The server measured 16000 for a request we estimated at 13000; since then 2000 (estimated) arrived.
        used = compact.used_tokens(15000, 16000, 0, est_sent=13000)
        self.assertGreater(used, 16000 + 2000)           # scaled by the measured/estimated ratio
        self.assertLessEqual(used, 16000 + 2 * 2000)

    def test_old_behaviour_without_a_measurement(self):
        self.assertEqual(compact.used_tokens(9000, 0, 0, est_sent=0), 9000)
        self.assertEqual(compact.used_tokens(9000, 8000, 0), 9000)
        self.assertEqual(compact.used_tokens(7000, 8000, 0), 8000)

    def test_estimate_wins_when_larger(self):
        # The server counted less than we estimated (ratio < 1): the growth is not shrunk, the estimate holds.
        self.assertEqual(compact.used_tokens(30000, 10000, 0, est_sent=12000), 30000)

    def test_due_one_step_earlier(self):
        # X1 at a 16k window: the server said 16288, our estimate had fallen to 12900.
        self.assertGreaterEqual(compact.fill_pct(13500, 16288, 0, 16384, est_sent=12900), config.COMPACT_AT)


class ThresholdTests(unittest.TestCase):
    def test_large_window_keeps_compact_at(self):
        for w in (49_152, 65_536, 131_072):
            self.assertEqual(compact.threshold(w), config.COMPACT_AT)

    def test_small_window_compacts_while_the_inplace_summary_still_fits(self):
        for w in (16_384, 24_576, 32_768):
            at = compact.threshold(w)
            self.assertLess(at, config.COMPACT_AT)
            self.assertTrue(compact.inplace_fits(w * at // 100, w))


class HeadlessChecklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from maxedcode import headless
        self.h = headless
        self.tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        prefix = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "<env>x</env>"}]
        for name, value in (("PREFIX", prefix), ("HEAD", len(prefix)), ("current_tool_specs", lambda: self.tools)):
            p = patch.object(headless, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.prefix = prefix
        headless.TODOS.clear()
        self.addCleanup(headless.TODOS.clear)

    def client(self):
        async def create(**kw):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=GOOD, tool_calls=None))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def history(self):
        steps = []
        for i in range(10):
            steps += [{"role": "assistant", "content": "", "tool_calls": [
                          {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000}]
        return self.prefix + [{"role": "user", "content": '<user_input mode="act">do it</user_input>'}] + steps

    async def test_todo_write_is_tracked(self):
        todos = [{"content": "fix bugs", "status": "done"}, {"content": "add renew", "status": "in_progress"}]
        with patch.object(self.h, "ALLOWED", None):
            out = await self.h.exec_tool("todo_write", {"todos": todos})
        self.assertFalse(str(out).startswith("Error"), out)
        self.assertEqual([t["status"] for t in self.h.TODOS], ["done", "in_progress"])

    async def test_open_items_survive_the_summary(self):
        self.h.TODOS[:] = [{"content": "fix bugs", "status": "done"}, {"content": "add renew", "status": "in_progress"},
                           {"content": "update README", "status": "pending"}]
        m = self.history()
        self.assertTrue(await self.h.compact_history(self.client(), m, "sid"))
        last = m[-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith("<pending_work>"))
        self.assertIn("add renew", last["content"])
        self.assertIn("update README", last["content"])
        self.assertNotIn("fix bugs", last["content"])
        # A second compaction replaces the old checklist instead of stacking another one.
        m += [{"role": "assistant", "content": "", "tool_calls": [{"id": "z", "type": "function",
               "function": {"name": "read_file", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "z", "content": "y" * 30000}]
        self.assertTrue(await self.h.compact_history(self.client(), m, "sid"))
        self.assertEqual(sum(1 for x in m if str(x.get("content") or "").startswith("<pending_work>")), 1)

    def test_system_prompt_is_counted_once(self):
        from maxedcode.util import tok
        system = "word " * 4000
        prefix = [{"role": "system", "content": system}, {"role": "user", "content": "<env>x</env>"}]
        with patch.object(self.h, "SYSTEM", system), patch.object(self.h, "PREFIX", prefix), \
                patch.object(self.h, "HEAD", 2), patch.object(self.h, "USAGE", {}):
            est = self.h.fill_pct(list(prefix), self.tools)[1]
        self.assertLess(est, tok(system) * 1.5)

    async def test_history_under_the_tail_budget_still_compacts(self):
        # Small window: the window is full while the whole history is below COMPACT_KEEP_TOKENS.
        steps = []
        for i in range(4):
            steps += [{"role": "assistant", "content": "", "tool_calls": [
                          {"id": f"s{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": f"s{i}", "content": "x" * 800}]
        m = self.prefix + [{"role": "user", "content": '<user_input mode="act">do it</user_input>'}] + steps
        self.assertIsNone(compact.cut_index(m, len(self.prefix)))
        self.assertTrue(await self.h.compact_history(self.client(), m, "sid"))
        self.assertEqual([x["role"] for x in m[len(self.prefix):]], ["user", "assistant", "tool"])
        self.assertEqual(m[-2]["tool_calls"][0]["id"], "s3")

    async def test_no_checklist_no_pending_block(self):
        m = self.history()
        self.assertTrue(await self.h.compact_history(self.client(), m, "sid"))
        self.assertFalse(any(str(x.get("content") or "").startswith("<pending_work>") for x in m))


def call(cid, name, args):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))


class ScriptedCompletions:
    """Replies in order; records each request's messages."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def create(self, **kw):
        self.calls.append([dict(m) for m in kw["messages"]])
        content, calls = self.replies.pop(0)
        msg = SimpleNamespace(content=content, tool_calls=calls, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class ChildLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.box = tempfile.TemporaryDirectory()
        self.addCleanup(self.box.cleanup)
        self.path = os.path.join(self.box.name, "a.txt")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("hello\n")
        self.tools = [t for t in subagent.BUILTIN if t["function"]["name"] in ("read_file", "write_file")]
        self.prefix = [{"role": "system", "content": "# Memory\nm"}, {"role": "user", "content": "<env>e</env>"}]

    async def run_child(self, replies, **kw):
        comp = ScriptedCompletions(replies)
        client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
        with patch.object(subagent, "AsyncOpenAI", return_value=client):
            out = await subagent.run("do the part", prefix=self.prefix, parent_sid="sid", tools=self.tools,
                                     model="m", allow_write=True, **kw)
        return out, comp

    async def test_text_tool_call_is_run(self):
        text = ('<tool_call>{"name": "read_file", "arguments": {"path": "%s"}}</tool_call>' % self.path.replace("\\", "/"))
        out, comp = await self.run_child([(text, None), ("done: a.txt says hello", None)])
        self.assertIn("hello", out)
        self.assertEqual(len(comp.calls), 2)
        tool_msgs = [m for m in comp.calls[1] if m.get("role") == "tool"]
        self.assertTrue(tool_msgs and "hello" in tool_msgs[-1]["content"])

    async def test_empty_reply_is_retried_once(self):
        out, comp = await self.run_child([("", None), ("found it", None)])
        self.assertEqual(out, "found it")
        self.assertEqual(len(comp.calls), 2)

    async def test_cut_off_child_reports_what_it_changed(self):
        target = os.path.join(self.box.name, "b.txt")
        args = '{"path": "%s", "content": "new\\n"}' % target.replace("\\", "/")
        replies = [("", [call("w", "write_file", args)]),
                   ("", [call("r", "read_file", '{"path": "%s"}' % self.path.replace("\\", "/"))]),
                   ("", [call("r2", "read_file", '{"path": "%s"}' % self.path.replace("\\", "/"))])]
        out, comp = await self.run_child(replies, max_steps=3)
        # The last step carried the wrap-up note and its tool call did not run.
        last_request = comp.calls[-1]
        self.assertTrue(any(LATE_OPEN in str(m.get("content")) and "last step" in str(m.get("content"))
                            for m in last_request))
        self.assertIn("b.txt", out)
        self.assertTrue(os.path.exists(target))

    async def test_server_error_returns_a_partial_report(self):
        from openai import BadRequestError
        err = BadRequestError.__new__(BadRequestError)   # no HTTP response needed for the loop's handler
        Exception.__init__(err, "context length exceeded")

        class Failing(ScriptedCompletions):
            async def create(self, **kw):
                if self.calls:
                    raise err
                return await super().create(**kw)

        target = os.path.join(self.box.name, "c.txt")
        comp = Failing([("", [call("w", "write_file", '{"path": "%s", "content": "x\\n"}' % target.replace("\\", "/"))])])
        client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
        with patch.object(subagent, "AsyncOpenAI", return_value=client):
            out = await subagent.run("do it", prefix=self.prefix, parent_sid="sid", tools=self.tools,
                                     model="m", allow_write=True)
        self.assertIn("server rejected", out)
        self.assertIn("c.txt", out)

    async def test_garbled_call_name_is_recovered_from_its_parameters(self):
        # Live run: `<2>` where `<function=read_file>` belongs came back as the child's "report".
        text = ("<tool_call>\n<2>\n<parameter=path>\n%s\n</parameter>\n</function>\n</tool_call>"
                % self.path.replace("\\", "/"))
        out, comp = await self.run_child([(text, None), ("a.txt says hello", None)])
        self.assertEqual(out, "a.txt says hello")
        self.assertTrue(any(m.get("role") == "tool" and "hello" in str(m.get("content")) for m in comp.calls[1]))

    async def test_unreadable_call_is_retried_not_reported(self):
        broken = "<tool_call>\n<=powerShell>\n<parameter=command>\n</function>\n</tool_call>"
        out, comp = await self.run_child([(broken, None), ("the real report", None)])
        self.assertEqual(out, "the real report")
        # The step went again unchanged: no feedback message moved the last user message.
        self.assertEqual(comp.calls[0], comp.calls[1])

    async def test_unreadable_calls_never_become_the_report(self):
        broken = "<tool_call>\n<=powerShell>\n<parameter=command>\n</function>\n</tool_call>"
        out, _ = await self.run_child([(broken, None)] * 3)
        self.assertNotIn("<tool_call>", out)
        self.assertIn("could not be read", out)

    async def test_last_step_tools_are_named_in_the_report(self):
        read = '{"path": "%s"}' % self.path.replace("\\", "/")
        replies = [("", [call(f"r{i}", "read_file", read)]) for i in range(3)]
        out, _ = await self.run_child(replies, max_steps=3)
        self.assertIn("did not run", out)
        self.assertIn("read_file", out)

    async def test_steps_left_note_comes_before_the_wrap_up(self):
        read = '{"path": "%s"}' % self.path.replace("\\", "/")
        replies = [("", [call(f"r{i}", "read_file", read)]) for i in range(5)] + [("report", None)]
        _, comp = await self.run_child(replies, max_steps=6)
        noted = [i for i, req in enumerate(comp.calls) if any("Two steps are left" in str(m.get("content")) for m in req)]
        self.assertEqual(noted[0], 3)

    async def test_write_records_the_real_diff_for_the_tab(self):
        args = '{"path": "%s", "content": "bye\\n"}' % self.path.replace("\\", "/")
        sid = "agent-test-diff"
        replies = [("", [call("w", "write_file", args)]), ("done", None)]
        comp = ScriptedCompletions(replies)
        client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
        saved = {}
        with patch.object(subagent, "AsyncOpenAI", return_value=client), \
                patch.object(subagent, "_update_child_session", lambda *a, **k: saved.update(msgs=a[4])):
            await subagent.run("do the part", prefix=self.prefix, parent_sid="sid", tools=self.tools,
                               model="m", allow_write=True, session_id=sid)
        owner = next(m for m in saved["msgs"] if m.get("edit_changes"))
        self.assertEqual(owner["edit_changes"]["w"], ["hello\n", "bye\n"])
        # The diff is for the tab only: the next request does not carry it.
        self.assertFalse(any("edit_changes" in m for m in comp.calls[1]))

    async def test_huge_tool_output_is_capped_like_the_main_loop(self):
        big = "x" * 300_000
        with patch.object(subagent, "exec", return_value=big):
            out = await subagent._exec_call({"name": SHELL}, {"command": "dir"}, True, self.tools)
        self.assertLess(len(out), 30_000)
        self.assertIn("output truncated", out)

    async def test_writing_child_may_write_from_python(self):
        async def fake_py(code):
            return "ran"
        with patch.object(subagent.runners, "run_py", fake_py):
            ok = await subagent.exec("py_run", {"code": "open('x.txt', 'w').write('1')"}, allow_write=True,
                                     tool_specs=subagent.BUILTIN)
            no = await subagent.exec("py_run", {"code": "open('x.txt', 'w').write('1')"}, allow_write=False,
                                     tool_specs=subagent.BUILTIN)
        self.assertEqual(ok, "ran")
        self.assertNotEqual(no, "ran")

    def test_fresh_and_small_results_are_not_trimmed(self):
        head = len(self.prefix) + 1
        msgs = list(self.prefix) + [{"role": "user", "content": "task"}]
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "e", "type": "function",
                  "function": {"name": "write_file", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "e", "content": "Error: missing content " + "y" * 140}]
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"f{i}", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}} for i in range(2)]}]
        msgs += [{"role": "tool", "tool_call_id": f"f{i}", "content": "z" * 24_000} for i in range(2)]
        base = [{"role": "system", "content": "S"}, *self.prefix]
        before = [dict(m) for m in msgs]
        # No explicit limit: the window decides, and two just-read files fit in it.
        self.assertEqual(subagent._trim_history(msgs, head, self.tools, base), 0)
        self.assertEqual(msgs, before)
        # Under a tight explicit limit old output goes, but a short result is never swapped for a marker:
        # that saves nothing and costs a re-read of everything after it.
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"g{i}", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]} for i in range(1)]
        msgs += [{"role": "tool", "tool_call_id": "g0", "content": "ok"}] * 6
        subagent._trim_history(msgs, head, self.tools, base, 1000)
        self.assertTrue(msgs[head + 1]["content"].startswith("Error: missing content"))
        self.assertTrue(any(str(m.get("content")).startswith("[subagent history trimmed") for m in msgs))

    def test_trim_goes_well_below_the_budget(self):
        head = len(self.prefix) + 1
        msgs = list(self.prefix) + [{"role": "user", "content": "task"}]
        for i in range(20):
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}", "type": "function",
                      "function": {"name": "read_file", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"t{i}", "content": "x" * 2000}]
        base = [{"role": "system", "content": "S"}, *self.prefix]
        first = subagent._trim_history(msgs, head, self.tools, base, 4000)
        self.assertGreater(first, 0)
        # One more step after a trim does not trim again: there is room left.
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "n", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "n", "content": "x" * 2000}]
        self.assertEqual(subagent._trim_history(msgs, head, self.tools, base, 4000), 0)


class BrokenCallTests(unittest.TestCase):
    def test_unreadable_markup_is_a_failed_call(self):
        from maxedcode.tool_validation import broken_text_call, text_tool_calls
        reply = "<tool_call>\n<=powerShell>\n<parameter=command>\n</function>\n</tool_call>"   # real X3 reply
        self.assertEqual(text_tool_calls(reply, "t", [])[0], [])
        self.assertTrue(broken_text_call(reply))
        # A garbled name with no parameter read is still a failed call, never a guess.
        self.assertEqual(text_tool_calls(reply, "t", subagent.BUILTIN)[0], [])

    def test_garbled_names_resolve_only_to_one_fitting_tool(self):
        from maxedcode.tool_validation import text_tool_calls
        shell = "<tool_call>\n<2>\n<parameter=command>\nnode -v\n</parameter>\n</function>\n</tool_call>"
        patch_ = ("<tool_call>\n<=pack>\n<parameter=path>\nC:/t/a.js\n</parameter>\n<parameter=blocks>\n"
                  '[{"old_text": "a", "new_text": "b"}]\n</parameter>\n</function>\n</tool_call>')
        ambiguous = ("<tool_call>\n<x>\n<parameter=pattern>\n*.py\n</parameter>\n<parameter=path>\n.\n"
                     "</parameter>\n</function>\n</tool_call>")   # grep and glob both fit
        self.assertEqual(text_tool_calls(shell, "t", subagent.BUILTIN)[0][0]["function"]["name"], SHELL)
        found = text_tool_calls(patch_, "t", subagent.BUILTIN)[0][0]["function"]
        self.assertEqual((found["name"], '"blocks": [{' in found["arguments"]), ("patch", True))
        self.assertEqual(text_tool_calls(ambiguous, "t", subagent.BUILTIN)[0], [])

    def test_plain_answers_are_not_flagged(self):
        from maxedcode.tool_validation import broken_text_call
        for text in ("Готово: все тесты проходят.", "Use `tool_call` ids from the log.", "a < b and c > d"):
            self.assertFalse(broken_text_call(text), text)


class ResearchBudgetTests(unittest.TestCase):
    def _turn(self, *names):
        msgs = [{"role": "user", "content": "<user_input>q</user_input>"}]
        for i, n in enumerate(names):
            msgs += [{"role": "assistant", "content": "", "tool_calls": [
                         {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": "ok"}]
        return msgs

    def test_plan_cap_after_local_reads_only(self):
        self.assertTrue(config.planning_step(self._turn("read_file", "read_file")))
        self.assertFalse(config.planning_step(self._turn("web_search")))
        self.assertFalse(config.planning_step(self._turn("read_file", "web_search", "fetch_url")))
        # The step after web results keeps the normal medium cap, not the high one.
        msgs = self._turn("web_search", "web_search")
        b = config.think_budget(0, "medium", step=config.quick_step(msgs), plan=config.planning_step(msgs))
        self.assertLessEqual(b, config.THINK_CAP)


class PhraseLoopTests(unittest.TestCase):
    def test_repeated_phrase_is_a_loop(self):
        from maxedcode.util import think_loop
        looped = "Hmm, what about water polo? " + "Enough! Let me consider **Argan? no.** " * 15   # live X5 tail
        self.assertTrue(think_loop(looped))

    def test_hmm_brainstorm_is_not(self):
        from maxedcode.util import think_loop
        sports = ["handball", "rugby", "water polo", "basketball", "korfball", "netball", "lacrosse", "hurling"]
        text = " ".join(f"Hmm. What about {s}? No. Hmm." for s in sports)
        self.assertFalse(think_loop(text))

    def test_varied_brainstorm_is_not(self):
        from maxedcode.util import think_loop
        text = " ".join(f"What about sport number {i}? Team {i} plays at home. Score {i} is unusual." for i in range(20))
        self.assertFalse(think_loop(text))
        prose = ("The function reads the file. Then it parses each line into fields. A missing field raises. "
                 "The caller retries once. If that fails, it logs the error and moves on. ") * 3
        self.assertFalse(think_loop(prose))


def _chunk(reasoning=None, content=None, tc=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tc, model_extra={"reasoning_content": reasoning} if reasoning else {})
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)] if usage is None else [],
                           usage=usage)


class FakeStream:
    def __init__(self, chunks):
        self.chunks, self.sent, self.closed = list(chunks), 0, False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed or not self.chunks:
            raise StopAsyncIteration
        self.sent += 1
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class HeadlessStreamTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result):
        self.kw = {}

        async def create(**kw):
            self.kw = kw
            return result
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def test_tool_call_is_assembled_from_chunks(self):
        from maxedcode import headless
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, model_dump=lambda: {})
        tc = lambda **f: [SimpleNamespace(index=0, id=f.pop("id", None), function=SimpleNamespace(**f))]   # noqa: E731
        stream = FakeStream([_chunk(reasoning="Read the file first. "), _chunk(tc=tc(id="c1", name="read_", arguments='{"pa')),
                             _chunk(tc=tc(name="file", arguments='th": "a.py"}')), _chunk(finish="tool_calls"),
                             _chunk(usage=usage)])
        r, looped = await headless.stream_completion(self.client(stream), model="m", messages=[])
        self.assertFalse(looped)
        self.assertTrue(self.kw["stream"])
        call = r.choices[0].message.tool_calls[0]
        self.assertEqual((call.id, call.function.name, call.function.arguments), ("c1", "read_file", '{"path": "a.py"}'))
        self.assertEqual(r.choices[0].message.reasoning_content, "Read the file first. ")
        self.assertEqual(r.usage.prompt_tokens, 100)

    async def test_looping_reasoning_is_cut_early(self):
        from maxedcode import headless
        chunks = [_chunk(reasoning="Enough! Let me consider Argan? no. ") for _ in range(400)]
        stream = FakeStream(chunks)
        r, looped = await headless.stream_completion(self.client(stream), model="m", messages=[])
        self.assertTrue(looped)
        self.assertTrue(stream.closed)
        self.assertLess(stream.sent, 100)          # stopped long before the 400 chunks (the budget)

    async def test_a_complete_response_is_used_as_is(self):
        from maxedcode import headless
        done = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))], usage=None)
        r, looped = await headless.stream_completion(self.client(done), model="m", messages=[])
        self.assertIs(r, done)
        self.assertFalse(looped)


class LateFeedbackTests(unittest.TestCase):
    """CLI feedback goes into the freshest tool result, so the turn is not re-read by the server."""

    def test_app_and_headless_use_attach_late_for_mid_turn_feedback(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "maxedcode"
        app = (root / "ui" / "app.py").read_text(encoding="utf-8")
        head = (root / "headless.py").read_text(encoding="utf-8")
        for src in (app, head):
            self.assertNotIn('"content":\n                "<tool_validation_feedback>', src)
            self.assertNotIn('{"role": "user", "content": "<finish_tool_feedback>', src)
        self.assertNotIn('"role": "system",\n                        "content": (\n                            "[CLI TASK CHECKPOINT]', app)
        self.assertNotIn("auto_compacts < 2", app)
        self.assertNotIn("COMPACT_ATTEMPTS < 2", head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
