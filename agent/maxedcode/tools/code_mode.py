"""Confined, AST-interpreted Python orchestration over the existing tool registry.

Adapted from OpenCode v2 CodeMode's architecture: a small interpreted subset,
explicitly exposed tools, call/output/time budgets, per-call status, and a single
aggregate result. We intentionally do not compile or exec model-provided code.
"""
from __future__ import annotations

import ast
import asyncio
import json
import operator
import inspect
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .catalog import builtin_catalog
from ..config import CODE_MODE_TIMEOUT, TOOL_OUTPUT_LIMIT, output_limit
from ..util import normalize_tool_output

MAX_CODE_CHARS = 20_000
MAX_CALLS = 40
MAX_RANGE = 10_000
MAX_AST_NODES = 3_000
MAX_STEPS = 20_000
MAX_STRING_CHARS = 100_000
MAX_COLLECTION_ITEMS = 10_000
FETCH_TIMEOUT = 8
FETCH_MAX_BYTES = 32_000


class _Return(Exception):
    def __init__(self, value: Any):
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


@dataclass(frozen=True)
class _ToolFunction:
    name: str
    dispatch: Any
    counter: list[int]
    calls: list[dict]

    async def __call__(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            kwargs = args[0]
        elif args:
            raise TypeError("tools.<name> accepts one argument object or keyword arguments")
        self.counter[0] += 1
        if self.counter[0] > MAX_CALLS:
            raise RuntimeError(f"code_mode tool-call limit exceeded ({MAX_CALLS})")
        record = {"tool": self.name, "status": "running", "argument_keys": sorted(str(k) for k in kwargs)}
        self.calls.append(record)
        try:
            result = await self.dispatch(self.name, kwargs)
            if isinstance(result, dict) and result.get("error"):
                record["status"] = "error"
            else:
                record["status"] = "completed"
            return normalize_tool_output(result, output_limit(self.name))
        except Exception as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            return {"error": f"{type(exc).__name__}: {exc}", "tool": self.name}


@dataclass(frozen=True)
class _ToolRoot:
    allowed: frozenset[str]
    dispatch: Any
    counter: list[int]
    calls: list[dict]


@dataclass(frozen=True)
class _AsyncioAPI:
    name: str


class _SafeRange:
    def __call__(self, *args):
        value = range(*args)
        if len(value) > MAX_RANGE:
            raise ValueError(f"range exceeds code_mode limit ({MAX_RANGE})")
        return value


def _safe_fetch_sync(url: str, method: str = "GET", headers: dict | None = None, body: str | None = None):
    """Bounded HTTP-only helper, exposed as a host function (not as a Python module)."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("fetch only supports absolute http:// and https:// URLs")
    method = str(method or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValueError("unsupported HTTP method")
    data = None if body is None else str(body).encode("utf-8")
    request = urllib.request.Request(str(url), data=data, headers={str(k): str(v) for k, v in (headers or {}).items()}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read(FETCH_MAX_BYTES + 1)
            truncated = len(raw) > FETCH_MAX_BYTES
            raw = raw[:FETCH_MAX_BYTES]
            text = raw.decode(response.headers.get_content_charset() or "utf-8", "replace")
            if truncated:
                text += f"\n[fetch output truncated at {FETCH_MAX_BYTES} bytes]"
            return {"status": response.status, "url": response.geturl(), "content_type": response.headers.get("Content-Type", ""), "text": text}
    except urllib.error.HTTPError as exc:
        raw = exc.read(FETCH_MAX_BYTES).decode("utf-8", "replace")
        return {"status": exc.code, "url": str(url), "error": str(exc), "text": raw}


async def _safe_fetch(url: str, method: str = "GET", headers: dict | None = None, body: str | None = None):
    """Async HTTP facade; blocking urllib work is kept off the event loop."""
    return await asyncio.to_thread(_safe_fetch_sync, url, method, headers, body)


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 20:
        return "[maximum nesting depth exceeded]"
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_CHARS else value[:MAX_STRING_CHARS] + "…[string truncated]"
    if isinstance(value, dict):
        items = list(value.items())[:MAX_COLLECTION_ITEMS]
        return {str(k): _safe_value(v, depth + 1) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, depth + 1) for v in list(value)[:MAX_COLLECTION_ITEMS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, range):
        return list(value[:MAX_COLLECTION_ITEMS])
    return str(value)


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}


class _Interpreter:
    """An allowlisted AST evaluator; no Python bytecode compilation or unrestricted exec."""
    def __init__(self, tree: ast.AsyncFunctionDef, tools: _ToolRoot, timeout: float):
        self.body = tree.body
        self.tools = tools
        self.timeout = timeout
        self.steps = 0
        self.output: list[str] = []
        self.env: dict[str, Any] = {}
        self.last_expression: Any = None
        self.has_last_expression = False
        self.builtins = {
            "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
            "enumerate": enumerate, "filter": filter, "float": float, "int": int,
            "isinstance": isinstance, "len": len, "list": list, "map": map,
            "max": max, "min": min, "print": self._print, "range": _SafeRange(),
            "repr": repr, "reversed": reversed, "round": round, "set": set,
            "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
            "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
            "RuntimeError": RuntimeError,
        }

    def _tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise RuntimeError(f"code_mode instruction budget exceeded ({MAX_STEPS})")

    def _print(self, *values, sep=" ", end="\n"):
        self.output.append(sep.join(str(value) for value in values) + end)
        joined = "".join(self.output)
        if len(joined) > MAX_STRING_CHARS:
            self.output = [joined[:MAX_STRING_CHARS] + "…[stdout truncated]"]

    async def run(self):
        for statement in self.body:
            await self.stmt(statement)
        # OpenCode CodeMode treats the final top-level expression as the result when
        # no explicit return was used. Preserve that affordance for small CLI scripts.
        return self.last_expression if self.has_last_expression else None

    async def stmt(self, node):
        self._tick()
        if isinstance(node, ast.Expr):
            value = await self.expr(node.value)
            if inspect.isawaitable(value):
                value.close()
                raise RuntimeError("tool calls must be awaited; use `await tools.name(...)` or `await asyncio.gather(...)`")
            self.last_expression = _safe_value(value)
            self.has_last_expression = True
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = await self.expr(node.value)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name) or target.id.startswith("_"):
                    raise ValueError("only simple local variable assignments are supported")
                self.env[target.id] = _safe_value(value)
            return
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name) or node.target.id not in self.env:
                raise ValueError("augmented assignment requires an existing local variable")
            op = _BINOPS.get(type(node.op))
            if not op:
                raise ValueError("unsupported augmented assignment operator")
            self.env[node.target.id] = _safe_value(op(self.env[node.target.id], await self.expr(node.value)))
            return
        if isinstance(node, ast.If):
            branch = node.body if await self.expr(node.test) else node.orelse
            for child in branch:
                await self.stmt(child)
            return
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name) or node.target.id.startswith("_"):
                raise ValueError("for loops may assign only to simple local variables")
            iterable = await self.expr(node.iter)
            if not isinstance(iterable, (list, tuple, str, range, set)):
                raise ValueError("for loops require a bounded list, tuple, set, string, or range")
            if len(iterable) > MAX_RANGE:
                raise ValueError(f"for loop exceeds {MAX_RANGE} items")
            for item in iterable:
                self._tick()
                self.env[node.target.id] = item
                try:
                    for child in node.body:
                        await self.stmt(child)
                except _Continue:
                    continue
                except _Break:
                    break
            for child in node.orelse:
                await self.stmt(child)
            return
        if isinstance(node, ast.Return):
            raise _Return(await self.expr(node.value) if node.value else None)
        if isinstance(node, ast.Break):
            raise _Break()
        if isinstance(node, ast.Continue):
            raise _Continue()
        if isinstance(node, ast.Pass):
            return
        if isinstance(node, ast.Assert):
            if not await self.expr(node.test):
                raise AssertionError(str(await self.expr(node.msg)) if node.msg else "assertion failed")
            return
        raise ValueError(f"unsupported statement: {type(node).__name__}")

    async def expr(self, node):
        self._tick()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            if node.id in self.builtins:
                return self.builtins[node.id]
            if node.id == "tools":
                return self.tools
            if node.id == "fetch":
                return _safe_fetch
            if node.id == "asyncio":
                return "__safe_asyncio__"
            raise NameError(f"unknown name: {node.id}")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if len(node.elts) > MAX_COLLECTION_ITEMS:
                raise ValueError("literal collection is too large")
            values = [await self.expr(item) for item in node.elts]
            return values if isinstance(node, ast.List) else tuple(values) if isinstance(node, ast.Tuple) else set(values)
        if isinstance(node, ast.Dict):
            if len(node.keys) > MAX_COLLECTION_ITEMS:
                raise ValueError("literal dictionary is too large")
            if any(key is None for key in node.keys):
                raise ValueError("dictionary unpacking is unavailable in the confined interpreter")
            keys = [await self.expr(key) for key in node.keys]
            values = [await self.expr(value) for value in node.values]
            return dict(zip(keys, values))
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError("private attributes are unavailable")
            if isinstance(node.value, ast.Name) and node.value.id == "tools":
                if node.attr not in self.tools.allowed:
                    raise ValueError(f"unknown or unavailable tool: {node.attr}")
                return _ToolFunction(node.attr, self.tools.dispatch, self.tools.counter, self.tools.calls)
            if isinstance(node.value, ast.Name) and node.value.id == "asyncio" and node.attr == "gather":
                return _AsyncioAPI(node.attr)
            raise ValueError("attribute access is only allowed for listed tools and asyncio.gather/as_completed")
        if isinstance(node, ast.Await):
            result = await self.expr(node.value)
            if not inspect.isawaitable(result):
                raise TypeError("await requires a tool call, asyncio gather, or fetch request")
            return await result
        if isinstance(node, ast.Call):
            function = await self.expr(node.func)
            args = [await self.expr(arg) for arg in node.args]
            kwargs = {kw.arg: await self.expr(kw.value) for kw in node.keywords if kw.arg is not None}
            if len(kwargs) != len(node.keywords):
                raise ValueError("**kwargs expansion is not supported")
            if isinstance(function, _AsyncioAPI):
                # OpenCode uses Promise.allSettled for independent calls; gather(return_exceptions=True)
                # gives the Python adaptation the same property: failures don't erase successes.
                return asyncio.gather(*args, return_exceptions=True)
            if not callable(function) or function not in self.builtins.values() and function is not _safe_fetch:
                if not isinstance(function, _ToolFunction):
                    raise ValueError("only safe builtins, fetch(), and tools.<name>(...) are callable")
            if isinstance(function, _ToolFunction):
                return function(*args, **kwargs)
            return function(*args, **kwargs)
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if not op:
                raise ValueError(f"unsupported binary operator: {type(node.op).__name__}")
            left, right = await self.expr(node.left), await self.expr(node.right)
            if isinstance(node.op, ast.Mult):
                for sequence, multiplier in ((left, right), (right, left)):
                    if isinstance(sequence, (str, list, tuple)) and isinstance(multiplier, int) and not isinstance(multiplier, bool) and multiplier > MAX_COLLECTION_ITEMS:
                        raise ValueError("sequence multiplication exceeds code_mode budget")
            return _safe_value(op(left, right))
        if isinstance(node, ast.UnaryOp):
            op = _UNARY.get(type(node.op))
            if not op:
                raise ValueError("unsupported unary operator")
            return op(await self.expr(node.operand))
        if isinstance(node, ast.BoolOp):
            values = node.values
            last = None
            for item in values:
                last = await self.expr(item)
                if isinstance(node.op, ast.And) and not last:
                    return last
                if isinstance(node.op, ast.Or) and last:
                    return last
            return last
        if isinstance(node, ast.Compare):
            left = await self.expr(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                right = await self.expr(comparator)
                op = _CMPS.get(type(op_node))
                if not op or not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return await self.expr(node.body if await self.expr(node.test) else node.orelse)
        if isinstance(node, ast.Subscript):
            value = await self.expr(node.value)
            index = await self.expr(node.slice)
            return _safe_value(value[index])
        if isinstance(node, ast.Slice):
            lower = await self.expr(node.lower) if node.lower else None
            upper = await self.expr(node.upper) if node.upper else None
            step = await self.expr(node.step) if node.step else None
            return slice(lower, upper, step)
        if isinstance(node, ast.JoinedStr):
            pieces = []
            for value in node.values:
                pieces.append(str(await self.expr(value.value)) if isinstance(value, ast.FormattedValue) else str(value.value))
            return "".join(pieces)[:MAX_STRING_CHARS]
        if isinstance(node, ast.FormattedValue):
            return str(await self.expr(node.value))
        raise ValueError(f"unsupported expression: {type(node).__name__}")


def _validate_ast(tree: ast.AsyncFunctionDef):
    """Reject Python's powerful/dynamic syntax and require tool calls to be awaited."""
    forbidden = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.Lambda,
                 ast.While, ast.AsyncFor, ast.AsyncWith, ast.With, ast.Try, ast.FunctionDef,
                 ast.AsyncFunctionDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
                 ast.Yield, ast.YieldFrom, ast.Delete, ast.NamedExpr)
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise ValueError(f"script is too complex ({len(nodes)} AST nodes; limit {MAX_AST_NODES})")
    parents: dict[ast.AST, ast.AST] = {}
    for parent in nodes:
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    # The outer async function is a synthetic parsing wrapper. It is interpreted,
    # never executed; user-authored nested functions remain forbidden.
    # All forbidden constructs at once: one by one, a small model rewrites the script
    # once per construct (import, then while, then a comprehension...).
    found = list(dict.fromkeys(type(n).__name__ for n in nodes if n is not tree and isinstance(n, forbidden)))
    if found:
        raise ValueError(f"{', '.join(found)} unavailable in the confined interpreter. code_mode only "
                         "orchestrates tool calls; to run Python code, a test or a subprocess, use py_run "
                         "(or powershell: python <file>.py)")
    for node in nodes:
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private/dunder attributes are unavailable")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in {"open", "eval", "exec", "compile", "globals", "locals", "vars", "getattr", "setattr", "delattr", "input", "breakpoint", "__import__", "help", "dir", "type", "object", "memoryview", "classmethod", "staticmethod", "property"}):
            raise ValueError(f"name {node.id!r} is unavailable in the confined interpreter")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise ValueError("exponentiation is unavailable")
        is_tool_call = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "tools")
        is_fetch_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fetch"
        if is_tool_call or is_fetch_call:
            parent = parents.get(node)
            while parent is not None and not isinstance(parent, (ast.stmt, ast.Await)):
                if isinstance(parent, ast.Await):
                    break
                parent = parents.get(parent)
            if not isinstance(parent, ast.Await):
                raise ValueError("every tools.<name>(...) call must be awaited, usually with `await asyncio.gather(...)`")


async def run_code(code, dispatch, *, timeout=None, allowed_tools=None):
    """Run one confined orchestration program and return a single compact aggregate result."""
    source = str(code or "")
    if not source.strip():
        return "Error: code_mode requires non-empty Python code."
    if len(source) > MAX_CODE_CHARS:
        return f"Error: code_mode script exceeds {MAX_CODE_CHARS} characters."
    # In addition to builtins, callers pass the actual current registry names so connected
    # MCP tools can be exposed without inventing names. The outer orchestration tool and
    # recursive/meta controls remain unavailable from inside CodeMode.
    allowed = ({spec["function"]["name"] for spec in builtin_catalog()}
               if allowed_tools is None else {str(name) for name in allowed_tools})
    allowed.difference_update({"code_mode", "finish", "exit_plan_mode", "tool_search"})
    wrapped = "async def __maxedcode_script__():\n" + "\n".join("    " + line for line in source.splitlines()) + "\n"
    try:
        module = ast.parse(wrapped, mode="exec")
        script = module.body[0]
        if not isinstance(script, ast.AsyncFunctionDef):
            raise ValueError("invalid script wrapper")
        _validate_ast(script)
    except (SyntaxError, ValueError) as exc:
        return f"Error: code_mode rejected script: {exc}"

    call_states: list[dict] = []
    interpreter = _Interpreter(script, _ToolRoot(frozenset(allowed), dispatch, [0], call_states), float(timeout or CODE_MODE_TIMEOUT))
    started = time.monotonic()
    try:
        try:
            result = await asyncio.wait_for(interpreter.run(), timeout=interpreter.timeout)
        except _Return as returned:
            result = returned.value
    except asyncio.TimeoutError:
        result = {"error": "TimeoutExceeded", "message": f"execution exceeded {interpreter.timeout:g}s"}
    except Exception as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    elapsed = time.monotonic() - started
    result = _safe_value(result)
    try:
        result_text = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "null"
    except (TypeError, ValueError):
        result_text = repr(result)
    parts = [f"code_mode: {elapsed:.2f}s; tool calls: {interpreter.tools.counter[0]} / {MAX_CALLS}"]
    if interpreter.output:
        parts.append("stdout:\n" + "".join(interpreter.output).strip())
    if call_states:
        parts.append("tool call status:\n" + "\n".join(
            f"- {item['tool']}: {item['status']} (args: {', '.join(item['argument_keys']) or 'none'})"
            + (f" — {item['error']}" if item.get("error") else "")
            for item in call_states
        ))
    if result is not None:
        parts.append("result:\n" + result_text)
    return normalize_tool_output("\n\n".join(part for part in parts if part), TOOL_OUTPUT_LIMIT)
