"""Regression tests for speed-related runtime fixes."""
import ast
import asyncio
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from maxedcode import config
from maxedcode.tools import BUILTIN, last_assistant_has_tool_calls, parallel_safe, prompt_needs_repo_map, prompt_needs_tools, tool_request_policy
from maxedcode.loopguard import LoopGuard
from maxedcode.tools.todo import TaskProgressGuard
from maxedcode import permissions
from maxedcode.shelltool import NAME as SHELL  # noqa: E402  (powershell on Windows, bash on Linux/macOS)


def load_headless_with_optional_dependency_stubs():
    """Import headless in minimal/offline test environments without making network calls."""
    try:
        return importlib.import_module("maxedcode.headless")
    except ImportError as exc:
        if "openai" not in str(exc).lower() and "ddgs" not in str(exc).lower():
            raise
    openai_stub = types.ModuleType("openai")
    class AsyncOpenAI:
        def __init__(self, *args, **kwargs):
            pass
    class BadRequestError(Exception):
        pass
    openai_stub.AsyncOpenAI = AsyncOpenAI
    openai_stub.BadRequestError = BadRequestError
    ddgs_stub = types.ModuleType("ddgs")
    class DDGS:
        def text(self, *args, **kwargs):
            return []
    ddgs_stub.DDGS = DDGS
    sys.modules["openai"] = openai_stub
    sys.modules["ddgs"] = ddgs_stub
    sys.modules.pop("maxedcode.headless", None)
    return importlib.import_module("maxedcode.headless")


headless = load_headless_with_optional_dependency_stubs()


class AgentAppStartupSourceTests(unittest.TestCase):
    def test_tool_policy_flags_are_initialized_before_first_status_refresh(self):
        # The UI dependencies are optional in this test container, so check the
        # constructor's state contract via AST; test_fixes.py also has a real
        # Textual run_test smoke test for environments with installed dependencies.
        app_path = Path(__file__).parents[1] / "maxedcode" / "ui" / "app.py"
        module = ast.parse(app_path.read_text(encoding="utf-8"))
        app_class = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        init = next(node for node in app_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__")
        initialized = {}
        for node in ast.walk(init):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        initialized[target.attr] = node.value.value
        # The tool catalog is part of the cached prefix, so it must be in the very first request;
        # only tool_choice may be off for a conversational prompt.
        self.assertIs(initialized.get("_use_tool_schemas"), True)
        self.assertIs(initialized.get("_allow_tool_calls"), False)
        self.assertIs(initialized.get("_tool_schemas_enabled"), True)


class ThinkBudgetTests(unittest.TestCase):
    def test_default_reasoning_caps_match_the_documented_fast_defaults(self):
        if "AGENT_THINK_CAP" not in os.environ:
            self.assertEqual(config.THINK_CAP, 4096)
        if "AGENT_THINK_CAP_LOW" not in os.environ:
            self.assertEqual(config.THINK_CAP_LOW, 1024)
        if "AGENT_THINK_CAP_HIGH" not in os.environ:
            # 16384: live LCB 3733 (2026-11-01) was cut at 8192 mid-code -> 24 steps, 337 s; 16384 -> 9 steps, 238 s
            self.assertEqual(config.THINK_CAP_HIGH, 16384)
        if "AGENT_THINK_CAP_STEP" not in os.environ:
            self.assertEqual(config.THINK_CAP_STEP, 2048)

    def test_default_reasoning_budget_is_bounded_by_effort(self):
        with patch.object(config, "THINK_BUDGET", 0), \
             patch.object(config, "THINK_CAP", 4096), \
             patch.object(config, "THINK_CAP_LOW", 1024), \
             patch.object(config, "THINK_CAP_HIGH", 8192):
            self.assertEqual(config.think_budget(0, "low"), 1024)
            self.assertEqual(config.think_budget(0, "medium"), 4096)
            self.assertEqual(config.think_budget(0, "high"), 8192)
            self.assertEqual(config.think_budget(0, "none"), 0)
            self.assertLessEqual(config.think_budget(CTX_WINDOW := config.CTX_WINDOW - 2048, "high"), 8192)

    def test_small_remaining_window_does_not_send_budget(self):
        with patch.object(config, "THINK_BUDGET", 0):
            self.assertEqual(config.think_budget(config.CTX_WINDOW - 100, "medium"), 0)

    def test_explicit_budget_override_is_respected(self):
        with patch.object(config, "THINK_BUDGET", 1536):
            self.assertEqual(config.think_budget(0, "high"), 1536)


class ToolRoutingTests(unittest.TestCase):
    def test_schema_registry_is_reexported_from_package(self):
        from maxedcode.tools.schemas import BUILTIN as SCHEMA_BUILTIN
        self.assertIs(BUILTIN, SCHEMA_BUILTIN)
        names = [item["function"]["name"] for item in BUILTIN]
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))
        for item in BUILTIN:
            function = item["function"]
            self.assertTrue(function["name"])
            self.assertIsInstance(function["description"], str)
            self.assertEqual(function["parameters"]["type"], "object")

    def test_every_request_may_use_tools_in_any_language(self):
        # The intent is not guessed from words: the same answer for every language and every phrasing.
        for prompt in (
            "сколько будет 17 * 23 + 4?", "ответь одним словом: столица Польши", "Python?",
            "почини баг в buggy.py", "проверь проблему в коде", "go ahead", "what is 2+2?",
            "виправ помилку в коді", "behebe den Fehler im Parser", "arregla el error del parser",
            "修复解析器的错误", "パーサーのバグを直して", "popraw błąd w parserze", "أصلح الخطأ",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(prompt_needs_tools(prompt))
        self.assertFalse(prompt_needs_tools(""))
        self.assertFalse(prompt_needs_tools('<user_input mode="act"></user_input>'))

    def test_only_a_latest_unfinished_assistant_tool_call_keeps_schemas(self):
        completed = [
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
            {"role": "assistant", "content": "Done."},
        ]
        unfinished = completed[:-1]
        self.assertFalse(last_assistant_has_tool_calls(completed))
        self.assertTrue(last_assistant_has_tool_calls(unfinished))

    def test_policy_keeps_cached_schema_prefix(self):
        history = [
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "read app.py"},
            {"role": "assistant", "content": "I found the issue."},
        ]
        # the catalog is always in the prefix; the model itself decides whether a reply needs a tool
        for prompt in ("why is that?", "continue", "чому так?", "为什么？"):
            with self.subTest(prompt=prompt):
                self.assertEqual(tool_request_policy(prompt, history, schemas_enabled=True), (True, True))
        self.assertEqual(tool_request_policy("what is 2+2?", [], schemas_enabled=False), (True, True))

    def test_repo_map_skipped_only_when_the_request_names_a_file(self):
        for prompt in ("почини баг в app.py", "прочитай CLAUDE.md и поправь инструкцию", "покажи что в .env",
                       "修复 app.py 里的错误", "lies C:/work/notes und fasse zusammen"):
            with self.subTest(prompt=prompt):
                self.assertFalse(prompt_needs_repo_map(prompt))
        for prompt in ("проверь проблему в коде", "виправ помилку в коді", "修复解析器的错误", "find the bug in the parser"):
            with self.subTest(prompt=prompt):
                self.assertTrue(prompt_needs_repo_map(prompt))
        self.assertFalse(prompt_needs_repo_map(""))

    def test_headless_tool_catalog_drops_only_tools_switched_off(self):
        old_allowed = headless.ALLOWED
        try:
            headless.ALLOWED = set()
            with patch.object(headless, "MCPS", None), \
                 patch.object(headless.skills, "skills", return_value=[]), \
                 patch.object(headless.runners, "BG", {}), \
                 patch.object(headless, "SETTINGS", {"mode": "act"}), \
                 patch.dict(os.environ, {"AGENT_ENABLE_SUBAGENT": "0", "AGENT_ENABLE_REMEMBER": "0"}):
                names = {item["function"]["name"] for item in headless.current_tool_specs()}
            self.assertFalse({"subagent", "remember"} & names)
            # Mode and skills are enforced at execution: their schemas stay, so the prefix does not move.
            self.assertLessEqual({"skill", "exit_plan_mode"}, names)
        finally:
            headless.ALLOWED = old_allowed

    def test_headless_and_tui_send_the_same_tools(self):
        # The tools are rendered into the system turn: one list keeps the prefix cache shared between
        # the CLI and the desktop window (a different one re-read the whole prompt on every switch).
        from maxedcode.tools.catalog import session_catalog
        old_allowed = headless.ALLOWED
        try:
            headless.ALLOWED = set()
            env = {k: v for k, v in os.environ.items() if k not in ("AGENT_ENABLE_SUBAGENT", "AGENT_ENABLE_REMEMBER")}
            with patch.object(headless, "MCPS", None), patch.dict(os.environ, env, clear=True), \
                 patch.object(headless, "SETTINGS", {"mode": "act"}):
                self.assertEqual(headless.current_tool_specs(), session_catalog(None, allowed=None, include_search=True))
        finally:
            headless.ALLOWED = old_allowed

    def test_headless_catalog_does_not_change_when_a_background_job_starts(self):
        old_allowed = headless.ALLOWED
        try:
            headless.ALLOWED = set()
            with patch.object(headless, "MCPS", None), patch.object(headless, "SETTINGS", {"mode": "act"}):
                with patch.object(headless.runners, "BG", {}):
                    before = headless.current_tool_specs()
                with patch.object(headless.runners, "BG", {"1": (None, None)}):
                    after = headless.current_tool_specs()
            self.assertEqual(before, after)
            self.assertIn("bg_output", {item["function"]["name"] for item in before})
        finally:
            headless.ALLOWED = old_allowed

    def test_parallel_safe_is_imported_by_headless(self):
        self.assertIs(headless.parallel_safe, parallel_safe)




class OptionalChecklistTests(unittest.TestCase):
    def test_missing_checklist_does_not_force_an_extra_tool_turn(self):
        guard = TaskProgressGuard()
        for _ in range(5):
            self.assertIsNone(guard.blocked_reason("read_file", {"path": "a.py"}, []))
            guard.observe_tool("read_file", {"path": "a.py"}, True)
        self.assertIsNone(guard.blocked_reason("edit_file", {"path": "a.py"}, []))


class LoopGuardSearchTests(unittest.TestCase):
    def test_one_narrow_retry_then_blocks_empty_searches(self):
        guard = LoopGuard()
        first = guard.after("grep", {"pattern": "calculate_tax"}, "No matches.")
        self.assertIn("one narrow retry", first)
        second = guard.after("grep", {"pattern": "tax"}, "No matches.")
        self.assertIn("second search with no matches", second)
        blocked = guard.before("grep", {"pattern": "calculate"})
        self.assertIn("Blocked", blocked)
        self.assertIn("Stop searching", blocked)

    def test_deleting_git_head_ref_requires_confirmation(self):
        why = permissions.dangerous(SHELL, "git update-ref -d HEAD")
        self.assertEqual(why, "deletes a Git reference")

class AnyLanguageTests(unittest.TestCase):
    """Token estimates, checklists and write guards work the same for every writing system."""

    def test_token_estimate_counts_cjk_as_dense(self):
        from maxedcode.util import tok
        self.assertEqual(tok("abcd" * 10), 10)
        self.assertEqual(tok("абвг" * 10), 20)
        self.assertEqual(tok("你好世界" * 10), 40)

    def test_checklist_language_by_writing_system(self):
        from maxedcode.tools.todo import validate
        self.assertIsNone(validate([{"content": "运行 pytest", "status": "pending"},
                                    {"content": "修复错误", "status": "pending"}]))
        self.assertIn("another language", validate([{"content": "运行测试", "status": "pending"},
                                                    {"content": "Fix the bug", "status": "pending"}]))

    def test_placeholder_comment_in_any_language(self):
        from maxedcode.tools.fs import LAZY_RE
        for line in ("    // … resto sin cambios", "# ... 其余代码不变", "<!-- ... reszta bez zmian -->"):
            self.assertTrue(LAZY_RE.search(line), line)
        for line in ("x = [1, 2, ...]", "    ...", "print('...done')"):
            self.assertFalse(LAZY_RE.search(line), line)


class HeadlessStartupTests(unittest.TestCase):
    def test_no_configured_mcp_servers_means_no_startup(self):
        old_argv = sys.argv[:]
        try:
            sys.argv = ["local_agent.py", "--print", "--output-format", "text"]
            answer = types.SimpleNamespace(content="395", tool_calls=None)
            response = types.SimpleNamespace(
                usage=None,
                choices=[types.SimpleNamespace(message=answer, finish_reason="stop")],
            )
            api = types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=AsyncMock(return_value=response))))
            hooks_result = {"decision": None, "input": None, "context": "", "message": "", "stop": False}
            with patch.object(headless, "AsyncOpenAI", return_value=api), \
                 patch.object(headless, "MCP") as mcp_cls, \
                 patch.object(headless.hooks, "run", new=AsyncMock(return_value=hooks_result)), \
                 patch.object(headless.memory, "text", return_value=""), \
                 patch.object(headless.memory, "startup_files", return_value=[]), \
                 patch.object(headless.skills, "listing", return_value=""), \
                 patch.object(headless, "save_session"), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                mcp_cls.cfg.return_value = {}
                asyncio.run(headless.run("сколько будет 17 * 23 + 4?", sid="simple-qa"))
            self.assertIn("395", stdout.getvalue())
            mcp_cls.assert_not_called()
            api.chat.completions.create.assert_awaited_once()
        finally:
            sys.argv = old_argv


class FileToolEncodingAndGlobTests(unittest.TestCase):
    def test_absolute_glob_pattern_finds_files_outside_cwd(self):
        from maxedcode.tools.fs import tool_glob
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "local_agent.py").write_text("x = 1\n", encoding="utf-8")
            pattern = Path(tmp).as_posix() + "/*.py"           # cwd stays elsewhere, path is the default "."
            self.assertIn("local_agent.py", tool_glob(pattern))
            self.assertIn("local_agent.py", tool_glob(str(Path(tmp, "local_agent.py"))))

    def test_glob_for_one_named_file_returns_its_content(self):
        # Live runs: glob("slow.py") and only then read_file — one model turn wasted per named file.
        from maxedcode.tools.fs import tool_glob
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "slow.py").write_text("x = 42\n", encoding="utf-8")
            Path(tmp, "other.py").write_text("y = 1\n", encoding="utf-8")
            exact = tool_glob("slow.py", tmp)
            self.assertIn("x = 42", exact)
            self.assertIn("no need to read it again", exact)
            masked = tool_glob("*.py", tmp)
            self.assertNotIn("x = 42", masked)

    def test_utf16_powershell_log_is_read_and_grepped_as_text(self):
        from maxedcode.tools.fs import clear_read_cache, tool_grep, tool_read
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "_runlog.txt")
            log.write_text("[RUN] syntax\nAgent not found: C:\\x\\local_agent.py\n", encoding="utf-16")
            clear_read_cache(str(log))
            self.assertIn("2: Agent not found", tool_read(str(log)))
            self.assertIn("Agent not found", tool_grep("agent not found", str(log)))


if __name__ == "__main__":
    unittest.main()
