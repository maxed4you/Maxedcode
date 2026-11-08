import unittest

from maxedcode.events import EventEmitter, EventMetrics


class EventMetricTests(unittest.TestCase):
    def test_wall_stream_and_tool_metrics_are_aggregated(self):
        metrics = EventMetrics()
        emitter = EventEmitter(callback=metrics.consume)
        emitter.emit("agent_start")
        emitter.emit("message_start", id="req-1")
        emitter.emit("message_end", id="req-1", ms=23, ok=True)
        emitter.emit("message_start", id="req-2")
        emitter.emit("message_end", id="req-2", ms=17, ok=True)
        emitter.emit("tool_execution_start", id="read-1", tool="read_file", decision="allow")
        emitter.emit("tool_execution_end", id="read-1", tool="read_file", ok=True, ms=10)
        emitter.emit("tool_execution_start", id="grep-1", tool="grep", decision="allow")
        emitter.emit("tool_execution_end", id="grep-1", tool="grep", ok=False, ms=5)
        emitter.emit("agent_end", ok=True)
        data = metrics.result()
        self.assertEqual(data["model_calls"], 2)
        self.assertGreaterEqual(data["stream_ms"], 0)
        self.assertEqual(data["tool_calls"], 2)
        self.assertEqual(data["tool_ms"], 15)
        self.assertEqual(data["tool_ms_by_name"], {"grep": 5, "read_file": 10})
        self.assertGreaterEqual(data["wall_ms"], 0)

    def test_unclosed_tool_start_is_not_counted_as_finished_call(self):
        metrics = EventMetrics()
        emitter = EventEmitter(callback=metrics.consume)
        emitter.emit("agent_start")
        emitter.emit("tool_execution_start", id="x", tool="powershell", decision="ask")
        self.assertEqual(metrics.result()["tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
