"""Typed, ordered event stream shared by TUI and headless agent loops.

Events are deliberately additive: callers may keep the legacy assistant/tool_result/result JSONL
aliases while consumers migrate to the normalized event names defined here.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

EventName = Literal[
    "agent_start", "turn_start", "message_start", "message_end",
    "tool_execution_start", "tool_execution_end", "turn_end", "agent_end",
]
ToolExecutionMode = Literal["sequential", "parallel"]
EVENT_NAMES = frozenset({
    "agent_start", "turn_start", "message_start", "message_end",
    "tool_execution_start", "tool_execution_end", "turn_end", "agent_end",
})


@dataclass
class AgentContext:
    """Per-loop state: the loop can use this context instead of reaching into UI globals."""
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_execution_mode: ToolExecutionMode = "sequential"
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    name: str
    timestamp: float
    sequence: int
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"event": self.name, "ts": self.timestamp, "seq": self.sequence, **self.data}


class EventEmitter:
    """Emit a normalized event stream and match tool starts/ends by call id."""

    def __init__(self, callback: Callable[[dict[str, Any]], Any] | None = None):
        self.callback = callback
        self.events: list[dict[str, Any]] = []
        self._seq = 0
        self._tool_starts: dict[str, float] = {}

    def emit(self, name: str, **data) -> dict[str, Any]:
        if name not in EVENT_NAMES:
            raise ValueError(f"unknown event type: {name}")
        now = time.monotonic()
        if name == "tool_execution_start":
            decision = data.get("decision")
            if decision not in {"allow", "deny", "ask"}:
                raise ValueError("tool_execution_start requires decision=allow|deny|ask")
            call_id = str(data.get("id") or data.get("call_id") or f"tool-{self._seq + 1}")
            data.setdefault("id", call_id)
            self._tool_starts[call_id] = now
        elif name == "tool_execution_end":
            call_id = str(data.get("id") or data.get("call_id") or "")
            start = self._tool_starts.pop(call_id, None)
            if "ms" not in data and start is not None:
                data["ms"] = max(0, int((now - start) * 1000))
            data.setdefault("ok", False)
        self._seq += 1
        event = AgentEvent(name, now, self._seq, dict(data)).as_dict()
        self.events.append(event)
        if self.callback is not None:
            self.callback(event)
        return event

    def tool_start(self, tool: str, args: dict[str, Any], *, decision: str, call_id: str):
        return self.emit("tool_execution_start", tool=tool, input=args, decision=decision, id=call_id)

    def tool_end(self, tool: str, *, call_id: str, ok: bool, ms: int | None = None, **extra):
        data = {"tool": tool, "id": call_id, "ok": bool(ok), **extra}
        if ms is not None:
            data["ms"] = max(0, int(ms))
        return self.emit("tool_execution_end", **data)


class EventMetrics:
    """Derive wall/stream/tool metrics from the event stream (parallel tools may overlap)."""

    def __init__(self):
        self.agent_started: float | None = None
        self.agent_ended: float | None = None
        self.model_calls = 0
        self.stream_ms = 0
        self.tool_calls = 0
        self.tool_ms = 0
        self.tool_ms_by_name: dict[str, int] = {}
        self._message_started: dict[str, float] = {}
        self._tool_started: dict[str, tuple[float, str]] = {}

    def consume(self, event: dict[str, Any]):
        name = event.get("event")
        ts = float(event.get("ts", time.monotonic()))
        data = event
        if name == "agent_start":
            self.agent_started = ts
        elif name == "agent_end":
            self.agent_ended = ts
        elif name == "message_start":
            mid = str(data.get("id") or data.get("call_id") or f"message-{self.model_calls + 1}")
            self._message_started[mid] = ts
            self.model_calls += 1
        elif name == "message_end":
            mid = str(data.get("id") or data.get("call_id") or "")
            begin = self._message_started.pop(mid, None)
            elapsed = max(0, int((ts - begin) * 1000)) if begin is not None else max(0, int(data.get("ms", 0)))
            self.stream_ms += elapsed
        elif name == "tool_execution_start":
            cid = str(data.get("id") or data.get("call_id") or f"tool-{self.tool_calls + 1}")
            self._tool_started[cid] = (ts, str(data.get("tool") or "unknown"))
        elif name == "tool_execution_end":
            cid = str(data.get("id") or data.get("call_id") or "")
            started = self._tool_started.pop(cid, None)
            tool = str(data.get("tool") or (started[1] if started else "unknown"))
            elapsed = max(0, int(data.get("ms", ((ts - started[0]) * 1000) if started else 0)))
            self.tool_calls += 1
            self.tool_ms += elapsed
            self.tool_ms_by_name[tool] = self.tool_ms_by_name.get(tool, 0) + elapsed

    def result(self) -> dict[str, Any]:
        end = self.agent_ended or time.monotonic()
        wall = max(0, int((end - self.agent_started) * 1000)) if self.agent_started is not None else 0
        return {"wall_ms": wall, "stream_ms": self.stream_ms, "model_calls": self.model_calls,
                "tool_calls": self.tool_calls, "tool_ms": self.tool_ms,
                "tool_ms_by_name": dict(sorted(self.tool_ms_by_name.items()))}
