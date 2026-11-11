"""Regression tests for prompt-cache stability.

The cached prefix is: system prompt + tool schemas, then the memory message, then the history.
Any byte of that prefix that changes between requests costs a full re-read, so these tests pin
the rules that keep it byte-identical from request to request.
"""
import json
import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TEST_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-cache-tests-")
os.environ["AGENT_HOME"] = _TEST_HOME.name

from maxedcode import compact, config, memory, state, subagent
from maxedcode.cachekey import cache_key, extra_body, subagent_body
from maxedcode.tool_validation import sanitize_tool_history
from maxedcode.tools import tool_request_policy
from maxedcode.ui import app
from maxedcode.util import build_content, squash_loops, wire

_KIND = config.SERVER_KIND


def setUpModule():
    # the default server, as on a fresh install: a server chosen in this copy's config.json (LM Studio gets no
    # Strata cache fields) must not change what these tests check
    config.SERVER_KIND = ""


def tearDownModule():
    config.SERVER_KIND = _KIND


def spec(name, properties, required=()):
    return {"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}


class ArgumentPrefixTests(unittest.TestCase):
    """An argument string the server already saw must be sent back byte-identical."""

    def setUp(self):
        self.schemas = [spec("read_file", {"path": {"type": "string"}, "limit": {"type": "integer"}}, ["path"])]

    def call(self, raw):
        return {"id": "1", "type": "function", "function": {"name": "read_file", "arguments": raw}}

    def test_canonical_arguments_are_left_byte_identical(self):
        raw = '{"path":"D:\\\\work\\\\a.py","limit":10}'
        cleaned = sanitize_tool_history([{"role": "assistant", "content": "reading",
                                          "tool_calls": [self.call(raw)]}], self.schemas)
        self.assertEqual(cleaned[0]["tool_calls"][0]["function"]["arguments"], raw)

    def test_broken_arguments_are_repaired_once_and_then_stay_stable(self):
        raw = '{"path": "D:/work/a.py", "limit": "12",}'
        msgs = [{"role": "assistant", "tool_calls": [self.call(raw)]}]
        first = sanitize_tool_history(msgs, self.schemas)
        args = json.loads(first[0]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args, {"path": "D:\\work\\a.py", "limit": 12})
        # The repaired history is canonical: a second pass must not change it again.
        second = sanitize_tool_history(first, self.schemas)
        self.assertEqual(second, first)

    def test_history_order_is_not_rebuilt(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok"}]
        self.assertEqual(sanitize_tool_history(msgs, self.schemas), msgs)


class ToolCatalogPrefixTests(unittest.TestCase):
    def test_conversational_followup_keeps_the_catalog(self):
        # The tool step is finished (call + result + a plain answer), so the next turn is conversational.
        history = [{"role": "assistant", "tool_calls": [{"id": "1", "type": "function",
                                                         "function": {"name": "grep", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "1", "content": "no match"},
                   {"role": "assistant", "content": "nothing found"}]
        keep_prefix, allow_calls = tool_request_policy("thanks, that is all", history, schemas_enabled=True)
        self.assertTrue(keep_prefix)        # the prefix itself must not change
        self.assertTrue(allow_calls)        # no guessing from words: the model decides in any language

    def test_first_tool_task_keeps_calls_enabled(self):
        keep_prefix, allow_calls = tool_request_policy("run the tests in tests/test_a.py", [], schemas_enabled=True)
        self.assertTrue(keep_prefix and allow_calls)

    def test_pure_question_keeps_the_catalog(self):
        # The catalog is in the prefix from the first request on, so a pure question must not drop
        # it, otherwise the server re-processes the whole conversation.
        keep_prefix, allow_calls = tool_request_policy("what does this mean?", [], schemas_enabled=False)
        self.assertTrue(keep_prefix)
        self.assertTrue(allow_calls)


class PrefixOrderTests(unittest.TestCase):
    def test_memory_is_its_own_message_not_part_of_system(self):
        with patch.object(memory, "text", return_value="MARKER MEMORY"):
            m = memory.message()
        self.assertEqual(m["role"], "system")
        self.assertTrue(m["content"].startswith("# Memory"))
        self.assertIn("MARKER MEMORY", m["content"])
        self.assertNotIn("MARKER MEMORY", config.SYSTEM)

    def test_wire_keeps_system_then_memory_then_history(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "system", "content": "# Memory\nmem"},
                {"role": "user", "content": "hi"}]
        sent = wire(msgs)
        self.assertEqual([m["role"] for m in sent], ["system", "system", "user"])
        self.assertEqual(sent[0], msgs[0])          # cached prefix start is untouched
        self.assertEqual(sent[1], msgs[1])

    def test_cache_key_is_stable_per_cwd(self):
        self.assertEqual(cache_key("D:/project"), cache_key("D:/project"))
        self.assertNotEqual(cache_key("D:/project"), cache_key("D:/other"))
        body = extra_body("D:/project")
        self.assertTrue(body["cache_prompt"])
        self.assertEqual(body["promptCacheKey"], cache_key("D:/project"))
        # The pin at the end of the system turn is what a brand new session reuses.
        self.assertEqual(body["strata_prefix"], {"messages": 1})
        self.assertEqual(subagent_body("D:/project", "parent")["strata_prefix"], {"messages": 1})

    def test_timestamp_sessions_in_same_ten_minute_bucket_do_not_collide(self):
        # Old [:12] key suffix collapsed both IDs to the same YYYYMMDD-HHM prefix.
        a = cache_key("D:/project", "20261022-190601")
        b = cache_key("D:/project", "20261022-190902")
        self.assertNotEqual(a, b)
        # Subagents intentionally share their parent session's affinity group.
        self.assertEqual(cache_key("D:/project", "child-session", "parent-session"),
                         cache_key("D:/project", "parent-session"))

    def test_system_is_identical_for_every_folder_and_every_day(self):
        """A new session must hit the shared root checkpoint, so nothing per-session may be in SYSTEM."""
        self.assertNotIn("Working directory", config.SYSTEM)
        self.assertNotIn("Today", config.SYSTEM)
        with patch.object(os, "getcwd", return_value="D:/other-project"):
            importlib.reload(config)
            other_system, other_env = config.SYSTEM, config.ENV_BLOCK
        importlib.reload(config)
        self.assertEqual(other_system, config.SYSTEM)      # same bytes for every folder
        self.assertNotEqual(other_env, config.ENV_BLOCK)   # only the env block differs

    def test_wire_keeps_env_block_as_the_first_message_after_memory(self):
        msgs = [{"role": "system", "content": config.SYSTEM}, {"role": "system", "content": "# Memory\nmem"},
                {"role": "user", "content": config.ENV_BLOCK}, {"role": "user", "content": "hi"}]
        sent = wire(msgs)
        self.assertEqual([m["role"] for m in sent], ["system", "system", "user", "user"])
        self.assertEqual(sent[0]["content"], config.SYSTEM)   # the shared root checkpoint ends there
        self.assertTrue(sent[2]["content"].startswith("<env>"))


class CompactionAnchorTests(unittest.TestCase):
    def test_recompaction_updates_the_same_anchor(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "system", "content": "# Memory\nmem"},
                {"role": "user", "content": '<compact id="abc-1">\nold summary\n(3 messages compressed)</compact>'},
                {"role": "user", "content": "later work"}]
        anchor = app.compact_anchor(msgs)
        self.assertEqual(anchor, "abc-1")
        rebuilt = [msgs[0], msgs[1], {"role": "user", "content": f'<compact id="{anchor}">\nnew summary</compact>'},
                   msgs[3]]
        self.assertEqual(app.compact_anchor(rebuilt), anchor)
        # One summary message only: nothing after the anchor shifted its position.
        self.assertEqual(sum(1 for m in rebuilt if "<compact id=" in str(m.get("content") or "")), 1)
        self.assertEqual(rebuilt[3], msgs[3])

    def test_anchor_missing_falls_back_to_the_session_id(self):
        self.assertIsNone(app.compact_anchor([{"role": "system", "content": "sys"},
                                              {"role": "user", "content": "plain"}]))


class DrainAndBackgroundTests(unittest.TestCase):
    def test_steuer_items_are_new_messages_not_edits_of_the_prompt(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "prompt"}]
        sent_prompt = dict(msgs[1])
        app.drain_pending(msgs, ["steer text"])
        self.assertEqual(msgs[1], sent_prompt)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[2]["role"], "user")
        self.assertIn("steer text", msgs[2]["content"])

    def test_background_subagent_results_are_delivered_as_messages(self):
        state.SUBAGENT_RESULTS.clear()
        state.SUBAGENT_RESULTS.append('<subagent_result id="1" agent="general">\nfindings\n</subagent_result>')
        state.SUBAGENT_RESULTS.append('<subagent_result id="2" agent="general">\nmore\n</subagent_result>')
        drained = subagent.take_background_results()
        self.assertEqual(len(drained), 2)
        self.assertIn('<subagent_result id="1"', drained[0])
        self.assertEqual(subagent.take_background_results(), [])
        msgs = [{"role": "system", "content": "sys"}]
        app.drain_pending(msgs, drained)
        self.assertEqual(len(msgs), 3)
        self.assertTrue(all(m["role"] == "user" for m in msgs[1:]))
        self.assertIn("<subagent_result", msgs[1]["content"])


class PlanModeAndFillTests(unittest.TestCase):
    """Plan mode must keep the schema prefix, and the fill must not lag behind the real history."""

    def test_plan_mode_keeps_schemas_in_the_prefix(self):
        # A plan is built by reading: tools=[] here would change the prefix on the next request.
        keep_prefix, allow_calls = tool_request_policy("what does this mean?", [], schemas_enabled=False, mode="plan")
        self.assertTrue(keep_prefix and allow_calls)
        # switching mode mid-session must not change the prefix, only tool_choice
        self.assertEqual(tool_request_policy("what does this mean?", [], mode="plan")[0],
                         tool_request_policy("what does this mean?", [], mode="act")[0])

    def test_fill_uses_the_estimate_when_the_server_number_is_stale(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "mem"}]
        head = 2
        for i in range(20):
            msgs.append({"role": "assistant", "content": "step",
                         "tool_calls": [{"id": f"t{i}", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "z" * 6000})
        est = compact.est_tokens(msgs, head, compact.prefix_cost("sys", [], [msgs[1]]))
        # The last measured request was smaller than the history now: the bar must show the estimate.
        self.assertEqual(compact.used_tokens(est, est - 6000, 0), est)


if __name__ == "__main__":
    unittest.main()


class ReasoningHistoryTests(unittest.TestCase):
    """The same assistant message must be rendered identically in every request of a session."""

    def history(self):
        return [{"role": "system", "content": "sys"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1", "reasoning_content": "thinking about q1"},
                {"role": "user", "content": "q2"}]

    def test_reasoning_is_not_sent_back(self):
        sent = wire(self.history())
        self.assertFalse(any("reasoning_content" in m for m in sent))
        self.assertEqual(sent[2]["content"], "a1")

    def test_stored_history_keeps_reasoning_for_the_ui(self):
        msgs = self.history()
        wire(msgs)
        self.assertEqual(msgs[2]["reasoning_content"], "thinking about q1")

    def test_wire_output_is_the_same_before_and_after_a_new_user_turn(self):
        before = wire(self.history()[:3])
        after = wire(self.history())
        self.assertEqual(after[:len(before)], before)

    def test_think_block_inside_content_is_dropped(self):
        sent = wire([{"role": "assistant", "content": "<think>x</think>\n\nanswer"}])
        self.assertEqual(sent[0]["content"], "answer")

    def test_tool_choice_none_is_off_by_default(self):
        self.assertFalse(config.TOOL_CHOICE_NONE)

    def test_every_reasoning_is_sent_back_verbatim(self):
        # Long and looped thinks included: cutting a long one turned an LCB pass (176 s) into a wrong answer
        # (413 s), and a squashed copy differed from the server's cache (a 9.4k-token re-read).
        looped = "Block 3 holds sprites.\n\n" + "Hmm, hmm.\n\nHmm.\n\n" * 200 + "Let me decode it."
        plan = " ".join(f"Edge {i} joins node {i * 7 % 97} and node {i * 13 % 89}." for i in range(120))
        call = [{"id": "c", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "reasoning_content": looped, "tool_calls": call},
                {"role": "assistant", "content": "", "reasoning_content": plan, "tool_calls": call}]
        with patch.object(config, "KEEP_REASONING", True):
            sent = wire(msgs)
            self.assertEqual(wire(msgs), sent)   # the same bytes every request
        self.assertEqual(sent[1]["reasoning_content"], looped)
        self.assertEqual(sent[2]["reasoning_content"], plan)
        self.assertEqual(squash_loops(looped) != looped, True)   # a loop, and still sent as it was


class ReasoningLoopTests(unittest.TestCase):
    def test_detects_a_repeating_tail_in_any_language(self):
        from maxedcode.util import think_loop
        self.assertTrue(think_loop("Let me check. " + "Hmm, hmm. Hmm. " * 20))
        self.assertTrue(think_loop("Хм, хм. " * 30))
        self.assertFalse(think_loop("Hmm, hmm. " * 5 + "Let me check the TAP header once more."))

    def test_data_and_normal_prose_are_not_loops(self):
        from maxedcode.util import squash_loops
        for text in ("00 ff " * 60, "GREN_DY = [1," + "0," * 40 + "-1]", "neighbors: (r-1,c), (r-1,c+1), (r,c-1), (r,c+1)" * 3,
                     "Directions: [[0,1],[1,0],[0,-1],[-1,0],[1,1],[-1,-1],[1,-1],[-1,1]]",
                     "Let me read the file, then check the header, then decode the blocks. " * 5):
            self.assertEqual(squash_loops(text), text)


class CacheDebugTests(unittest.TestCase):
    def test_reports_append_only_and_first_difference(self):
        from maxedcode import cachedebug
        cachedebug._prev.update({"msgs": None, "tools": None, "tmpl": None, "choice": None})
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        tools = [spec("t", {})]
        cachedebug.report(base, tools)
        self.assertIn("append-only OK", cachedebug.report(base + [{"role": "assistant", "content": "a"}], tools))
        line = cachedebug.report([base[0], {"role": "user", "content": "changed"}], tools)
        self.assertIn("first difference at #1", line)
        self.assertIn("tools same", line)
        self.assertIn("tools CHANGED", cachedebug.report([base[0], {"role": "user", "content": "changed"}], []))


class BtwForkTests(unittest.IsolatedAsyncioTestCase):
    """/btw mid-turn answers in the gap after a model step, forked from exactly what that step sent."""

    class _Stream:
        def __init__(self, text):
            self.chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, model_extra={}))])]
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.chunks:
                raise StopAsyncIteration
            return self.chunks.pop(0)

        async def aclose(self):
            self.closed = True

    def agent_with_client(self):
        agent = app.AgentApp()
        calls = []

        async def create(**kw):
            calls.append(kw)
            return self._Stream("side answer")

        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return agent, calls

    async def test_mid_turn_btw_forks_from_the_last_request_and_waits_for_the_step(self):
        from maxedcode.ui.screens import BtwScreen
        agent, calls = self.agent_with_client()
        sent = [{"role": "system", "content": "s"}, {"role": "user", "content": "<user_input>task</user_input>"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "r", "content": "1: x = 1"}]
        agent._last_kw = {"model": "m", "messages": sent, "tools": [spec("read_file", {})], "stream": True,
                          "extra_body": {"promptCacheKey": "k", "reasoning_effort": "low"},
                          "stream_options": {"include_usage": True}, "tool_choice": "none"}
        agent.busy, agent._btw_ready = "Thinking…", False   # the model is streaming its next step
        scr = BtwScreen("what is x?")
        agent._btw_jobs.append(("what is x?", scr))
        agent.btw_kick()
        self.assertIsNone(agent._btw_task)                  # never competes with a running step
        agent._btw_ready = True                             # the step ended, tools are running
        agent.btw_kick()
        await agent.btw_settle()                            # the next step waits for the side answer
        self.assertFalse(agent._btw_ready)
        kw = calls[0]
        self.assertEqual(kw["messages"][:len(sent)], sent)  # the prefix the server holds, byte for byte
        self.assertIn("<btw>", kw["messages"][-1]["content"])
        self.assertEqual(kw["extra_body"]["strata_prefix"], {"messages": len(sent)})
        self.assertEqual(kw["extra_body"]["reasoning_effort"], "low")
        self.assertEqual((kw["tools"], kw["tool_choice"]), (agent._last_kw["tools"], "none"))
        self.assertNotIn("stream_options", kw)
        self.assertNotIn("strata_prefix", agent._last_kw["extra_body"])
        self.assertEqual(scr.text, "side answer")
        self.assertFalse(any("<btw>" in str(m.get("content")) for m in agent.msgs))   # not in the history
