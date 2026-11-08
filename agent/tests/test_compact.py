"""Tests for the compaction rules taken from opencode (plan file 13) and the transcript fix (01b).

Run from the agent folder: python -m unittest tests.test_compact -v
"""
import os
import tempfile
import unittest

_TEST_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-tests-")
os.environ["AGENT_HOME"] = _TEST_HOME.name

from maxedcode import compact, config


def msgs():
    return [{"role": "system", "content": "system"},
            {"role": "system", "content": "# Memory\nnote"}]


class ReserveTests(unittest.TestCase):
    def test_reserve_is_share_or_minimum(self):
        self.assertEqual(compact.reserve(100_000), max(compact.RESERVE_MIN, 15_000))
        # A small local window keeps at most a quarter for the summary request, not 16k of 20k.
        self.assertEqual(compact.reserve(20_000), 5_000)
        self.assertEqual(compact.reserve(49_152), 12_288)
        self.assertEqual(compact.reserve(131_072), 19_660)

    def test_whole_history_is_sent_when_it_fits_the_room(self):
        # The summarizer request keeps its own reserve; while the history fits inside it, the
        # transcript is sent as it is, so the request prefix stays byte-identical.
        m = msgs() + [{"role": "user", "content": "x" * 200}]
        room = config.CTX_WINDOW - compact.reserve(config.CTX_WINDOW)
        head = 2
        est = compact.est_tokens(compact.without_tail(m, head), head,
                                 compact.prefix_cost("system", [], m[1:2]))
        self.assertLessEqual(est, room)
        self.assertEqual(compact.without_tail(m, head), m[head:len(m) - config.COMPACT_KEEP])


class FitOutputTests(unittest.TestCase):
    def test_middle_is_cut_head_and_tail_kept(self):
        text = "\n".join(str(i) for i in range(2000))
        out = compact.fit_output(text, 1000)
        self.assertIn(compact.MIDDLE_MARK, out)
        self.assertTrue(out.startswith("0"))
        self.assertTrue(out.endswith("1999"))
        self.assertLessEqual(len(out), 1000 + len(compact.MIDDLE_MARK) + 5)

    def test_short_text_is_returned_unchanged(self):
        self.assertEqual(compact.fit_output("abc", 100), "abc")


class SummaryTemplateTests(unittest.TestCase):
    def test_prose_summary_is_not_filled(self):
        ok, why = compact.usable("The agent looked at files and did some work.")
        self.assertFalse(ok)
        self.assertIn("headings", why)

    def test_template_without_next_move_is_rejected(self):
        ok, why = compact.usable("# Objective\nsee maxedcode/compact.py\n# Work State\ndone")
        self.assertFalse(ok)
        self.assertIn("Next Move", why)

    def test_template_without_a_path_is_rejected(self):
        ok, why = compact.usable("# Objective\nfix the bug\n# Next Move\nrun the tests")
        self.assertFalse(ok)
        self.assertIn("path", why)

    def test_good_summary_passes(self):
        ok, why = compact.usable("# Objective\nx\n# Next Move\nedit files\n# Relevant Files\nmaxedcode/compact.py")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_nudge_is_one_shot(self):
        # One nudge before compaction.failed: the nudge text must not ask the model to continue.
        self.assertIn("sections", compact.NUDGE)
        self.assertIn("no tool calls", compact.NUDGE)


class TranscriptTests(unittest.TestCase):
    def test_path_in_the_middle_survives(self):
        # Old behaviour: [:900] cut threw away a path that sat in the middle of a long message.
        body = "a" * 400 + "\nagent/maxedcode/compact.py line 137\n" + "b" * 1200
        m = msgs() + [{"role": "user", "content": body}]
        tr = compact.transcript(m)
        self.assertIn("agent/maxedcode/compact.py", tr)

    def test_tool_call_with_path_is_not_cut_to_200_chars(self):
        args = '{"path": "agent/maxedcode/compact.py", "old_text": "' + "x" * 300 + '"}'
        m = msgs() + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "edit_file", "arguments": args}}]}]
        tr = compact.transcript(m)
        self.assertIn("agent/maxedcode/compact.py", tr)
        self.assertIn("edit_file(", tr)

    def test_transcript_is_capped_by_the_summarizer_room(self):
        m = msgs() + [{"role": "user", "content": "y" * 60_000}]
        tr = compact.transcript(m, 32_000, compact.reserve(32_000))
        self.assertLessEqual(compact.tok(tr), 32_000)
        self.assertIn("cut", tr)


class ImageCutTests(unittest.TestCase):
    def test_images_are_dropped_only_above_the_trigger(self):
        m = msgs() + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 900_000}}]}]
        sent, dropped = compact.bound_images(m, 2, 1_000_000, 500_000)
        self.assertEqual(dropped, 0)
        self.assertEqual(sent, m)

    def test_cut_stays_stable_inside_the_band(self):
        # Two requests with the same history must produce the same bytes: the cut only moves when
        # the media total passes the trigger, and only down to the target.
        m = msgs() + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 400_000}}]},
            {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "b" * 400_000}}]}]
        first, n1 = compact.bound_images(m, 2, 700_000, 300_000)
        second, n2 = compact.bound_images(m, 2, 700_000, 300_000)
        self.assertEqual(n1, n2)
        self.assertEqual(first, second)
        self.assertGreater(n1, 0)
        # The history itself is not mutated: append-only invariant holds.
        self.assertEqual(len(m[2]["content"]), 1)

    def test_nonvision_model_gets_a_stable_stub(self):
        m = msgs() + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,zzz"}}]}]
        sent, replaced = compact.unsupported_parts(m, 2, vision=False)
        self.assertEqual(replaced, 1)
        self.assertEqual(sent[2]["content"][0]["type"], "text")
        again, _ = compact.unsupported_parts(m, 2, vision=False)
        self.assertEqual(sent, again)

    def test_vision_model_keeps_images(self):
        m = msgs() + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,zzz"}}]}]
        sent, replaced = compact.unsupported_parts(m, 2, vision=True)
        self.assertEqual(replaced, 0)
        self.assertEqual(sent, m)


class EffortTests(unittest.TestCase):
    def test_effort_is_always_sent(self):
        # Without reasoning_effort the Qwen template thinks in xhigh: 10k+ tokens and an empty answer.
        self.assertEqual(config.effort_body("medium"), {"reasoning_effort": "medium"})
        self.assertEqual(config.effort_body("none")["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(config.effort_body("default"), {})

    def test_summary_effort_is_low(self):
        self.assertEqual(config.summary_effort("high"), "low")
        self.assertEqual(config.summary_effort("none"), "none")


class LossTests(unittest.TestCase):
    def test_tool_output_keeps_its_tail(self):
        out = "\n".join(f"tests/test_{i}.py ...." for i in range(300)) + "\nFAILED test_x - assert -37.5 == 0\n1 failed"
        tr = compact.transcript([{"role": "tool", "tool_call_id": "t", "content": out}])
        self.assertIn("FAILED test_x", tr)

    def test_previous_summary_is_not_cut(self):
        prev = '<compact id="s">\n# Objective\nx\n' + "detail\n" * 400 + "LAST_FACT\n</compact>"
        tr = compact.transcript([{"role": "user", "content": prev}] +
                                [{"role": "user", "content": "y" * 800}] * 40)
        self.assertIn("LAST_FACT", tr)

    def test_user_requests_survive_recompaction(self):
        body = compact.anchor_body("# Objective\nx", [
            {"role": "user", "content": '<user_input mode="act">NEVER touch legacy.py</user_input>'},
            {"role": "user", "content": "<env>\nOS\n</env>"}])
        body = compact.anchor_body("# Objective\ny", [
            {"role": "user", "content": f'<compact id="s">\n{body}\n</compact>'},
            {"role": "user", "content": '<user_input mode="act">also keep VAT</user_input>'}])
        self.assertIn("- NEVER touch legacy.py\n- also keep VAT", body)
        self.assertNotIn("OS", body.split("<user_requests>")[1])
        self.assertEqual(body.count("<user_requests>"), 1)

    def test_inplace_request_is_appended_last(self):
        req = compact.inplace_request("PROMPT")
        self.assertEqual(req["role"], "user")
        self.assertIn("Do not call any tools", req["content"])
        self.assertTrue(compact.inplace_fits(1000, 32768))
        self.assertFalse(compact.inplace_fits(32000, 32768))

    def test_transcript_request_carries_the_transcript(self):
        req = compact.transcript_request("PROMPT", "user: hello")
        self.assertEqual(req["role"], "user")
        self.assertIn("<transcript>\nuser: hello\n</transcript>", req["content"])
        self.assertIn("Do not call any tools", req["content"])

    def test_summary_body_keeps_the_conversation_body_and_pins_the_prefix(self):
        eb = {"promptCacheKey": "k", "reasoning_effort": "high", "strata_prefix": {"messages": 1}}
        body = compact.summary_body(eb, 3)
        self.assertEqual(body["reasoning_effort"], "high")       # rendered into the system turn
        self.assertEqual(body["promptCacheKey"], "k")
        self.assertEqual(body["strata_prefix"], {"messages": 3})
        self.assertEqual(body["reasoning_budget_tokens"], compact.INPLACE_THINK)
        self.assertEqual(eb["strata_prefix"], {"messages": 1})   # the conversation body is not mutated


class SummaryPrefixTests(unittest.IsolatedAsyncioTestCase):
    """The transcript summary must reuse the run's cached prefix, not replace it with its own system."""

    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from maxedcode import headless
        self.h, self.ns = headless, SimpleNamespace
        self.tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        prefix = [{"role": "system", "content": "SYS"}, {"role": "system", "content": "# Memory\nm"},
                  {"role": "user", "content": "<env>x</env>"}]
        for name, value in (("PREFIX", prefix), ("HEAD", len(prefix)), ("current_tool_specs", lambda: self.tools)):
            p = patch.object(headless, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.prefix = prefix

    def client(self, *replies):
        calls, replies = [], list(replies)

        async def create(**kw):
            calls.append(kw)
            content, tool_calls = replies.pop(0)
            return self.ns(choices=[self.ns(message=self.ns(content=content, tool_calls=tool_calls))])
        return self.ns(chat=self.ns(completions=self.ns(create=create))), calls

    async def test_summary_is_asked_after_the_cached_prefix(self):
        good = "# Objective\nx\n# Next Move\ny\n# Relevant Files\na.py"
        client, calls = self.client((good, None))
        out = await self.h.ask_summary(client, "user: hi", "sid")
        self.assertEqual(out, good)
        kw = calls[0]
        self.assertEqual([m["content"] for m in kw["messages"][:3]], [m["content"] for m in self.prefix])
        self.assertIn("<transcript>", kw["messages"][3]["content"])
        self.assertEqual(kw["tools"], self.tools)
        self.assertEqual(kw["extra_body"]["strata_prefix"], {"messages": 3})
        self.assertEqual(kw["extra_body"]["reasoning_effort"], self.h.EFFORT)

    async def test_tool_call_falls_back_to_a_standalone_request(self):
        good = "# Objective\nx\n# Next Move\ny\n# Relevant Files\na.py"
        client, calls = self.client(("", [object()]), (good, None))
        out = await self.h.summarize(client, [{"role": "user", "content": "hi"}], "sid")
        self.assertEqual(out, good)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("tools", calls[1])
        self.assertEqual(calls[1]["messages"][0]["role"], "system")

    def test_step_cut_keeps_only_the_last_step(self):
        m = msgs() + [{"role": "user", "content": "task"},
                      {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
                      {"role": "tool", "tool_call_id": "a", "content": "r1"},
                      {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
                      {"role": "tool", "tool_call_id": "b", "content": "r2"},
                      {"role": "user", "content": "steer"}]
        self.assertEqual(compact.step_cut(m, 3), 5)
        self.assertEqual(compact.step_cut(m, 6), 6)       # never moves backwards
        self.assertEqual(compact.step_cut(m[:4], 3), 3)   # no assistant in the tail: unchanged

    async def test_inplace_summary_keeps_only_the_last_step(self):
        good = "# Objective\nx\n# Next Move\ny\n# Relevant Files\na.py"
        client, _ = self.client((good, None))
        steps = []
        for i in range(12):
            steps += [{"role": "assistant", "content": "", "tool_calls": [
                          {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000}]
        m = self.prefix + [{"role": "user", "content": '<user_input mode="act">do it</user_input>'}] + steps
        self.assertTrue(await self.h.compact_history(client, m, "sid"))
        self.assertEqual([x["role"] for x in m[3:]], ["user", "assistant", "tool"])
        self.assertEqual(m[4]["tool_calls"][0]["id"], "c11")
        self.assertIn("- do it", m[3]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
