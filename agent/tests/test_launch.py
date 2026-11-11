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
        own = launch.own_ways(st)
        self.assertEqual(own[0]["command"], str(Path(_HOME.name) / "start.bat"))   # a picked script: an own way
        self.assertEqual(st["way"]["strata"], own[0]["id"])


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

    def test_another_model_server_started_from_here_makes_room(self):
        downs = iter([False, True])
        record = {"kind": "strata", "server": "strata", "base": "http://127.0.0.1:8080/v1"}
        with patch.object(servers, "port_down", lambda *a, **k: next(downs, True)), \
                patch.object(servers, "kind_of", lambda *a, **k: "strata"), \
                patch.object(launch, "mine", lambda **k: record), patch.object(launch, "release") as release:
            self.assertTrue(launch.free_port("llamacpp", lambda t, p=None: None))
        release.assert_called_once()
        self.assertEqual(release.call_args[0][0], record)

    def test_a_model_server_started_by_hand_is_left_alone(self):
        said = []
        with patch.object(servers, "port_down", lambda *a, **k: False), \
                patch.object(servers, "kind_of", lambda *a, **k: "strata"), \
                patch.object(launch, "mine", lambda **k: None), \
                patch.object(launch, "release") as release, patch.object(launch, "stop") as stop:
            self.assertFalse(launch.free_port("llamacpp", lambda t, p=None: said.append(t)))
        release.assert_not_called()
        stop.assert_not_called()
        self.assertIn("outside maxedcode", said[0])


class Ours(unittest.TestCase):
    """What a start from here brought up is closed by the next start from here; the user's own servers never."""
    def setUp(self):
        launch.save_state({})

    def test_the_record_holds_only_while_the_same_process_listens(self):
        with patch.object(launch, "listener", lambda port: 111):
            launch.keep_ours({"kind": "strata", "server": "strata", "base": "http://127.0.0.1:8080/v1"})
        with patch.object(servers, "port_down", lambda *a, **k: False):
            with patch.object(launch, "listener", lambda port: 111):
                self.assertEqual(launch.mine("strata")["kind"], "strata")
                self.assertIsNone(launch.mine("lmstudio"))
            with patch.object(launch, "listener", lambda port: 222):     # started again by hand
                self.assertIsNone(launch.mine("strata"))
        with patch.object(servers, "port_down", lambda *a, **k: True):    # not running any more
            self.assertIsNone(launch.mine("strata"))

    def test_a_start_closes_the_previous_start_from_here_of_another_program(self):
        prev = {"kind": "lmstudio", "server": "lmstudio", "base": "http://127.0.0.1:1234/v1", "model": "gemma"}
        with patch.object(launch, "mine", lambda *a, **k: prev), patch.object(launch, "release") as release, \
                patch.dict(launch.START, {"strata": lambda *a, **k: launch._done("strata", "http://127.0.0.1:8080/v1", None)}), \
                patch.object(launch, "keep_ours") as keep, patch.object(launch, "use", lambda r: True), \
                patch.object(servers, "describe", lambda *a, **k: {"kind": "strata"}), \
                patch.dict(os.environ, {"MAXEDCODE_OFFLINE": ""}):
            r = launch.start("strata", "iq3_xxs", 65536)
        self.assertTrue(r["ok"])
        self.assertEqual(release.call_args[0][0], prev)
        self.assertEqual(keep.call_args[0][0]["base"], "http://127.0.0.1:8080/v1")
        self.assertTrue(keep.call_args[0][0]["serve"])

    def test_release_unloads_only_what_was_loaded_from_here(self):
        cmds = []
        with patch.object(launch, "lms_cli", lambda: "lms"), patch.object(launch, "run", lambda c, *a, **k: cmds.append(c) or (0, "")), \
                patch.object(servers, "loaded_models", lambda *a, **k: [("users-own", 8192)]):
            launch.release({"kind": "lmstudio", "server": "lmstudio", "base": "http://127.0.0.1:1234/v1",
                            "model": "gemma", "serve": True}, lambda *a: None)
        self.assertEqual(cmds, [["lms", "unload", "gemma"]])      # the server holds the user's model: it stays up


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

    def test_a_model_loaded_by_hand_stays_and_one_slot_is_the_default(self):
        self.loaded = [("other", 4096)]
        with patch.object(launch, "mine", lambda *a, **k: None):
            r = launch.lms_start("gemma", 32768, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertFalse(any(c[1] == "unload" for c in self.cmds))
        load = next(c for c in self.cmds if c[1] == "load" and "-y" in c)
        self.assertEqual(load[:3], ["lms", "load", "gemma"])
        self.assertEqual(load[load.index("--parallel") + 1], "1")       # the default of the setting
        self.assertEqual(load[load.index("--context-length") + 1], "32768")

    def test_the_chosen_settings_and_own_flags_reach_lms_load(self):
        r = launch.lms_start("gemma", 65536, lambda t, p=None: None, opts={"gpu": "max", "parallel": "2", "args": "--ttl 600"})
        self.assertTrue(r["ok"])
        load = next(c for c in self.cmds if c[1] == "load" and "-y" in c)
        self.assertEqual(load[load.index("--gpu") + 1], "max")
        self.assertEqual(load[load.index("--parallel") + 1], "2")
        self.assertEqual(load[load.index("--ttl") + 1], "600")

    def test_nothing_reloads_when_the_choice_is_already_served(self):
        self.loaded = [("gemma", 32768)]
        r = launch.lms_start("gemma", 32768, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertFalse(any(c[1] in ("load", "unload") for c in self.cmds if len(c) > 1 and c[2:3] != ["--help"]))


class LmStudioOurs(unittest.TestCase):
    def test_the_model_loaded_from_here_before_is_unloaded_by_its_id(self):
        cmds, loaded = [], [("old-from-here", 32768), ("users-own", 8192)]

        class Proc:
            def poll(self_):
                return 0

        def spawn(cmd, log=None, **kw):
            cmds.append(cmd)
            loaded.append((cmd[2], 32768))
            return Proc()
        prev = {"kind": "lmstudio", "server": "lmstudio", "base": "http://127.0.0.1:1234/v1", "model": "old-from-here"}
        with patch.object(launch, "run", lambda c, *a, **k: cmds.append(c) or (0, "--parallel")), \
                patch.object(launch, "spawn", spawn), patch.object(launch, "lms_cli", lambda: "lms"), \
                patch.object(servers, "port_down", lambda *a, **k: False), \
                patch.object(servers, "loaded_models", lambda *a, **k: list(loaded)), \
                patch.object(launch, "mine", lambda *a, **k: prev):
            r = launch.lms_start("gemma", 32768, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertIn(["lms", "unload", "old-from-here"], cmds)
        self.assertFalse(any(c[1:3] == ["unload", "--all"] or c[1:3] == ["unload", "users-own"] for c in cmds))


class LmStudioOff(unittest.TestCase):
    def test_a_look_at_the_models_never_wakes_lm_studio(self):
        """`lms` starts LM Studio: with its server off the list comes from its folder, publisher/repo/file."""
        with tempfile.TemporaryDirectory() as d:
            for rel in ("pub/repo-GGUF/model-Q4.gguf", "Strata-shards/part-00001-of-00002.gguf"):
                Path(d, rel).parent.mkdir(parents=True, exist_ok=True)
                gguf(Path(d, rel), 8192)
                with open(Path(d, rel), "r+b") as f:
                    f.truncate(60_000_000)
            st = launch.load_state()
            st.pop("lms_models", None)
            launch.save_state(st)
            with patch.object(servers, "port_down", lambda *a, **k: True),                     patch.object(launch, "lms_folder", lambda: Path(d)),                     patch.object(launch, "run") as run:
                models = launch.lms_models()
            run.assert_not_called()
            self.assertEqual([m["id"] for m in models], ["pub/repo-GGUF/model-Q4.gguf"])


class Ways(unittest.TestCase):
    def setUp(self):
        launch.save_state({})

    def test_own_found_preset_and_builtin_ways_in_order(self):
        launch.save_way("strata", "mine 64K", "D:/AI/mine.bat")
        launch.save_way("lmstudio", "lm", "lms load x")
        ws = launch.ways("strata", cli=True, presets=["fast"], scripts=["D:/AI/Strata/launcher/bat/start.bat"])
        self.assertEqual([w["source"] for w in ws], ["builtin", "program", "found", "own"])
        self.assertEqual(ws[1]["id"], "preset:fast")
        self.assertEqual(ws[3]["name"], "mine 64K")
        self.assertEqual([w["source"] for w in launch.ways("strata", cli=None)], ["own"])   # no strata command

    def test_save_edit_and_remove_an_own_way(self):
        w = launch.save_way("other", "kobold", "koboldcpp.exe --config a.kcpps", ":5001")
        self.assertTrue(w["base"].endswith(":5001/v1"))
        same = launch.save_way("other", "kobold 2", "koboldcpp.exe --config b.kcpps", "127.0.0.1:5001", w["id"])
        self.assertEqual(same["id"], w["id"])
        self.assertEqual(len(launch.own_ways()), 1)
        self.assertEqual(launch.own_ways()[0]["command"], "koboldcpp.exe --config b.kcpps")
        launch.remember("other", way=w["id"])
        launch.remove_way(w["id"])
        self.assertEqual(launch.own_ways(), [])
        self.assertNotIn("other", launch.load_state().get("way") or {})

    def test_the_first_versions_picked_scripts_become_own_ways(self):
        launch.save_state({"scripts": ["D:/x/go.bat"]})
        own = launch.own_ways()
        self.assertEqual((own[0]["kind"], own[0]["command"], own[0]["name"]), ("strata", "D:/x/go.bat", "go"))

    def test_an_address_in_any_form(self):
        self.assertEqual(launch.norm_base("", "ollama"), "http://127.0.0.1:11434/v1")
        self.assertEqual(launch.norm_base("192.168.0.10", "lmstudio"), "http://192.168.0.10:1234/v1")
        self.assertEqual(launch.norm_base("127.0.0.1:5001", "other"), "http://127.0.0.1:5001/v1")
        self.assertEqual(launch.norm_base(":8000", "other"), "http://127.0.0.1:8000/v1")
        self.assertEqual(launch.norm_base("1337", "other"), "http://127.0.0.1:1337/v1")


class Settings(unittest.TestCase):
    def test_flags_env_and_defaults(self):
        self.assertEqual(launch.opt_flags("strata", {"kv": "q4_0", "vision": "", "args": "--set a=b"}),
                         ["--kv", "q4_0", "--set", "a=b"])
        self.assertEqual(launch.opt_flags("lmstudio", {}), ["--parallel", "1"])     # one slot unless chosen
        env = launch.opt_env("ollama", {"flash": "1", "kv": "q8_0", "env": "OLLAMA_DEBUG=1 bad-name=x"})
        self.assertEqual(env, {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NUM_PARALLEL": "1",
                               "OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_DEBUG": "1"})
        self.assertEqual([o["key"] for o in launch.options("ollama")][-1], "env")     # no flags for ollama serve
        self.assertEqual(launch.options("other"), [])


class Script(unittest.TestCase):
    def test_an_own_way_runs_in_its_window_and_the_agent_follows_its_address(self):
        launched = []
        answers = iter([False, False, True])
        with patch.object(launch, "console", lambda command, **k: launched.append(command)), \
                patch.object(servers, "port_down", lambda *a, **k: True), \
                patch.object(launch, "_ready", lambda kind, base: next(answers, True)), \
                patch.object(servers, "kind_of", lambda *a, **k: "koboldcpp"), \
                patch.object(launch.time, "sleep", lambda s: None):
            r = launch.script_start("other", {"id": "own:1", "command": "koboldcpp.exe --config a.kcpps",
                                              "base": "http://127.0.0.1:5001/v1", "name": "kobold"}, lambda t, p=None: None)
        self.assertTrue(r["ok"])
        self.assertEqual((r["kind"], r["base"]), ("koboldcpp", "http://127.0.0.1:5001/v1"))
        self.assertEqual(launched, ["koboldcpp.exe --config a.kcpps"])

    def test_a_missing_script_file_and_a_missing_address_are_said(self):
        self.assertIn("not there", launch.script_start("strata", {"command": "D:/nope/x.bat"}, lambda *a: None)["error"])
        self.assertIn("address", launch.script_start("other", {"command": "kobold.exe"}, lambda *a: None)["error"])


class Programs(unittest.TestCase):
    """Programs in places of their own: found on the disks, or pointed at by the user; servers on other ports."""
    def setUp(self):
        launch.save_state({})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        return path

    def test_find_programs_looks_through_the_folders_and_keeps_what_it_found(self):
        llama = self.make("tools", "llama", "bin", "llama-server" + launch.EXE)
        self.make("node_modules", "x", "ollama" + launch.EXE)                 # a skipped folder
        kobold = self.make("ai", "koboldcpp_cu12" + (launch.EXE or ".py"))
        with patch.object(launch.shutil, "which", lambda name: None), patch.object(launch, "ollama_cli", lambda: None),                 patch.object(launch, "lms_cli", lambda: None), patch.object(servers, "strata_source", lambda: None):
            found, kobolds = launch.find_programs(10, roots=[(self.root, 4)])
        self.assertEqual(found, {"llamacpp": str(llama)})
        self.assertEqual(kobolds, [str(kobold)])
        self.assertEqual(launch.load_state()["paths"]["llamacpp"], str(llama))
        self.assertEqual(launch.llama_cli(), str(llama))
        self.assertEqual([w["name"] for w in launch.ways(launch.OTHER)], [kobold.stem])     # a found way of "other"

    def test_a_picked_file_must_be_the_programs_own(self):
        llama = self.make("llama-server" + launch.EXE)
        other = self.make("notepad" + launch.EXE)
        self.assertEqual(launch.set_path("llamacpp", str(other)), None)
        self.assertEqual(launch.set_path("llamacpp", f'"{llama}"'), str(llama))
        strata = self.make("Strata", "strata.py")
        self.make("Strata", "bin", "strata.cmd")
        self.assertEqual(launch.check_path("strata", str(self.make("Strata", "engine", "x.txt"))), str(strata.parent))

    def test_a_port_that_asks_for_a_login_is_no_model_server(self):
        import urllib.error

        def refuse(*a, **k):
            raise urllib.error.HTTPError("u", 401, "login", {}, None)
        with patch.object(servers, "model_ids", refuse):
            self.assertIsNone(servers._probe_other(3091, "", 0.3))


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
