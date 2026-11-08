"""Privacy and payload contract for cloud-only code drafting."""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from maxedcode import cloud


class CloudPrivacyTests(unittest.TestCase):
    def setUp(self):
        # A run from tests AI sets MAXEDCODE_CLOUD=off; config tests check the file itself, without this switch.
        env = patch.dict(os.environ, {"MAXEDCODE_CLOUD": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_rejects_secrets_local_paths_and_pasted_source(self):
        blocked = [
            r"Write code using C:\Users\alex\project\main.py",
            "api_key=AIza123456789012345678901234567890",
            "Implement this:\n```python\nprint('private')\n```",
            "<file_attachment path='secret.txt'>private contents</file_attachment>",
            "password=hunter2",
        ]
        for task in blocked:
            with self.subTest(task=task), self.assertRaises(ValueError):
                cloud.validate_task(task)

    def test_accepts_short_standalone_specification(self):
        task = "Write a pure Python function that groups a list of integers by parity. Include type hints and two unit tests."
        self.assertEqual(cloud.validate_task(task), task)

    def test_requires_https_for_non_local_provider(self):
        with self.assertRaises(ValueError):
            cloud._valid_base_url("http://example.com/v1")
        with self.assertRaises(ValueError):
            cloud._valid_base_url("https://example.com/v1?api_key=secret")
        self.assertEqual(cloud._valid_base_url("https://example.com/v1"), "https://example.com/v1/")

    def test_config_never_persists_api_key_in_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = __import__("pathlib").Path(temp) / "cloud_config.json"
            with patch.object(cloud, "CONFIG_PATH", config_path), patch.object(cloud, "_store_key") as store:
                cloud.configure("https://example.com/v1", "test-model", "test-key-secret", False)
            store.assert_called_once_with("test-key-secret")
            raw = config_path.read_text(encoding="utf-8")
            self.assertNotIn("test-key-secret", raw)
            self.assertEqual(json.loads(raw)["model"], "test-model")

    def test_cli_options_change_one_field_and_validate(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = __import__("pathlib").Path(temp) / "cloud_config.json"
            with patch.object(cloud, "CONFIG_PATH", config_path), patch.object(cloud, "_store_key"):
                cloud.configure("https://example.com/v1", "test-model", "test-key-secret", True)
                cloud.set_option("model", "gemini-3.1-flash-lite")
                cloud.set_option("tokens", "16384")
                cloud.set_option("subagents", "off")
                for name, bad in (("tokens", "12"), ("tokens", "lots"), ("model", "two words"),
                                  ("url", "http://example.com/v1"), ("subagents", "maybe"), ("key", "x")):
                    with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                        cloud.set_option(name, bad)
                cfg = cloud.load_config()
            self.assertEqual((cfg["model"], cfg["max_tokens"], cfg["allow_subagents"], cfg["enabled"]),
                             ("gemini-3.1-flash-lite", 16384, False, True))
            self.assertEqual(cfg["base_url"], "https://example.com/v1/")

    def test_autosend_is_off_by_default_and_set_from_cli_and_window(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = __import__("pathlib").Path(temp) / "cloud_config.json"
            with patch.object(cloud, "CONFIG_PATH", config_path), patch.object(cloud, "_store_key"):
                cloud.configure("https://example.com/v1", "test-model", "test-key-secret", True)
                self.assertFalse(cloud.load_config()["auto_approve"])
                cloud.set_option("autosend", "on")
                self.assertTrue(cloud.load_config()["auto_approve"])
                with self.assertRaises(ValueError):
                    cloud.set_option("autosend", "maybe")
                cloud.apply_change({"do": "options", "auto_approve": False})
                self.assertFalse(cloud.load_config()["auto_approve"])

    def test_broken_token_value_in_file_keeps_cloud_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = __import__("pathlib").Path(temp) / "cloud_config.json"
            config_path.write_text(json.dumps({"model": "m", "enabled": True, "max_tokens": "oops"}), encoding="utf-8")
            with patch.object(cloud, "CONFIG_PATH", config_path):
                cfg = cloud.load_config()
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["max_tokens"], cloud.MAX_OUTPUT_TOKENS)


class CloudRequestIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_handler = cloud._APPROVAL_HANDLER
        async def approve(task, source):
            self.approved_payload = task
            self.approved_source = source
            return True
        cloud.set_approval_handler(approve)
        self.payloads = []

        class FakeCompletions:
            async def create(inner_self, **kwargs):
                self.payloads.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="def group_by_parity(values: list[int]) -> dict[str, list[int]]:\n    return {'even': [x for x in values if x % 2 == 0], 'odd': [x for x in values if x % 2 != 0]}"))])

        class FakeClient:
            def __init__(inner_self, **kwargs):
                inner_self.client_args = kwargs
                inner_self.chat = SimpleNamespace(completions=FakeCompletions())
            async def close(inner_self):
                return None
        self.fake_client_cls = FakeClient
        fake_openai = types.ModuleType("openai")
        fake_openai.AsyncOpenAI = FakeClient
        self.patcher = patch.dict(sys.modules, {"openai": fake_openai})
        self.patcher.start()
        self.config_patcher = patch.object(cloud, "load_config", return_value={
            "enabled": True, "base_url": "https://example.com/v1/", "model": "test-code-model", "allow_subagents": True})
        self.config_patcher.start()
        self.key_patcher = patch.object(cloud, "get_api_key", return_value="unit-test-key")
        self.key_patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.config_patcher.stop()
        self.key_patcher.stop()
        cloud.set_approval_handler(self.old_handler)

    async def test_only_explicit_task_is_sent_without_history_or_tools(self):
        task = "Write a pure Python function that groups integers by parity. Include type hints."
        result = await cloud.generate_code(task, source="subagent")
        self.assertIn("Cloud draft", result)
        self.assertEqual(self.approved_payload, task)
        self.assertEqual(self.approved_source, "subagent")
        request = self.payloads[0]
        self.assertEqual(len(request["messages"]), 2)
        self.assertEqual(request["messages"][1], {"role": "user", "content": task})
        self.assertNotIn("tools", request)
        self.assertEqual(set(request), {"model", "messages", "max_tokens", "temperature"})
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertNotIn("C:\\Users", str(request))

    async def test_setup_connection_probe_sends_only_fixed_non_sensitive_text(self):
        result = await cloud.test_connection("https://example.com/v1", "test-code-model", "unit-test-key")
        self.assertIn("Connection successful", result)
        request = self.payloads[0]
        self.assertEqual(len(request["messages"]), 1)
        self.assertEqual(request["messages"][0], {
            "role": "user",
            "content": "Reply with exactly OK. This is a connection test; no user data is included.",
        })
        self.assertNotIn("tools", request)

    async def test_uses_configured_token_limit_and_flags_truncation(self):
        class CutCompletions:
            async def create(inner_self, **kwargs):
                self.payloads.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="def half("), finish_reason="length")])
        sys.modules["openai"].AsyncOpenAI = lambda **kw: SimpleNamespace(
            chat=SimpleNamespace(completions=CutCompletions()), close=self.fake_client_cls(**kw).close)
        with patch.object(cloud, "load_config", return_value={
                "enabled": True, "base_url": "https://example.com/v1/", "model": "m",
                "allow_subagents": True, "max_tokens": 4096}):
            result = await cloud.generate_code("Write a small Python utility function.")
        self.assertEqual(self.payloads[0]["max_tokens"], 4096)
        self.assertIn("Truncated", result)
        self.assertIn("/cloud tokens", result)

    async def test_cancel_sends_nothing(self):
        async def reject(task, source):
            return False
        cloud.set_approval_handler(reject)
        result = await cloud.generate_code("Write a small Python utility function.")
        self.assertIn("No data was sent", result)
        self.assertEqual(self.payloads, [])

    async def test_headless_without_explicit_opt_in_fails_closed(self):
        cloud.set_approval_handler(None)
        with patch.dict(os.environ, {"MAXEDCODE_CLOUD_AUTO_APPROVE": "0"}):
            result = await cloud.generate_code("Write a small Python utility function.")
        self.assertIn("No data was sent", result)
        self.assertEqual(self.payloads, [])

    async def test_autosend_skips_the_question_but_not_the_privacy_guard(self):
        async def never(task, source):
            raise AssertionError("autosend must not ask")
        cloud.set_approval_handler(never)
        cfg = {"enabled": True, "base_url": "https://example.com/v1/", "model": "m", "allow_subagents": True,
               "auto_approve": True}
        with patch.object(cloud, "load_config", return_value=cfg):
            result = await cloud.generate_code("Write a small Python utility function.")
            blocked = await cloud.generate_code("Fix C:\\Users\\me\\project\\main.py")
        self.assertIn("Cloud draft", result)
        self.assertIn("privacy guard", blocked)
        self.assertEqual(len(self.payloads), 1)

    async def test_child_cloud_calls_can_be_disabled(self):
        with patch.object(cloud, "load_config", return_value={
            "enabled": True, "base_url": "https://example.com/v1/", "model": "test", "allow_subagents": False}):
            result = await cloud.generate_code("Write a small Python utility function.", source="subagent")
        self.assertIn("subagents is disabled", result)
        self.assertEqual(self.payloads, [])


class CloudAccountsTests(unittest.IsolatedAsyncioTestCase):
    """Many keys and providers: the agent picks the model, a limited key hands over to the next one."""

    CFG = {"enabled": True, "allow_subagents": True, "max_tokens": 1024, "notes": {"fast-1": "quick drafts"},
           "accounts": [{"id": "default", "label": "gemini", "base_url": "https://a.example/v1/", "model": "fast-1",
                         "models": ["fast-1"]},
                        {"id": "k2", "label": "groq", "base_url": "https://b.example/v1/", "model": "big-2",
                         "models": ["big-2", "fast-1"]}]}

    async def test_route_list_note_and_fallover(self):
        cfg = json.loads(json.dumps(self.CFG))
        self.assertEqual([a["id"] for a in cloud.route("fast-1", cfg)], ["default", "k2"])
        self.assertEqual([a["id"] for a in cloud.route("big-2", cfg)], ["k2"])
        self.assertEqual(cloud.route(None, cfg)[0]["use"], "fast-1")
        listing = cloud.catalog(cfg)
        self.assertIn("fast-1  [default]  via gemini, groq: quick drafts", listing)
        self.assertIn("big-2", listing)
        self.assertIn("web_search", listing)          # a model without a note asks the agent to look it up
        self.assertNotIn("example", listing.split("\n")[0] + "key")
        calls = []

        async def fake_ask(account, key, task, limit):
            calls.append(account["id"])
            return ("RateLimitError: 429 quota exceeded", True) if account["id"] == "default" else ("draft from k2", False)

        with patch.object(cloud, "load_config", return_value=cfg), patch.object(cloud, "get_api_key", return_value="k"),\
             patch.object(cloud, "_ask", fake_ask), patch.dict(os.environ, {"MAXEDCODE_CLOUD_AUTO_APPROVE": "1"}),\
             patch.object(cloud, "_APPROVAL_HANDLER", None):
            out = await cloud.run_tool({"task": "Write a function that adds two integers.", "model": "fast-1"})
        self.assertEqual(calls, ["default", "k2"])
        self.assertTrue(out.startswith("draft from k2"))
        self.assertEqual(cloud.provider_for_key("AIza" + "x" * 30), "gemini")
        self.assertEqual(cloud.provider_for_key("sk-ant-abc"), "anthropic")


class CloudWindowTests(unittest.TestCase):
    """Settings -> Cloud in the window and /cloud in the CLI change the same file through the same functions."""

    def test_add_default_note_remove_and_no_key_in_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            base = __import__("pathlib").Path(temp)
            keys = {}
            with patch.object(cloud, "CONFIG_PATH", base / "cloud_config.json"), \
                    patch.object(cloud, "_store_key", side_effect=lambda k, aid=None: keys.__setitem__(aid or "default", k)), \
                    patch.object(cloud, "get_api_key", side_effect=lambda aid=None: keys.get(aid or "default", "")), \
                    patch.object(cloud, "fetch_models", return_value=["m-fast", "m-big", "text-embedding-1"]):
                cloud.apply_change({"do": "add", "key": "AIza" + "k" * 35})
                snap = cloud.apply_change({"do": "add", "provider": "groq", "key": "gsk_secret_value", "model": "m-big"})
                self.assertEqual([a["label"] for a in snap["accounts"]], ["gemini", "groq"])
                self.assertNotIn("gsk_secret_value", json.dumps(snap))
                self.assertNotIn("gsk_secret_value", (base / "cloud_config.json").read_text(encoding="utf-8"))
                second = snap["accounts"][1]["id"]
                snap = cloud.apply_change({"do": "default", "id": second})
                self.assertEqual((snap["accounts"][0]["label"], cloud.load_config()["model"]), ("groq", "m-big"))
                cloud.apply_change({"do": "note", "model": "m-big", "note": "strong coder"})
                self.assertIn("m-big  [default]  via groq, gemini: strong coder", cloud.catalog())
                snap = cloud.apply_change({"do": "remove", "id": second})
                self.assertEqual([a["label"] for a in snap["accounts"]], ["gemini"])


if __name__ == "__main__":
    unittest.main()
