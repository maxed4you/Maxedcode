"""Stable tool registry and per-tool validation metadata.

The runtime still owns dispatch in UI/headless/subagent, but schemas, validation metadata,
parallel-safety and tool-kind are exposed through one small immutable registry. This keeps
prompt bytes deterministic and makes validation callable before permissions are evaluated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict[str, Any]
    kind: str = "run"
    parallel: bool = False
    max_output_chars: int | None = None
    runner: Callable[[dict[str, Any]], Any] | None = field(default=None, compare=False, repr=False)

    def validate(self, args: dict[str, Any]) -> list[dict[str, str]]:
        """Return structured field problems without formatting or executing anything."""
        from .tool_validation import semantic_argument_problems
        problems: list[dict[str, str]] = []
        if not isinstance(args, dict):
            return [{"field": "arguments", "why": "expected a JSON object", "fix": "send an object with named fields"}]
        params = self.schema.get("function", {}).get("parameters", {}) or {}
        for field_name in params.get("required", []) or []:
            value = args.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip() and field_name != "content"):
                problems.append({"field": str(field_name), "why": "required field is missing or empty",
                                 "fix": f"provide {field_name} using the tool schema"})
        problems.extend(semantic_argument_problems(self.name, args))
        return problems

    def build(self, params: dict[str, Any]) -> tuple[dict[str, Any], bool, list[dict[str, str]]]:
        """Canonicalize a call; return (args, changed, validation-problems)."""
        from .tool_validation import repair_tool_arguments
        source = dict(params) if isinstance(params, dict) else {}
        fixed = repair_tool_arguments(self.name, source, [self.schema])
        changed = fixed != source or any(type(fixed.get(k)) is not type(source.get(k)) for k in set(source) | set(fixed))
        return fixed, changed, self.validate(fixed)

    async def run(self, args: dict[str, Any]):
        """Invoke an explicitly supplied handler; dispatch is not guessed by the registry."""
        if self.runner is None:
            raise RuntimeError(f"Tool {self.name} has no runtime handler attached")
        return await self.runner(args)


class ToolRegistry:
    def __init__(self, specs=None, *, parallel_names=(), read_names=(), writer_names=(), output_limits=None, runners=None):
        self._tools: dict[str, Tool] = {}
        parallel_names = set(parallel_names)
        read_names = set(read_names) | parallel_names
        writer_names = set(writer_names)
        output_limits = output_limits or {}
        runners = runners or {}
        for spec in sorted((deepcopy(s) for s in (specs or [])),
                           key=lambda s: str(s.get("function", {}).get("name", ""))):
            fn = spec.get("function", {}) or {}
            name = str(fn.get("name", ""))
            if not name:
                continue
            kind = "write" if name in writer_names else ("read" if name in read_names else "run")
            self._tools[name] = Tool(name, spec, kind, name in parallel_names,
                                     output_limits.get(name), runners.get(name))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        for name in sorted(self._tools):
            yield self._tools[name]

    def schemas(self) -> list[dict[str, Any]]:
        return [deepcopy(tool.schema) for tool in self]

    def stable_bytes(self) -> bytes:
        return json.dumps(self.schemas(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def validate(self, name: str, args: dict[str, Any]) -> list[dict[str, str]]:
        tool = self.get(name)
        if tool is None:
            return [{"field": "tool", "why": "unknown tool name", "fix": "use an advertised tool name"}]
        return tool.validate(args)


def builtin_registry():
    """Create a registry for the builtins without changing their source schemas."""
    from .tools.schemas import BUILTIN, PARALLEL_READS, READONLY, EDIT_TOOLS
    from .permissions import WRITE
    return ToolRegistry(BUILTIN, parallel_names=PARALLEL_READS, read_names=READONLY,
                        writer_names=set(EDIT_TOOLS) | set(WRITE))
