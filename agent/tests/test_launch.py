"""Run a model on this PC (maxedcode/launch.py): contexts offered, the remembered choice, GGUF files found and
read, the Strata size flags, a busy port never freed by killing an unknown program, and the LM Studio start
(one slot, nothing reloaded when it already serves the choice). No server is started: commands are faked."""
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-launch-tests-")
os.environ.setdefault("AGENT_HOME", _HOME.name)

from maxedcode import launch, servers  # noqa: E402

# config may be imported already with another home (several suites in one process): never the real one
launch.home = lambda: Path(_HOME.name)


def gguf(path, ctx, arch="llama", extra_array=True):
    """A minimal GGUF header: general.architecture, an array before the context (as tokenizers have), ctx."""
    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [s("general.architecture") + struct.pack("<I", 8) + s(arch)]
    if extra_array:
        kv.append(s("general.tags") + struct.pack("<I", 9) + struct.pack("<IQ", 8, 2) + s("a") + s("bc"))
    kv.append(s(f"{arch}.context_length") + struct.pack("<I", 4) + struct.pack("<I", ctx))
    Path(path).write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv)) + b"".join(kv))


class Contexts(unittest.TestCase):
    def test_steps_stop_at_what_the_model_takes_and_keep_its_own(self):
        self.assertEqual(launch.ctx_options(131072), [16384, 32768, 65536, 131072])
        self.assertEqual(launch.ctx_options(None, 98304), [16384, 32768, 65536, 98304, 131072, 262144])
        self.assertEqual(launch.ctx_options(8192), [16384])          # never below what the agent needs

    def test_the_last_context_used_with_a_model_wins(self):
        st = {"ctx": {"lmstudio|m": 65536}}
        self.assertEqual(launch.default_ctx("lmstudio", {"id": "m", "max_ctx": 131072}, st), 65536)
        self.assertEqual(launch.default_ctx("lmstudio", {"id": "x", "max_ctx": 131072}, st), 32768)
        self.assertEqual(launch.default_ctx("strata", {"id": "s", "ctx": 98304}, st), 98304)
        self.assertEqual(launch.default_ctx("ollama", {"id": "y", "max_ctx": 20000}, st), 20000)

    def test_remember_keeps_choices_per_server(self):
        launch.remember("lmstudio", model="a", ctx=32768)
        launch.remember("strata", script=str(Path(_HOME.name) / "start.bat"))
        st = launch.load_state()
        self.assertEqual(st["kind"], "strata")
        self.assertEqual(st["model"]["lmstudio"], "a")
        self.assertEqual(st["ctx"]["lmstudio|a"], 32768)
        self.assertEqual(st["scripts"][0], str(Path(_HOME.name) / "start.bat"))


class Gguf(unittest.TestCase):
    def test_context_is_read_from_the_header_past_arrays(self):
        with tempfile.TemporaryDirectory() as d:
            gguf(Path(d, "m.gguf"), 131072, "qwen3")
            self.assertEqual(launch.gguf_ctx(Path(d, "m.gguf")), 131072)
            Path(d, "bad.gguf").write_bytes(b"nope")
            self.assertIsNone(launch.gguf_ctx(Path(d, "bad.gguf")))

    def test_projectors_and_later_parts_are_not_models(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("big-00001-of-00002.gguf", "big-00002-of-00002.gguf", "mmproj-big.gguf", "tiny.gguf"):
                gguf(Path(d, name), 4096)
            with open(Path(d, "solo.gguf"), "wb") as f:
                f.truncate(60_000_000)
            with patch.object(launch, "gguf_dirs", lambda: [Path(d)]):
                names = sorted(p.name for p, _ in launch.gguf_files())
            # tiny.gguf is too small to be a model; the split model counts once
            self.assertEqual(names, ["big-00001-of-00002.gguf", "solo.gguf"])


class Strata(unittest.TestCase):
    def test_a_size_with_a_family_prefix_is_split_when_the_plain_name_fails(self):
        calls = []

        def run(cmd, timeout=60, **kw):
            calls.append(cmd)
            return 0, json.dumps({"ok": "coder-iq1_m" not in cmd})
        with patch.object(launch, "run", run):
            self.assertEqual(launch.strata_flags(["strata"], "iq3_xxs", 32768), ["--model", "iq3_xxs"])
            self.assertEqual(calls, [])         # no dash: nothing asked
            self.assertEqual(launch.strata_flags(["strata"], "coder-iq1_m", 32768),
                             ["--family", "coder", "--model", "iq1_m"])
            self.assertEqual(launch.strata_flags(["strata"], "ud-q4_k_xl", 32768), ["--model", "ud-q4_k_xl"])

    def test_json_after_other_lines(self):
        self.assertEqual(launch._json('note\n{"ok": true}'), {"ok": True})
        self.assertIsNone(launch._json("no json"))


class Port(unittest.TestCase):
    def test_an_unknown_program_on_the_port_is_never_killed(self):
        said = []
        with patch.object(servers, "port_down", lambda *a, **k: False), \
                patch.object(servers, "kind_of", lambda *a, **k: ""), \
                patch.object(launch, "listener", lambda port: 4242), \
                patch.object(launch, "kill") as kill, patch.object(launch, "stop") as stop:
            self.assertFalse(launch.free_port("llamacpp", lambda t, p=None: said.append(t)))
        kill.assert_not_called()
        stop.assert_not_called()
        self.assertIn("close it first", said[0])

    def test_another_model_server_is_stopped_first(self):
        downs = iter([False, True])
        with patch.object(servers, "port_down", lambda *a, **k: next(downs, True)), \
                patch.object(servers, "kind_of", lambda *a, **k: "strata"), \
                patch.object(launch, "stop") as stop:
            self.assertTrue(launch.free_port("llamacpp", lambda t, p=None: None))
        stop.assert_called_once_with("strata")


class LmStudio(unittest.TestCase):
    def setUp(self):
        self.cmds = []
        self.loaded = []

        def run(cmd, timeout=60, **kw):
            self.cmds.append(cmd)
            return 0, "  --parallel <count>  ..." if cmd[1:3] == ["load", "--help"] else ""

        class Proc:
            def poll(self_):
                return 0

        def spawn(cmd, log=None, **kw):
            self.cmds.append(cmd)
            self.loaded[:] = [(cmd[2], int(cmd[cmd.index("--context-length") + 1]))]
            return Proc()
        self.stack = [patch.object(launch, "run", run), patch.object(launch, "spawn", spawn),
                      patch.object(launch, "lms_cli", lambda: "lms"),
                      patch.object(servers, "port_down", lambda *a, **k: False),
                      patch.object(servers, "loaded_models", lambda *a, **k: list(self.loaded))]
        for p in self.stack:
            p.start()

    def tearDown(self):
        for p in self.stack:
            p.stop()

    def test_load_with_one_slot_after_unloading_another_model(self):
        self.loaded = [("other", 4096)]
        r = launch.lms_start("gemma", 32768, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertIn(["lms", "unload", "--all"], self.cmds)
        load = next(c for c in self.cmds if c[1] == "load" and "-y" in c)
        self.assertEqual(load[:5], ["lms", "load", "gemma", "--parallel", "1"])
        self.assertIn("32768", load)

    def test_nothing_reloads_when_the_choice_is_already_served(self):
        self.loaded = [("gemma", 32768)]
        r = launch.lms_start("gemma", 32768, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertFalse(any(c[1] in ("load", "unload") for c in self.cmds if len(c) > 1 and c[2:3] != ["--help"]))


class Start(unittest.TestCase):
    def test_an_unknown_kind_and_offline_answer_with_words(self):
        self.assertFalse(launch.start("vllm")["ok"])
        with patch.dict(os.environ, {"MAXEDCODE_OFFLINE": "1"}):
            self.assertIn("offline", launch.start("lmstudio", "m", 32768)["error"])

    def test_a_crash_inside_an_adapter_is_an_error_not_a_traceback(self):
        with patch.dict(launch.START, {"ollama": lambda *a, **k: 1 / 0}), \
                patch.dict(os.environ, {"MAXEDCODE_OFFLINE": ""}):
            r = launch.start("ollama", "m", 32768)
        self.assertFalse(r["ok"])
        self.assertIn("ZeroDivisionError", r["error"])


if __name__ == "__main__":
    unittest.main()
