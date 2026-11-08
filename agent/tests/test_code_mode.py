"""Runtime checks for the confined CodeMode interpreter adapted from OpenCode."""
import asyncio
import unittest

from maxedcode.tools.code_mode import run_code


class CodeModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_tool_call_and_explicit_return(self):
        calls = []

        async def dispatch(name, args):
            calls.append((name, args))
            return f"{name}:{args.get('path', '')}"

        result = await run_code('return await tools.read_file(path="src/app.py")', dispatch, timeout=1)
        self.assertEqual(calls, [("read_file", {"path": "src/app.py"})])
        self.assertIn('"read_file:src/app.py"', result)
        self.assertIn("tool calls: 1 / 40", result)
        self.assertIn("tool call status:", result)
        self.assertIn("read_file: completed", result)

    async def test_gather_runs_independent_tools_concurrently(self):
        calls = []

        async def dispatch(name, args):
            calls.append(name)
            await asyncio.sleep(0.06)
            return f"{name}:ok"

        result = await run_code(
            'results = await asyncio.gather(\n'
            '    tools.read_file(path="one.py"),\n'
            '    tools.grep(pattern="needle"),\n'
            ')\n'
            'return results',
            dispatch,
            timeout=1,
        )
        self.assertCountEqual(calls, ["read_file", "grep"])
        self.assertIn("read_file:ok", result)
        self.assertIn("grep:ok", result)
        self.assertIn("tool calls: 2 / 40", result)

    async def test_connected_registry_name_can_be_exposed_explicitly(self):
        async def dispatch(name, args):
            return f"{name}:{args.get('value')}"

        result = await run_code(
            'return await tools.mcp__local__echo(value="ok")',
            dispatch,
            allowed_tools=["mcp__local__echo"],
            timeout=1,
        )
        self.assertIn("mcp__local__echo:ok", result)

    async def test_imports_filesystem_access_and_unawaited_tools_are_rejected(self):
        async def dispatch(name, args):
            self.fail("dispatch must not be called for a rejected program")

        for source in (
            "import os",
            "return open('secret.txt').read()",
            "return tools.read_file(path='x.py')",
            "return tools.read_file(path='x.py') + 1",
        ):
            with self.subTest(source=source):
                result = await run_code(source, dispatch, timeout=1)
                self.assertTrue(result.startswith("Error: code_mode rejected script:"), result)

    async def test_rejection_lists_every_forbidden_construct_and_points_to_py_run(self):
        source = "import subprocess\nwhile True:\n    break\nxs = [i for i in range(3)]\ndef f():\n    pass"
        result = await run_code(source, lambda *_: None, timeout=1)
        for name in ("Import", "While", "ListComp", "FunctionDef", "py_run"):
            self.assertIn(name, result)

    async def test_fetch_requires_await_and_refuses_non_http_schemes(self):
        result = await run_code('return await fetch("file:///etc/passwd")', lambda *_: None, timeout=1)
        self.assertIn("only supports absolute http:// and https:// URLs", result)
        result = await run_code('fetch("https://example.com")', lambda *_: None, timeout=1)
        self.assertTrue(result.startswith("Error: code_mode rejected script:"), result)

    async def test_timeout_is_reported_without_hanging_the_caller(self):
        async def dispatch(name, args):
            await asyncio.sleep(0.2)
            return "finished"

        result = await run_code('return await tools.read_file(path="slow.py")', dispatch, timeout=0.01)
        self.assertIn("TimeoutExceeded", result)

    async def test_final_expression_becomes_result(self):
        result = await run_code('1 + 2 * 3', lambda *_: None, timeout=1)
        self.assertIn("result:\n7", result)


if __name__ == "__main__":
    unittest.main()
