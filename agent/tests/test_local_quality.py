"""Regressions for local-model editing, repo maps, and model profiles."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode import config
from maxedcode.repo_map import build_repo_map
from maxedcode import hooks
from maxedcode.tools import fs, runners


class LocalQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(fs.clear_read_cache)
        self.snap = patch.object(fs.undo, "snap", lambda *args, **kwargs: None)
        self.snap.start()
        self.addCleanup(self.snap.stop)

    def test_repo_map_includes_python_signatures_and_respects_tiny_budget(self):
        (self.root / "module.py").write_text("class Sample:\n    def run(self, value):\n        return value\n", encoding="utf-8")
        text = build_repo_map(self.root, 256)
        self.assertIn("class Sample", text)
        self.assertIn("run", text)
        self.assertEqual(build_repo_map(self.root, 8), "")

    def test_write_invalidates_read_cache_and_reports_ast_error(self):
        file = self.root / "sample.py"
        file.write_text("value = 1\n", encoding="utf-8")
        first = fs.tool_read(str(file))
        self.assertIn("value = 1", first)
        result = fs.tool_write(str(file), "def broken(:\n    pass\n")
        self.assertIn("Python validation FAILED", result)
        second = fs.tool_read(str(file))
        self.assertIn("def broken(:", second)
        self.assertNotEqual(first, second)

    def test_write_invalidates_target_and_parent_directory_reads_only(self):
        package = self.root / "pkg"
        package.mkdir()
        target = package / "sample.py"
        unrelated = self.root / "other.py"
        target.write_text("value = 1\n", encoding="utf-8")
        unrelated.write_text("other = 2\n", encoding="utf-8")

        fs.tool_read(str(target))
        fs.tool_read(str(unrelated))
        fs.tool_read(str(package))
        fs.tool_read(str(self.root))

        norm = lambda p: str(p.resolve()).lower() if __import__("os").name == "nt" else str(p.resolve())
        cached_paths = {key[0] for key in fs.READ_CACHE}
        self.assertIn(norm(target), cached_paths)
        self.assertIn(norm(unrelated), cached_paths)
        self.assertIn(norm(package), cached_paths)
        self.assertIn(norm(self.root), cached_paths)

        fs.tool_write(str(target), "value = 3\n")

        cached_paths = {key[0] for key in fs.READ_CACHE}
        self.assertNotIn(norm(target), cached_paths)
        self.assertNotIn(norm(package), cached_paths)
        self.assertNotIn(norm(self.root), cached_paths)
        self.assertIn(norm(unrelated), cached_paths)

    def test_folder_overview_shows_every_top_level_entry_before_deep_files(self):
        # The alphabetically first folder with thousands of files (LM Studio in D:\Ai) must not crowd out its neighbours.
        deep = self.root / "AAA app" / "resources" / "vendor" / "Lib"
        deep.mkdir(parents=True)
        for i in range(300):
            (deep / f"mod{i}.py").write_text("x = 1\n", encoding="utf-8")
        for name in ("Models", "Strata", "packs"):
            (self.root / name).mkdir()
        (self.root / "Strata" / "START-HERE.bat").write_text("@echo off\n", encoding="utf-8")
        (self.root / "run.cmd").write_text("start strata\n", encoding="utf-8")
        text = fs.tool_read(str(self.root))
        for name in ("Models" + os.sep, "Strata" + os.sep, "packs" + os.sep, "run.cmd",
                     os.path.join("Strata", "START-HERE.bat")):
            self.assertIn(name, text)
        self.assertIn("start strata", text)
        self.assertNotIn("mod1.py", text)

    def test_binary_file_is_not_dumped_as_text(self):
        blob = self.root / "tool.bin"
        blob.write_bytes(b"MZ\x00\x01\x02garbage" * 100)
        text = fs.tool_read(str(blob))
        self.assertIn("binary file", text)
        self.assertNotIn("garbage", text)

    @unittest.skipUnless(os.name == "nt", "Windows default protected paths (AppData)")
    def test_protected_path_denial_says_protected_not_deny_rule(self):
        from maxedcode import permissions
        msg = permissions.deny_message("read_file", r"C:\Users\me\AppData\Roaming\app\config.json")
        self.assertIn("protected path (AppData)", msg)
        self.assertNotIn("permissions.json", msg)
        self.assertIsNone(permissions.protected(r"C:\Users\me\AppData\Local\Programs\Strata\BUNDLE.json"))

    def test_conflict_markers_are_rejected_without_writing(self):
        file = self.root / "sample.txt"
        file.write_text("original\n", encoding="utf-8")
        result = fs.tool_write(str(file), "<<<<<<< HEAD\nchanged\n=======\nother\n>>>>>>> branch\n")
        self.assertIn("conflict markers detected", result)
        self.assertEqual(file.read_text(encoding="utf-8"), "original\n")

    def test_edit_fallback_appears_after_two_exact_match_failures(self):
        file = self.root / "sample.txt"
        file.write_text("alpha\nbeta\n", encoding="utf-8")
        first = fs.tool_edit(str(file), "missing", "replacement")
        second = fs.tool_edit(str(file), "missing", "replacement")
        self.assertNotIn("apply_diff", first)
        self.assertIn("apply_diff", second)
        self.assertEqual(file.read_text(encoding="utf-8"), "alpha\nbeta\n")

    def test_background_completion_is_queued_for_headless_turn(self):
        class FinishedProcess:
            def poll(self):
                return 0
        with patch.object(runners, "BG", {"123": (FinishedProcess(), self.root / "task.log")}), \
             patch.object(runners, "BG_NOTIFICATIONS", []), \
             patch.object(runners, "bg_output", return_value="[id 123: finished, exit code 0]\ndone"):
            asyncio.run(runners.watch_background("123"))
            messages = runners.take_bg_notifications()
        self.assertEqual(len(messages), 1)
        self.assertIn('<background_task_completed id="123">', messages[0])
        self.assertIn("done", messages[0])

    def test_apply_diff_applies_unified_hunk(self):
        file = self.root / "sample.txt"
        file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        diff = "--- a/sample.txt\n+++ b/sample.txt\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+delta\n gamma\n"
        result = fs.tool_apply_diff(str(file), diff)
        self.assertIn("applied unified diff", result)
        self.assertEqual(file.read_text(encoding="utf-8"), "alpha\ndelta\ngamma\n")


    def test_apply_diff_accepts_bare_hunk_header_and_locates_by_context(self):
        file = self.root / "sample.txt"
        file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        result = fs.tool_apply_diff(str(file), "--- a/sample.txt\n+++ b/sample.txt\n@@\n beta\n-gamma\n+delta\n")
        self.assertIn("applied unified diff", result)
        self.assertEqual(file.read_text(encoding="utf-8"), "alpha\nbeta\ndelta\n")
        file.write_text("x\nrepeat\ny\nrepeat\n", encoding="utf-8")
        self.assertIn("ambiguous", fs.tool_apply_diff(str(file), "@@ ... @@\n-repeat\n+z\n"))
        self.assertIn("malformed", fs.tool_apply_diff(str(file), "@@ -x +1 @@\n-repeat\n+z\n"))

    def test_apply_diff_prefers_declared_line_when_context_repeats(self):
        file = self.root / "sample.txt"
        file.write_text("start\nrepeat\nmiddle\nrepeat\nend\n", encoding="utf-8")
        diff = "--- a/sample.txt\n+++ b/sample.txt\n@@ -4,1 +4,1 @@\n-repeat\n+changed\n"
        result = fs.tool_apply_diff(str(file), diff)
        self.assertIn("applied unified diff", result)
        self.assertEqual(file.read_text(encoding="utf-8"), "start\nrepeat\nmiddle\nchanged\nend\n")

    def test_apply_diff_accepts_blank_context_line_omitted_prefix(self):
        file = self.root / "sample.txt"
        file.write_text("alpha\n\nbeta\n", encoding="utf-8")
        diff = "--- a/sample.txt\n+++ b/sample.txt\n@@ -1,3 +1,3 @@\n alpha\n\n-beta\n+delta\n"
        result = fs.tool_apply_diff(str(file), diff)
        self.assertIn("applied unified diff", result)
        self.assertEqual(file.read_text(encoding="utf-8"), "alpha\n\ndelta\n")

    def test_auto_tests_are_deferred_and_multiple_edits_run_suite_once(self):
        old_command = hooks.SETTINGS.get("test_command")
        hooks.clear_pending_auto_tests()
        marker = self.root / "test-run-count.txt"
        hooks.SETTINGS["test_command"] = f'echo ran >> "{marker}"'
        self.addCleanup(hooks.clear_pending_auto_tests)
        self.addCleanup(lambda: hooks.SETTINGS.pop("test_command", None) if old_command is None
                        else hooks.SETTINGS.__setitem__("test_command", old_command))
        for tool_name in ("write_file", "edit_file", "apply_diff"):
            result = asyncio.run(hooks.run("PostToolUse", {
                "session_id": "coalesced-test-session", "cwd": str(self.root),
                "event": "PostToolUse", "tool_name": tool_name,
                "tool_input": {"path": str(self.root / "sample.txt")}, "tool_response": "OK, wrote file",
            }))
            self.assertEqual(result["context"], "")
            self.assertFalse(marker.exists(), "full tests must not run after every edit")
        result = asyncio.run(hooks.run_pending_auto_tests("coalesced-test-session", str(self.root)))
        self.assertIn("automatic tests PASS", result)
        # The hook runs through cmd.exe, where `echo ran >> file` keeps the space before the redirect,
        # so compare the lines without surrounding whitespace instead of assuming shell semantics.
        self.assertEqual(marker.read_text(encoding="utf-8").strip().splitlines(), ["ran"])
        self.assertEqual(asyncio.run(hooks.run_pending_auto_tests("coalesced-test-session", str(self.root))), "")

    def test_model_profile_matches_prefix_and_overrides_sampling(self):
        old = config.SETTINGS.get("model_settings")
        config.SETTINGS["model_settings"] = {"test-local": {"temperature": 0.17, "top_k": 7, "max_turns": 9, "edit_format": "udiff"}}
        self.addCleanup(lambda: config.SETTINGS.pop("model_settings", None) if old is None
                        else config.SETTINGS.__setitem__("model_settings", old))
        self.assertEqual(config.sampling_for("test-local-quantized")["temperature"], 0.17)
        self.assertEqual(config.top_k_for("test-local-quantized"), 7)
        self.assertEqual(config.max_turns_for("test-local-quantized"), 9)
        self.assertEqual(config.edit_format_for("test-local-quantized"), "udiff")


if __name__ == "__main__":
    unittest.main()


class ServerResolveTests(unittest.TestCase):
    """Without the server's model id no model_settings profile matched ("local"); min_p was hard-coded."""

    def test_profile_min_p_and_server_model_id(self):
        profiles = {"model_settings": {"qwen": {"min_p": 0.0, "temperature": 1.0}}}
        with patch.dict(config.SETTINGS, profiles), patch.object(config, "MODEL", "qwen3.8-flash-next"):
            self.assertEqual(config.min_p_for(), 0.0)
            self.assertEqual(config.sampling_for()["temperature"], 1.0)
        with patch.dict(config.SETTINGS, profiles), patch.object(config, "MODEL", "local"):
            self.assertEqual(config.min_p_for(), config.MIN_P)

    def test_resolve_server_fills_only_what_the_user_did_not_set(self):
        answers = {"/health": {"max_context": 98304}, "/v1/models": {"data": [{"id": "qwen-x"}]},
                   "/props": {"chat_template_caps": {"supports_preserve_reasoning": True}}}

        def fake(url, key="", body=None, timeout=0):
            for k, v in answers.items():
                if url.endswith(k):
                    return v
            raise OSError("404")

        # port_down: the fake server must count as listening whether or not a real one runs on 8080
        common = (patch.object(config.servers, "http", fake), patch.object(config.servers, "port_down", lambda *a, **k: False),
                  patch.object(config, "BASE_URL", "http://127.0.0.1:8080/v1"),
                  patch.object(config, "SERVER_KIND", ""), patch.object(config, "CTX_WINDOW", 32768),
                  patch.object(config, "KEEP_REASONING", False))
        for p in common:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in common])
        with patch.object(config, "CTX_SRC", "default"), patch.object(config, "MODEL", "local"),\
             patch.object(config, "MODEL_SET", False), patch.object(config, "_KEEP_RAW", "auto"):
            got = config.resolve_server()
            self.assertEqual((got["ctx"], got["model"], got["keep_reasoning"], got["kind"]),
                             (98304, "qwen-x", True, "strata"))
        with patch.object(config, "CTX_SRC", "settings.json"), patch.object(config, "CTX_WINDOW", 32768),\
             patch.object(config, "MODEL", "mine"), patch.object(config, "MODEL_SET", True),\
             patch.object(config, "_KEEP_RAW", "0"), patch.object(config, "SERVER_KIND", ""),\
             patch.object(config, "KEEP_REASONING", False):
            got = config.resolve_server()
            self.assertEqual((got["ctx"], got["model"], got["keep_reasoning"]), (32768, "mine", False))

    def test_server_kinds_context_and_cache_fields(self):
        """LM Studio / Ollama / llama.cpp report their window and loaded model their own way; each gets only
        the cache fields it understands."""
        from maxedcode import servers
        lms = {"/api/v0/models": {"data": [{"id": "a", "state": "not-loaded", "type": "llm"},
                                           {"id": "b", "state": "loaded", "type": "llm", "loaded_context_length": 24576}]}}
        oll = {"/api/version": {"version": "0.12"}, "/api/ps": {"models": [{"model": "qwen3:8b", "context_length": 8192}]}}
        cpp = {"/health": {"status": "ok"}, "/props": {"default_generation_settings": {"n_ctx": 16384}}}
        for answers, kind, ctx, model in ((lms, "lmstudio", 24576, "b"), (oll, "ollama", 8192, "qwen3:8b"),
                                          (cpp, "llamacpp", 16384, None)):
            def fake(url, key="", body=None, timeout=0, answers=answers):
                for k, v in answers.items():
                    if url.endswith(k):
                        return v
                raise OSError("404")
            # The fake answers stand for a listening server: the quick closed-port check must not skip them.
            with self.subTest(kind=kind), patch.object(servers, "http", fake),                     patch.object(servers, "port_down", lambda *a, **k: False):
                d = servers.describe("http://127.0.0.1:9/v1")
                self.assertEqual((d["kind"], d["ctx"], d["model"]), (kind, ctx, model))
        self.assertEqual(servers.cache_fields("strata"), {"cache_prompt", "promptCacheKey", "strata_prefix"})
        self.assertEqual(servers.cache_fields("llamacpp"), {"cache_prompt"})
        self.assertEqual(servers.cache_fields("ollama"), set())
        self.assertEqual(servers.cache_fields("openai"), {"prompt_cache_key"})
        self.assertEqual(servers.kind_of("https://api.openai.com/v1"), "openai")


class SystemPromptPolicyTests(unittest.TestCase):
    def test_full_prompt_is_preserved_and_checklist_is_not_forced_on_small_tasks(self):
        prompt_path = Path(__file__).resolve().parents[1] / "system_prompt.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertGreater(len(prompt.split()), 1800)
        for section in ("# How much work", "# Tool calls — each one costs time", "# Code rules", "# Truth", "# Safety", "# Plan / Act modes"):
            with self.subTest(section=section):
                self.assertIn(section, prompt)
        self.assertIn("3+ distinct kinds of work", prompt)
        self.assertIn("Do not call it for explanations, conversational Q&A, read-only lookups", prompt)
        self.assertNotIn("2+ distinct actions, call `todo_write` as the FIRST tool call", prompt)


class BundledSkillsTests(unittest.TestCase):
    def test_bundled_skills_are_listed_but_bodies_stay_out_of_the_listing(self):
        from maxedcode import skills
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(skills, "HOME", Path(tmp)), patch.object(skills.Path, "cwd", return_value=Path(tmp)):
            names = {s["name"] for s in skills.skills()}
            text = skills.listing()
        self.assertTrue({"code-review", "simplify", "security-review", "debug", "commit", "write-tests", "office-files"} <= names)
        self.assertIn("- debug: ", text)
        self.assertNotIn("Reproduce", text)

    def test_project_skill_overrides_bundled_one_with_the_same_name(self):
        from maxedcode import skills
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / ".maxedcode" / "skills" / "debug"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: debug\ndescription: own debug\n---\nown body $ARGUMENTS\n",
                                        encoding="utf-8")
            with patch.object(skills, "HOME", Path(tmp) / "home"), \
                 patch.object(skills.Path, "cwd", return_value=Path(tmp)):
                found = [s for s in skills.skills() if s["name"] == "debug"]
                x, body = skills.skill("debug", "boom")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["description"], "own debug")
        self.assertEqual(body, "own body boom")
