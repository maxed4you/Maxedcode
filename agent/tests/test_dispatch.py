"""Subagent dispatch keeps the parent's cache prefix while bounding its own working history."""
import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-dispatch-tests-")
os.environ["AGENT_HOME"] = _HOME.name

from maxedcode import config, subagent
from maxedcode.cachekey import extra_body, subagent_body
from maxedcode.util import wire
from maxedcode.tools.schemas import BUILTIN


def tool_specs():
    return [{"type": "function", "function": {"name": "read_file", "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"], "additionalProperties": False}}}]


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        task = str(kwargs["messages"][-1].get("content") or "")
        message = SimpleNamespace(content=f"src/example.py:17 — fact found for {task[-40:]}", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_prefix_tools_model_and_cache_key_are_reused(self):
        sid = "20261022-203000"
        parent_mem = [{"role": "system", "content": "# Memory\nshared memory"},
                      {"role": "user", "content": "<env>same environment</env>"}]
        expected_prefix = wire([{"role": "system", "content": config.SYSTEM}, *parent_mem])
        tools = tool_specs()
        client_completions = FakeCompletions()
        with patch.object(subagent, "AsyncOpenAI", return_value=FakeClient(client_completions)):
            result = await subagent.run("inspect one function", prefix=parent_mem, parent_sid=sid,
                                        tools=tools, model="parent-model")

        self.assertIn("src/example.py:17", result)
        call = client_completions.calls[0]
        self.assertEqual(call["model"], "parent-model")
        self.assertEqual(call["messages"][:len(expected_prefix)], expected_prefix)
        self.assertEqual(call["tools"], tools)
        self.assertIn("<delegation>", call["messages"][len(expected_prefix)]["content"])
        self.assertEqual(call["extra_body"]["promptCacheKey"], extra_body(os.getcwd(), sid)["promptCacheKey"])
        self.assertEqual(call["extra_body"]["promptCacheKey"], subagent_body(os.getcwd(), sid)["promptCacheKey"])
        # The fork point is pinned: the child's checkpoints must not evict the one the parent resumes from.
        self.assertEqual(call["extra_body"]["strata_prefix"], {"messages": len(expected_prefix)})

    async def test_child_forks_from_the_parent_history(self):
        tools = tool_specs()
        base = [{"role": "system", "content": "# Memory\nm"}, {"role": "user", "content": "<env>e</env>"}]
        history = [{"role": "system", "content": config.SYSTEM}, *base,
                   {"role": "user", "content": "<user_input>task</user_input>"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "D:/a.py"}'}}]},
                   {"role": "tool", "tool_call_id": "r", "content": "1: x = 1"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "s", "type": "function",
                    "function": {"name": "subagent", "arguments": '{"prompt": "find y"}'}}]}]
        forked = subagent.fork_prefix(history, base, tools)
        # Exactly what the parent sent (the same sanitizing as its request), without the pending call.
        self.assertEqual(forked, subagent.sanitize_tool_history(history[:-1], tools))
        huge = history[:-1] + [{"role": "tool", "tool_call_id": "r", "content": "z " * (config.CTX_WINDOW * 4)}] + history[-1:]
        self.assertIs(subagent.fork_prefix(huge, base, tools), base)   # no room for the child: fresh prefix
        client_completions = FakeCompletions()
        with patch.object(subagent, "AsyncOpenAI", return_value=FakeClient(client_completions)):
            await subagent.run("find y", prefix=forked, parent_sid="20261022-203001", tools=tools, model="m")
        call = client_completions.calls[0]
        self.assertEqual(call["messages"][:len(forked)], wire(forked))
        self.assertIn("parent agent's conversation", call["messages"][len(forked)]["content"])
        self.assertEqual(call["extra_body"]["strata_prefix"], {"messages": len(forked)})

    async def test_report_has_a_hard_character_limit(self):
        result = subagent._report("x" * 5000, 200)
        self.assertLessEqual(len(result), 200)
        self.assertTrue(result.endswith("[report truncated]"))

    async def test_trimming_never_mutates_the_shared_prefix(self):
        prefix = [{"role": "system", "content": config.SYSTEM},
                  {"role": "system", "content": "# Memory\nshared"}]
        before = [dict(m) for m in prefix]
        msgs = [*prefix, {"role": "user", "content": "delegation task"}]
        for i in range(10):
            msgs.append({"role": "assistant", "content": "read this file", "tool_calls": [
                {"id": f"call-{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"call-{i}", "content": "large result " + ("z" * 5000)})
        trimmed = subagent._trim_history(msgs, len(prefix), tool_specs(), prefix, 1000)
        self.assertGreater(trimmed, 0)
        self.assertEqual(msgs[:len(prefix)], before)
        self.assertTrue(any("subagent history trimmed" in str(m.get("content")) for m in msgs[len(prefix):]))

    async def test_fan_out_returns_one_report_per_parent_turn_and_shares_one_cache_slot(self):
        tasks = ["Inspect function A", "Inspect function B", "Inspect function C"]
        sid = "20261022-203100"
        mem = [{"role": "user", "content": "<env>same</env>"}]
        tools = tool_specs()
        completions = FakeCompletions()
        with patch.object(subagent, "SUBAGENT_MAX_CONCURRENT", 1), \
             patch.object(subagent, "AsyncOpenAI", return_value=FakeClient(completions)):
            result = await subagent.fan_out(tasks, parent_sid=sid, prefix=mem, tools=tools, model="parent-model")
            self.assertIn('task="1/3"', result)
            self.assertEqual(len(completions.calls), 1)  # Parent gets report before the next child starts.
            batch_id = re.search(r'batch_id="(batch-[a-f0-9]+)"', result).group(1)
            result2 = await subagent.continue_batch(batch_id, parent_sid=sid, prefix=mem, tools=tools, model="parent-model")
            self.assertIn('task="2/3"', result2)
            self.assertEqual(len(completions.calls), 2)
            result3 = await subagent.continue_batch(batch_id, parent_sid=sid, prefix=mem, tools=tools, model="parent-model")
        self.assertIn('task="3/3"', result3)
        self.assertIn("subagent_batch_complete", result3)
        self.assertEqual(len(completions.calls), len(tasks))
        keys = {c["extra_body"]["promptCacheKey"] for c in completions.calls}
        self.assertEqual(keys, {subagent_body(os.getcwd(), sid)["promptCacheKey"]})
        self.assertTrue(all(c["tools"] == tools for c in completions.calls))
        children = [row for row in subagent.state.CHILD_SESSIONS.values() if row.get("parent_sid") == sid]
        self.assertEqual(len(children), len(tasks))
        self.assertTrue(all(row.get("status") == "completed" for row in children))
        for child in children:
            payload = json.loads(Path(child["path"]).read_text(encoding="utf-8"))
            self.assertTrue(payload["is_subagent"])
            self.assertEqual(payload["parent_sid"], sid)
            self.assertEqual(payload["agent_status"], "completed")
            self.assertTrue(payload["msgs"])

    async def test_single_child_is_persisted_and_uses_parent_cache_prefix(self):
        sid = "parent-for-single-child"
        prefix = [{"role": "system", "content": "# Memory\nparent memory"},
                  {"role": "user", "content": "<env>same</env>"}]
        completions = FakeCompletions()
        with patch.object(subagent, "AsyncOpenAI", return_value=FakeClient(completions)):
            report = await subagent.run_child("Inspect the parser and report one fact", parent_sid=sid,
                                              prefix=prefix, tools=tool_specs(), model="parent-model")
        self.assertIn("src/example.py:17", report)
        call = completions.calls[0]
        self.assertEqual(call["extra_body"]["promptCacheKey"], subagent_body(os.getcwd(), sid)["promptCacheKey"])
        child = next(row for row in subagent.state.CHILD_SESSIONS.values() if row.get("parent_sid") == sid)
        payload = json.loads(Path(child["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["agent_status"], "completed")
        self.assertIn("<delegation>", payload["msgs"][0]["content"])
        self.assertFalse(any(m.get("role") == "system" and m.get("content") == config.SYSTEM for m in payload["msgs"]))

    async def test_implementation_child_can_use_writer_but_readonly_child_cannot(self):
        from unittest.mock import AsyncMock, Mock
        writer = next(t for t in BUILTIN if t["function"]["name"] == "write_file")
        readonly = await subagent.exec("write_file", {"path": "must-not-be-written.txt", "content": "x"},
                                       allow_write=False, tool_specs=BUILTIN)
        self.assertIn("writing is not allowed", readonly)
        hook_result = {"decision": "allow", "message": "", "input": None}
        with patch.object(subagent.hooks, "run", AsyncMock(return_value=hook_result)), \
             patch.object(subagent.permissions, "check", return_value=None), \
             patch.object(subagent, "tool_write", Mock(return_value="written")) as write_mock:
            result = await subagent.exec("write_file", {"path": "/tmp/maxedcode-dispatch-test.txt", "content": "x"},
                                         allow_write=True, tool_specs=BUILTIN)
        self.assertEqual(result, "written")
        write_mock.assert_called_once()
        self.assertTrue(writer["function"]["parameters"]["properties"])

    async def test_subagent_schema_supports_either_prompt_or_two_to_six_tasks(self):
        tool = next(t for t in BUILTIN if t["function"]["name"] == "subagent")
        schema = tool["function"]["parameters"]
        self.assertEqual(schema["properties"]["tasks"]["minItems"], 2)
        self.assertEqual(schema["properties"]["tasks"]["maxItems"], 6)
        self.assertNotIn("prompt", schema.get("required", []))
        self.assertIn("report_chars", schema["properties"])
        self.assertIn("batch_id", schema["properties"])
        self.assertIn("first child", tool["function"]["description"])

    async def test_fan_out_runs_one_child_then_waits_for_parent_to_continue(self):
        active, maximum = 0, 0
        events = []

        async def fake_run(task, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            events.append(("start", task))
            await asyncio.sleep(0.005)
            events.append(("end", task))
            active -= 1
            return f"done: {task}"

        prefix = [{"role": "user", "content": "env"}]
        tools = tool_specs()
        with patch.object(subagent, "SUBAGENT_MAX_CONCURRENT", 1), patch.object(subagent, "run", fake_run):
            first = await subagent.fan_out(["task A", "task B", "task C"], parent_sid="sid",
                                           prefix=prefix, tools=tools, model="parent-model")
            self.assertIn('task="1/3"', first)
            self.assertEqual(events, [("start", "task A"), ("end", "task A")])
            batch_id = re.search(r'batch_id="(batch-[a-f0-9]+)"', first).group(1)
            second = await subagent.continue_batch(batch_id, parent_sid="sid", prefix=prefix, tools=tools)
            self.assertIn('task="2/3"', second)
            self.assertEqual(events[-2:], [("start", "task B"), ("end", "task B")])
            third = await subagent.continue_batch(batch_id, parent_sid="sid", prefix=prefix, tools=tools)
        self.assertEqual(maximum, 1)
        self.assertEqual(events, [("start", "task A"), ("end", "task A"),
                                  ("start", "task B"), ("end", "task B"),
                                  ("start", "task C"), ("end", "task C")])
        self.assertIn("done: task A", first)
        self.assertIn("done: task B", second)
        self.assertIn("done: task C", third)
        self.assertIn("subagent_batch_complete", third)
        children = [row for row in subagent.state.CHILD_SESSIONS.values() if row.get("parent_sid") == "sid"]
        self.assertGreaterEqual(len(children), 3)

    async def test_fan_out_rejects_wrong_task_count_without_starting_runs(self):
        mocked_run = AsyncMock(return_value="fact")
        with patch.object(subagent, "SUBAGENT_MAX_CONCURRENT", 2), patch.object(subagent, "run", mocked_run):
            result = await subagent.fan_out(["only one task"], parent_sid="sid")
        self.assertIn("2–6", result)
        mocked_run.assert_not_awaited()

    async def test_background_inference_stays_disabled_even_when_capacity_flag_is_high(self):
        with patch.object(subagent, "SUBAGENT_MAX_CONCURRENT", 3):
            result = subagent.start_background("do work", parent_session="sid", prefix=[], tools=tool_specs())
        self.assertIn("disabled", result)
        self.assertEqual(subagent.background_count(), 0)

    async def test_inference_semaphore_hard_limits_to_one_even_if_capacity_flag_is_high(self):
        active, maximum = 0, 0

        class TrackingCompletions:
            async def create(self, **kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="path.py:1 — done", tool_calls=[]))])

        client = FakeClient(TrackingCompletions())
        kwargs = {"prefix": [{"role": "user", "content": "env"}], "parent_sid": "same-parent",
                  "tools": tool_specs(), "model": "parent-model"}
        with patch.object(subagent, "SUBAGENT_MAX_CONCURRENT", 3), \
             patch.object(subagent, "AsyncOpenAI", return_value=client):
            await asyncio.gather(subagent.run("task A", **kwargs), subagent.run("task B", **kwargs))
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
