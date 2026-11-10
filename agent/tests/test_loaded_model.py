"""LM Studio / Ollama load whatever listed model a request names: maxedcode must name the loaded one, with its
real window, and never make the server load a second model (or the same one again with another context)."""
import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-loaded-model-tests-")
os.environ.setdefault("AGENT_HOME", _HOME.name)

from maxedcode import config, headless, servers, subagent  # noqa: E402

CONFIGURED = "zai-org/glm-4.6v-flash"


def lms(*models):
    """A fake LM Studio /api/v0/models: (id, state, loaded window) per model."""
    return {"/api/v0/models": {"data": [{"id": i, "type": "vlm" if "glm" in i else "llm", "state": s,
                                         "max_context_length": 131072,
                                         **({"loaded_context_length": n} if n else {})} for i, s, n in models]},
            "/v1/models": {"data": [{"id": i} for i, _, _ in models]}}


def ollama(*loaded, listed=()):
    return {"/api/version": {"version": "0.12"},
            "/api/ps": {"models": [{"name": i, "model": i, "context_length": n} for i, n in loaded]},
            "/v1/models": {"data": [{"id": i} for i in listed or [i for i, _ in loaded]]}}


def server(answers, calls=None):
    """servers.http over a dict of endpoint answers; every other endpoint is a 404."""
    def fake(url, key="", body=None, timeout=0):
        if calls is not None:
            calls.append(url)
        for k, v in answers.items():
            if url.endswith(k):
                return v
        raise OSError("404")
    return fake


class PickModelTests(unittest.TestCase):
    def test_configured_one_loaded(self):
        on = [("other", 8192), (CONFIGURED, 32768)]
        self.assertEqual(servers.pick_model(CONFIGURED, on, "lmstudio"), (CONFIGURED, 32768, ""))

    def test_a_window_too_small_to_work_in_is_said_plainly(self):
        # live LM Studio: maxedcode's instructions and tools are ~9.3k tokens; at 8192 the server only said "400"
        model, ctx, note = servers.pick_model(CONFIGURED, [(CONFIGURED, 8192)], "lmstudio")
        self.assertEqual((model, ctx), (CONFIGURED, 8192))
        self.assertIn("8192", note)
        self.assertIn(str(servers.MIN_WINDOW), note)
        self.assertEqual(servers.pick_model(CONFIGURED, [(CONFIGURED, 16384)], "lmstudio")[2], "")

    def test_another_one_loaded_is_used_and_said(self):
        model, ctx, note = servers.pick_model(CONFIGURED, [("qwen3-8b", 8192)], "lmstudio")
        self.assertEqual((model, ctx), ("qwen3-8b", 8192))
        self.assertIn("qwen3-8b", note)
        self.assertIn(CONFIGURED, note)

    def test_several_loaded_without_the_configured_one_takes_the_first(self):
        model, _, note = servers.pick_model(CONFIGURED, [("a", 4096), ("b", 8192)], "lmstudio")
        self.assertEqual(model, "a")
        self.assertTrue(note)
        self.assertEqual(servers.pick_model(None, [("a", 4096), ("b", 8192)], "ollama")[0], "a")

    def test_nothing_loaded_keeps_the_choice_and_says_the_server_loads_it(self):
        model, ctx, note = servers.pick_model(CONFIGURED, [], "lmstudio")
        self.assertEqual((model, ctx), (CONFIGURED, None))
        self.assertIn(CONFIGURED, note)
        model, _, note = servers.pick_model(None, [], "lmstudio")
        self.assertIsNone(model)
        self.assertIn("load one", note)

    def test_server_that_does_not_say_keeps_the_choice_silently(self):
        self.assertEqual(servers.pick_model(CONFIGURED, None, "lmstudio"), (CONFIGURED, None, ""))

    def test_ollama_latest_tag_is_the_same_model(self):
        self.assertEqual(servers.pick_model("qwen3", [("qwen3:latest", 32768)], "ollama"), ("qwen3:latest", 32768, ""))
        self.assertFalse(servers.same_model("qwen3", "qwen3:8b"))


class ResolveTests(unittest.TestCase):
    """config.resolve_server / follow_loaded with config.json = {"model": CONFIGURED, "ctx": 32768}."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("BASE_URL", "http://127.0.0.1:1234/v1"), ("SERVER_KIND", ""), ("MODEL", CONFIGURED),
                            ("MODEL_SET", True), ("CHOSEN", ""), ("CTX_WINDOW", 32768), ("CTX_SRC", "default"),
                            ("_KEEP_RAW", "0")):
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(servers, "port_down", lambda *a, **k: False))

    def answer(self, answers, calls=None):
        self.stack.enter_context(patch.object(servers, "http", server(answers, calls)))

    def test_configured_model_loaded_gets_its_real_window(self):
        self.answer(lms(("qwen3-8b", "not-loaded", None), (CONFIGURED, "loaded", 16384)))
        got = config.resolve_server()
        self.assertEqual((got["kind"], got["model"], got["ctx"], got["note"]), ("lmstudio", CONFIGURED, 16384, ""))

    def test_other_model_loaded_is_used_not_jit_loaded(self):
        self.answer(lms((CONFIGURED, "not-loaded", None), ("qwen3-8b", "loaded", 8192)))
        got = config.resolve_server()
        self.assertEqual((got["model"], got["ctx"], config.MODEL), ("qwen3-8b", 8192, "qwen3-8b"))
        self.assertIn("qwen3-8b", got["note"])

    def test_several_loaded_prefers_the_configured_one(self):
        self.answer(lms(("qwen3-8b", "loaded", 8192), (CONFIGURED, "loaded", 24576)))
        got = config.resolve_server()
        self.assertEqual((got["model"], got["ctx"]), (CONFIGURED, 24576))

    def test_nothing_loaded_keeps_the_configured_model_and_window(self):
        self.answer(lms((CONFIGURED, "not-loaded", None), ("qwen3-8b", "not-loaded", None)))
        got = config.resolve_server()
        self.assertEqual((got["model"], got["ctx"]), (CONFIGURED, 32768))
        self.assertIn("no model loaded", got["note"])

    def test_nothing_loaded_or_chosen_takes_the_only_downloaded_model(self):
        config.MODEL_SET, config.MODEL = False, "local"
        self.answer(lms(("qwen3-8b", "not-loaded", None)))
        self.assertEqual(config.resolve_server()["model"], "qwen3-8b")

    def test_set_window_larger_than_the_loaded_one_is_cut_to_it(self):
        config.CTX_SRC, config.CTX_WINDOW = "settings.json", 65536
        self.answer(lms((CONFIGURED, "loaded", 4096)))
        got = config.resolve_server()
        self.assertEqual(got["ctx"], 4096)          # 65536 would compact far too late and overflow
        config.CTX_SRC, config.CTX_WINDOW = "settings.json", 8192
        self.stack.enter_context(patch.object(servers, "http", server(lms((CONFIGURED, "loaded", 16384)))))
        self.assertEqual(config.resolve_server()["ctx"], 8192)     # a smaller set window stays

    def test_ollama_loaded_model_and_its_window(self):
        config.BASE_URL = "http://127.0.0.1:11434/v1"
        config.MODEL = "qwen3"
        self.answer(ollama(("llama3.2:3b", 8192), listed=("qwen3:latest", "llama3.2:3b")))
        got = config.resolve_server()
        self.assertEqual((got["kind"], got["model"], got["ctx"]), ("ollama", "llama3.2:3b", 8192))

    def test_model_switched_in_lm_studio_between_turns_is_followed_in_one_request(self):
        self.answer(lms((CONFIGURED, "not-loaded", None), ("qwen3-8b", "loaded", 8192)))
        config.resolve_server()
        self.assertEqual(config.MODEL, "qwen3-8b")
        calls = []
        self.stack.enter_context(patch.object(servers, "http", server(
            lms((CONFIGURED, "loaded", 32768), ("qwen3-8b", "not-loaded", None)), calls)))
        got = config.follow_loaded()
        self.assertEqual((got["model"], got["ctx"], got["note"]), (CONFIGURED, 32768, ""))   # back to the choice
        self.assertEqual(len(calls), 1)

    def test_follow_loaded_finds_lm_studio_by_its_port_and_costs_nothing_elsewhere(self):
        self.answer(lms(("qwen3-8b", "loaded", 8192)))
        self.assertEqual(config.follow_loaded()["model"], "qwen3-8b")
        self.assertEqual(config.SERVER_KIND, "lmstudio")
        config.BASE_URL, config.SERVER_KIND, config.MODEL, config.CHOSEN = "http://127.0.0.1:8080/v1", "strata", "x", ""
        with patch.object(servers, "http", side_effect=AssertionError("no request")), \
                patch.object(servers, "port_down", side_effect=AssertionError("no probe")):
            self.assertEqual(config.follow_loaded()["model"], "x")


class SideRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_subagent_without_a_model_uses_the_resolved_one(self):
        """/init and other standalone children read the model at call time, not the one from import."""
        seen = []

        async def create(**kw):
            seen.append(kw["model"])
            msg = SimpleNamespace(content="done", tool_calls=None, model_extra={})
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(config, "MODEL", "qwen3-8b"), patch.object(config, "SUBAGENT_STREAM", False), \
                patch.object(subagent, "AsyncOpenAI", return_value=client):
            await subagent.run("look at the project", parent_session="sid")
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"qwen3-8b"})


class NoticeTests(unittest.TestCase):
    def test_model_note_is_said_once_per_change(self):
        with tempfile.TemporaryDirectory() as home, patch.object(config, "HOME", Path(home)), \
                patch.object(headless, "FORMAT", "text"):
            said = []
            with patch.object(headless, "notice", lambda kind, text: said.append(text)), redirect_stderr(io.StringIO()):
                for note in ("A loaded, not B", "A loaded, not B", "", "A loaded, not B"):
                    headless.model_notice(note)
        self.assertEqual(said, ["A loaded, not B", "A loaded, not B"])


if __name__ == "__main__":
    unittest.main()
