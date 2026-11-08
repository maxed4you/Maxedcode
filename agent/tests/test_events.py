import unittest

from maxedcode.events import AgentContext, EventEmitter, EventMetrics, EVENT_NAMES


class EventTests(unittest.TestCase):
    def test_fixed_event_names_and_tool_decision_validation(self):
        self.assertEqual(len(EVENT_NAMES), 8)
        emitter = EventEmitter()
        with self.assertRaises(ValueError):
            emitter.emit("other_event")
        with self.assertRaises(ValueError):
            emitter.emit("tool_execution_start", tool="read_file", id="x")

    def test_one_start_end_pair_per_tool_and_end_has_ms_ok(self):
        emitter = EventEmitter()
        start = emitter.emit("tool_execution_start", id="tool-1", tool="read_file", decision="allow")
        end = emitter.emit("tool_execution_end", id="tool-1", tool="read_file", ok=True)
        self.assertEqual(start["seq"] + 1, end["seq"])
        self.assertIn("ms", end)
        self.assertTrue(end["ok"])
        self.assertEqual([e["event"] for e in emitter.events].count("tool_execution_start"), 1)
        self.assertEqual([e["event"] for e in emitter.events].count("tool_execution_end"), 1)

    def test_parallel_ends_are_in_completion_order(self):
        emitter = EventEmitter()
        emitter.emit("tool_execution_start", id="a", tool="slow", decision="allow")
        emitter.emit("tool_execution_start", id="b", tool="fast", decision="allow")
        emitter.emit("tool_execution_end", id="b", tool="fast", ok=True, ms=1)
        emitter.emit("tool_execution_end", id="a", tool="slow", ok=True, ms=20)
        ends = [e["tool"] for e in emitter.events if e["event"] == "tool_execution_end"]
        self.assertEqual(ends, ["fast", "slow"])

    def test_agent_context_has_abort_signal_and_parallel_mode(self):
        context = AgentContext(messages=[], tools=[], tool_execution_mode="parallel")
        self.assertTrue(context.abort_event.is_set() is False)
        self.assertEqual(context.tool_execution_mode, "parallel")

    def test_callback_receives_serializable_events(self):
        received = []
        emitter = EventEmitter(received.append)
        event = emitter.emit("agent_start", cwd=".")
        self.assertEqual(received, [event])
        self.assertEqual(event["event"], "agent_start")
        self.assertGreater(event["seq"], 0)


if __name__ == "__main__":
    unittest.main()
