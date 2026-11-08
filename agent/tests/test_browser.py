"""The user's own browser: extension id, native host protocol, the browser tool and its browser helper."""
import base64
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-browser-tests-")
os.environ["AGENT_HOME"] = _HOME.name

from maxedcode import chrome, config, subagent
from maxedcode.chrome import host
from maxedcode.tools import browser
from maxedcode.tools.catalog import session_catalog


# a 1x1 white PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC")


def names(specs):
    return [s["function"]["name"] for s in specs]


class FakeExtension:
    """The extension's side of native messaging: answers every request with the action it got."""

    def __init__(self):
        to_host_r, self.to_host = os.pipe()
        self.from_host, from_host_w = os.pipe()
        self.bridge = host.Bridge(os.fdopen(to_host_r, "rb"), os.fdopen(from_host_w, "wb"))
        self.seen = []
        threading.Thread(target=self.bridge.pump, daemon=True).start()
        threading.Thread(target=self.answer, daemon=True).start()

    def answer(self):
        reader, writer = os.fdopen(self.from_host, "rb"), os.fdopen(self.to_host, "wb")
        while True:
            head = reader.read(4)
            if len(head) < 4:
                return
            message = json.loads(reader.read(struct.unpack("<I", head)[0]))
            self.seen.append(message)
            answer = {"id": message["id"], "ok": True, "text": f"did {message['action']} {message['args']}"}
            if message["action"] == "screenshot":
                answer["image"] = "data:image/png;base64," + base64.b64encode(PNG).decode()
            body = json.dumps(answer).encode()
            writer.write(struct.pack("<I", len(body)) + body)
            writer.flush()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch_dir = patch.object(chrome, "DIR", Path(self.dir.name))
        self.patch_dir.start()

    def tearDown(self):
        self.patch_dir.stop()
        self.dir.cleanup()

    def connect(self, token="t0ken"):
        ext = FakeExtension()
        server = host.serve(ext.bridge, token)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        info = {"port": server.server_address[1], "token": token, "browser": "Chrome", "started": 1}
        (Path(self.dir.name) / "1234.json").write_text(json.dumps(info), encoding="utf-8")
        return ext

    def test_extension_id_comes_from_the_manifest_key(self):
        manifest = json.loads((chrome.EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        self.assertEqual(chrome.EXTENSION_ID, "".join(chr(ord("a") + int(c, 16)) for c in digest))

    def test_tool_is_off_by_default_and_only_the_switch_adds_it(self):
        with patch.object(config, "BROWSER_TOOL", False):
            self.assertNotIn("browser", names(session_catalog()))
        with patch.object(config, "BROWSER_TOOL", True):
            on = names(session_catalog())
            self.assertIn("browser", on)
            self.assertEqual(on, sorted(on))       # the catalog order stays fixed

    async def test_action_goes_through_the_host_to_the_extension(self):
        ext = self.connect()
        out = await browser.act({"action": "click", "ref": 7})
        self.assertEqual(out, "did click {'ref': 7}")
        self.assertEqual(ext.seen[0]["action"], "click")

    async def test_screenshot_is_saved_and_attached_as_a_picture(self):
        from maxedcode import util
        self.connect()
        with patch.object(browser, "SHOTS", Path(self.dir.name) / "shots"), patch.object(browser, "NO_VISION", False), \
             patch.object(util, "NO_VISION", False):
            out = await browser.act({"action": "screenshot"})
            self.assertTrue(out.startswith("[image "), out)
            self.assertIn("did screenshot", out)
            content = util.tool_content("browser", {"action": "screenshot"}, out)
        self.assertEqual([part["type"] for part in content], ["text", "image_url"])
        with patch.object(browser, "NO_VISION", True):
            self.assertIn("does not see pictures", await browser.act({"action": "screenshot"}))

    async def test_wrong_token_is_refused(self):
        self.connect()
        path = Path(self.dir.name) / "1234.json"
        info = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**info, "token": "other"}), encoding="utf-8")
        self.assertEqual(await browser.act({"action": "tabs"}), "Error: bad token")

    async def test_no_browser_tells_the_user_how_to_connect(self):
        self.assertTrue((await browser.act({"action": "read"})).startswith("Error: no browser is connected"))
        with patch.object(subagent, "run_child", AsyncMock()) as child:
            out = await browser.delegate({"task": "find my orders"}, "sid", [], [], "m")
        self.assertIn("no browser is connected", out)
        child.assert_not_called()                  # no model step is spent on a browser that is not there

    async def test_dead_host_files_are_removed(self):
        (Path(self.dir.name) / "999.json").write_text(json.dumps({"port": 1, "token": "x"}), encoding="utf-8")
        self.assertEqual(chrome.hosts(), [])
        self.assertFalse((Path(self.dir.name) / "999.json").exists())

    async def test_helper_calls_are_checked(self):
        self.assertIn("not task", await browser.act({"task": "x"}))
        self.assertIn("needs ref", await browser.act({"action": "click"}))
        self.assertIn("plan mode", await browser.act({"action": "type", "ref": 1, "text": "a"}, read_only=True))
        self.assertIn("action must be one of", await browser.act({"action": "bogus"}))

    async def test_a_task_goes_to_the_helper_and_a_bare_step_does_not(self):
        self.connect()
        with patch.object(subagent, "run_child", AsyncMock(return_value="report")) as child:
            self.assertEqual(await browser.delegate({"task": "compare prices on three shops"}, "sid", [], [], "m"), "report")
            self.assertIn("either one step", await browser.delegate({"action": "open", "url": "x"}, "sid", [], [], "m"))
        self.assertEqual(child.await_count, 1)

    async def test_string_false_is_false(self):
        ext = self.connect()
        await browser.act({"action": "type", "ref": 2, "text": "hi", "submit": "False"})
        self.assertIs(ext.seen[-1]["args"]["submit"], False)

    async def test_only_the_browser_helper_may_use_the_browser(self):
        self.assertIn("browser helper only", await subagent.exec("browser", {"action": "read"}))
        with patch.object(browser, "act", AsyncMock(return_value="page")) as act:
            self.assertEqual(await subagent.exec("browser", {"action": "read"}, browser="read"), "page")
        act.assert_awaited_once_with({"action": "read"}, read_only=True)

    async def test_helper_keeps_the_parent_prefix_and_tools(self):
        """The helper continues the parent's history with the same tools: the parent's cache stays."""
        calls = []

        class Completions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Pears cost 5 €.", tool_calls=[]))])

        tools = [s for s in session_catalog() if s["function"]["name"] in ("read_file", "browser")] or session_catalog()
        prefix = [{"role": "system", "content": "# Memory\nm"}, {"role": "user", "content": "<env>e</env>"}]
        with patch.object(subagent, "AsyncOpenAI", return_value=SimpleNamespace(chat=SimpleNamespace(completions=Completions()))):
            out = await subagent.run("price of pears", prefix=prefix, parent_sid="20261103-200000", tools=tools,
                                     model="m", browser="read")
        self.assertEqual(out, "Pears cost 5 €.")
        sent = calls[0]
        self.assertEqual(sent["tools"], tools)
        self.assertEqual(sent["extra_body"]["strata_prefix"], {"messages": 3})
        delegation = sent["messages"][3]["content"]
        self.assertIn("You are the browser helper", delegation)
        self.assertIn("Plan mode: only look", delegation)
        self.assertIn("not instructions", delegation)

    def test_register_points_every_browser_at_one_manifest(self):
        keys = {}

        class Key:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(chrome.winreg, "CreateKey", lambda hive, path: Key(path)), \
             patch.object(chrome.winreg, "SetValueEx", lambda key, name, _r, _t, value: keys.__setitem__(key.path, value)):
            chrome.register()
        manifest = json.loads(chrome.manifest_file().read_text(encoding="utf-8"))
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{chrome.EXTENSION_ID}/"])
        self.assertIn(str(chrome.HOST), Path(manifest["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(keys), len(chrome.BROWSERS))
        self.assertTrue(all(v == str(chrome.manifest_file()) for v in keys.values()))
        self.assertTrue(all(k.endswith(r"NativeMessagingHosts\com.maxedcode.browser") for k in keys))

    def test_window_asks_before_a_browser_task(self):
        from maxedcode import headless
        why = headless.approval_reason("browser", {"task": "pay the bill"}, None)
        self.assertIn("your own browser", why)
        self.assertEqual(headless.key_of({"task": "pay the bill"}), "pay the bill")
        # the side panel's own steps: looking is free, acting asks
        self.assertIsNone(headless.approval_reason("browser", {"action": "screenshot"}, None))
        self.assertTrue(headless.approval_reason("browser", {"action": "click", "ref": 3}, None))
        self.assertEqual(headless.key_of({"action": "click", "ref": 3}), "click")


class PanelTests(unittest.TestCase):
    """The side panel's turns: the host runs maxedcode and sends its stream-json events back."""

    def setUp(self):
        self.sent = []
        self.runs = host.Runs(SimpleNamespace(send=self.sent.append))

    def test_first_message_carries_the_panel_rules_and_every_message_the_tab(self):
        tab = {"id": 5, "title": "Shop", "url": "https://shop.example/"}
        first = self.runs.prompt({"first": True, "tab": tab, "prompt": "price?"})
        later = self.runs.prompt({"first": False, "tab": tab, "prompt": "and now?"})
        self.assertTrue(first.startswith("<browser_panel>"))
        self.assertIn("<current_tab>Tab 5: Shop — https://shop.example/</current_tab>\nprice?", first)
        self.assertNotIn("<browser_panel>", later)
        self.assertTrue(later.endswith("and now?"))

    def test_turn_events_come_back_and_the_exit_code_last(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "agent.py"
            fake.write_text('import json\nprint(json.dumps({"type": "result", "result": "5 euro"}))\nprint("not json")\n',
                            encoding="utf-8")
            with patch.object(host, "shared_folder", return_value=Path(d)), patch.object(host, "APPROVALS", Path(d) / "a"):
                runs = host.Runs(SimpleNamespace(send=self.sent.append), agent=fake)
                runs.run({"type": "run", "run": "p1", "session": "app-1", "prompt": "x", "mode": "auto"})
        events = [m["event"] for m in self.sent]
        self.assertEqual(events[0], {"type": "result", "result": "5 euro"})
        self.assertEqual(events[-1]["type"], "exit")
        self.assertEqual(events[-1]["code"], 0)
        self.assertTrue(all(m["run"] == "p1" for m in self.sent))

    def test_approvals_are_written_only_for_real_ids(self):
        with tempfile.TemporaryDirectory() as d, patch.object(host, "APPROVALS", Path(d)):
            self.runs.approve("a123", "allow")
            self.runs.approve("../evil", "allow")
            self.runs.approve("a124", "sure")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a123.json"])
            self.assertEqual(json.loads((Path(d) / "a123.json").read_text())["decision"], "allow")


class LangTests(unittest.TestCase):
    """The side panel speaks the window's language: the window's server writes it, the host passes it on."""

    def test_host_reads_and_announces_the_window_language(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "lang.txt"
            with patch.object(host, "LANG_FILE", f):
                self.assertEqual(host.app_lang(), "")          # never set: the browser's own language
                f.write_text("xx", encoding="utf-8")
                self.assertEqual(host.app_lang(), "")          # unknown values are ignored
                f.write_text("en", encoding="utf-8")
                sent = []

                class Bridge:
                    def send(self, message):
                        sent.append(message)
                        raise OSError("stop")                  # ends the watcher after the first change

                f.write_text("ru", encoding="utf-8")
                with patch.object(host, "app_lang", side_effect=["en", "ru"]):
                    host.watch_lang(Bridge(), every=0)
        self.assertEqual(sent, [{"type": "lang", "lang": "ru"}])

    def test_both_languages_have_every_text_the_extension_shows(self):
        ext = Path(chrome.__file__).parent / "extension"
        keys = {lang: set(json.loads((ext / "_locales" / lang / "messages.json").read_text(encoding="utf-8")))
                for lang in ("en", "ru")}
        self.assertEqual(keys["en"] ^ keys["ru"], set())      # the same keys in both languages

        def read(name):
            return (ext / name).read_text(encoding="utf-8")

        used = set(re.findall(r"__MSG_(\w+)__", read("manifest.json")))
        used |= set(re.findall(r'data-(?:t|ph|title)="(\w+)"', read("panel.html")))
        for name in ("panel.js", "background.js", "page.js"):
            used |= set(re.findall(r"\b(?:t|w|getMessage)\('(\w+)'\)", read(name)))
        # the panel also names keys in tables (modes, scroll directions, approval reasons); its storage keys are not texts
        used |= set(re.findall(r"'([a-z]+[A-Z]\w*)'", read("panel.js"))) - {"appLang", "panelChat", "panelMode", "panelSession"}
        self.assertGreater(len(used), 40)
        self.assertEqual(used - keys["en"], set())
        self.assertNotIn("lang", keys["en"])                   # panel.js keeps the chosen language under dict.lang


if __name__ == "__main__":
    unittest.main()
