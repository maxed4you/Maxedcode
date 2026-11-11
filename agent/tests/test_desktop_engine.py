"""The desktop window (maxedcode/webapp) and the headless pieces it relies on: --session, live deltas, call ids
on tool results, the --btw side question, Plan/Auto, the settings page, the saved-chat view.
Offline: the model is a fake client."""
import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from maxedcode import headless


def lines(buf):
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


class SessionFlagTests(unittest.TestCase):
    def test_safe_ids_pass_and_others_stop_the_run(self):
        self.assertEqual(headless.session_flag("main-session-1f2e_3"), "main-session-1f2e_3")
        self.assertIsNone(headless.session_flag(None))
        for bad in ("../x", "a b", "c:\\x", "x" * 101):
            with self.assertRaises(SystemExit):
                headless.session_flag(bad)

    def test_flags_read_session_and_btw(self):
        with patch.object(headless.sys, "argv", ["x", "--print", "--session", "s1", "--btw"]):
            f = headless.flags()
        self.assertEqual(f["session"], "s1")
        self.assertTrue(f["btw"])


class WindowModeTests(unittest.TestCase):
    def test_plan_and_auto_wrap_like_the_tui_with_a_notice_on_switch(self):
        with patch.object(headless, "WINDOW_MODE", "plan"), patch.object(headless.config, "THINK_LANG", ""):
            self.assertTrue(headless.plan_mode())
            self.assertEqual(headless.wrap_prompt("hi", []), '<user_input mode="plan">hi</user_input>')
            history = [{"role": "user", "content": '<user_input mode="act">go</user_input>'}]
            self.assertTrue(headless.wrap_prompt("hi", history).startswith("<mode_notice>Mode switched to plan."))
        with patch.object(headless, "WINDOW_MODE", "act"), patch.object(headless, "SETTINGS", {}), \
                patch.dict(headless.state.S, {"mode": "act"}), patch.object(headless.config, "THINK_LANG", ""):
            self.assertFalse(headless.plan_mode())
            # Auto is the plain text the user typed, as the window always sent it
            self.assertEqual(headless.wrap_prompt("hi", []), "hi")
            self.assertEqual(headless.wrap_prompt("hi", [{"role": "user", "content": "earlier"}]), "hi")
            # ...except the first message after Plan, which says the read-only rule is over
            after_plan = [{"role": "user", "content": '<user_input mode="plan">look</user_input>'}]
            self.assertEqual(headless.wrap_prompt("do it", after_plan),
                             '<mode_notice>Mode switched to act.</mode_notice>\n<user_input mode="act">do it</user_input>')
            after_switch = after_plan + [{"role": "user", "content": '<user_input mode="act">do it</user_input>'}]
            self.assertEqual(headless.wrap_prompt("next", after_switch), "next")

    def test_without_the_window_the_prompt_is_unchanged(self):
        with patch.object(headless, "WINDOW_MODE", None):
            self.assertEqual(headless.wrap_prompt("hi", []), "hi")


class AppSettingsTests(unittest.TestCase):
    """The window's settings page writes the CLI's own files: config.json, state.json, settings.json and the
    global MEMORY.md every session loads."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from maxedcode import appsettings, servers
        self.s = appsettings
        root = Path(tempfile.mkdtemp())
        self.user, self.cfg, self.state = root / "settings.json", root / "config.json", root / "state.json"
        self.memory = root / "memory" / "MEMORY.md"
        self.recent = root / "recent_servers.json"
        self.user.write_text('{"max_turns": 30, "model": "pinned"}', encoding="utf-8")
        self.state.write_text('{"effort": "medium", "sampling": {"top_k": 40}}', encoding="utf-8")
        for p in (patch.object(appsettings, "USER_SETTINGS", self.user), patch.object(appsettings, "CONFIG_FILE", self.cfg),
                  patch.object(appsettings, "MEMORY_FILE", self.memory),
                  patch.object(appsettings, "RECENT_FILE", self.recent),
                  patch.object(servers, "find_base", side_effect=lambda addr, key="", timeout=2: addr),
                  patch.object(appsettings.config, "STATE_FILE", self.state),
                  patch.object(appsettings, "server_models", return_value=[]),
                  patch.object(appsettings.config, "resolve_server", return_value={"ctx": 65536, "model": "local"})):
            p.start()
            self.addCleanup(p.stop)

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_each_field_goes_where_the_cli_keeps_it(self):
        self.s.apply({"temperature": "0.3", "effort": "high", "base_url": "http://h:1/v1", "model": "qwen",
                      "memory": "The user is not a programmer: answer in plain words."})
        self.assertEqual(self.read(self.state), {"effort": "high", "sampling": {"top_k": 40, "temperature": 0.3}})
        self.assertEqual(self.read(self.cfg), {"base_url": "http://h:1/v1"})
        # a value the user pinned in settings.json is edited there (it wins over config.json)
        self.assertEqual(self.read(self.user), {"max_turns": 30, "model": "qwen"})
        self.assertIn("plain words", self.memory.read_text(encoding="utf-8"))
        data = self.s.get()
        self.assertEqual((data["values"]["temperature"], data["values"]["model"]), (0.3, "qwen"))
        self.assertEqual(data["server"]["ctx"], 65536)

    def test_empty_model_means_auto_and_tuned_fields_are_not_editable(self):
        self.s.apply({"model": None})
        self.assertNotIn("model", self.read(self.user))
        with self.assertRaises(ValueError):
            self.s.apply({"max_turns": 10})
        # the rest of the CLI's /sampling goes where /sampling writes it: state.json
        self.s.apply({"top_k": 10, "top_p": 0.9, "min_p": 0.05})
        with self.assertRaises(ValueError):
            self.s.apply({"top_p": 3})

    def test_recent_servers_keep_the_last_three_before_the_current_one(self):
        self.user.write_text("{}", encoding="utf-8")
        for n in range(1, 6):
            self.s.apply({"base_url": f"http://h{n}:1/v1", "server": "lmstudio", "model": f"m{n}"})
        recent = self.s.get()["server"]["recent"]
        self.assertEqual([x["base_url"] for x in recent], ["http://h4:1/v1", "http://h3:1/v1", "http://h2:1/v1"])
        self.assertEqual((recent[0]["model"], recent[0]["label"], recent[0]["cloud"]), ("m4", "LM Studio", False))
        self.assertNotIn("api_key", self.recent.read_text(encoding="utf-8"))
        # switching back moves it to the front, without a copy
        self.s.apply({"base_url": "http://h2:1/v1", "model": "m2"})
        self.assertEqual([x["base_url"] for x in self.s.get()["server"]["recent"]],
                         ["http://h5:1/v1", "http://h4:1/v1", "http://h3:1/v1"])
        # a server set by the installer or the CLI (config.json) is kept when the window reads its settings
        self.cfg.write_text('{"base_url": "http://cli:8080/v1"}', encoding="utf-8")
        self.assertEqual(self.s.get()["server"]["recent"][0]["base_url"], "http://h2:1/v1")

    def test_a_bad_value_writes_nothing(self):
        with self.assertRaises(ValueError) as error:
            self.s.apply({"temperature": "5", "effort": "high", "nope": 1})
        self.assertIn("temperature", str(error.exception))
        self.assertIn("nope", str(error.exception))
        self.assertEqual(self.read(self.state)["effort"], "medium")
        self.assertFalse(self.cfg.exists())


class WebAppTests(unittest.TestCase):
    def test_saved_chat_shows_what_the_tui_shows(self):
        import tempfile
        from pathlib import Path
        from maxedcode.webapp import server
        f = Path(tempfile.mkdtemp()) / "s1.json"
        f.write_text(json.dumps({"id": "s1", "cwd": "C:\\w", "msgs": [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "<env>\nOS: Windows\n</env>"},
            {"role": "user", "content": '<user_input mode="act">list files</user_input>'},
            {"role": "assistant", "content": "", "reasoning_content": "think",
             "tool_calls": [{"id": "c1", "function": {"name": "glob", "arguments": '{"pattern": "*"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.md"},
            {"role": "assistant", "content": "One file."}]}), encoding="utf-8")
        with patch.object(server.util, "session_path", return_value=f):
            items = server.transcript("s1", "C:\\w")["items"]
        self.assertEqual([i["role"] for i in items], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(items[0]["text"], "list files")
        self.assertEqual((items[1]["reasoning"], items[1]["calls"][0]["name"]), ("think", "glob"))
        self.assertEqual((items[2]["id"], items[3]["text"]), ("c1", "One file."))

    def test_a_turn_outlives_its_page_and_replays_to_one_that_comes_back(self):
        import subprocess
        import sys
        from maxedcode.webapp import server
        code = ("import json; print(json.dumps({'type': 'delta', 'text': 'hi'})); print('noise'); "
                "print(json.dumps({'type': 'result', 'result': 'hi'}))")
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        proc.stdin.close()
        with server.RUNS_LOCK:
            run = server.RUNS["app-test"] = server.Run("app-test", "C:\\w", "hello", proc, {"items": []})
        page = run.follow(0)
        self.assertEqual(json.loads(next(page))["type"], "session")
        del page                                  # the page left mid-turn: the turn goes on
        back = [json.loads(x)["type"] for x in run.follow(0)]
        self.assertEqual(back, ["session", "delta", "result", "exit"])
        self.assertEqual([json.loads(x)["type"] for x in run.follow(2)], ["result", "exit"])
        self.assertNotIn("app-test", server.RUNS)  # ended: the saved chat has it now

    def test_archive_search_and_stat(self):
        import tempfile
        from pathlib import Path
        from maxedcode.webapp import server
        root = Path(tempfile.mkdtemp())
        with patch.object(server, "ARCHIVED", root / "archived.json"):
            server.archive("app-1", True)
            server.archive("app-2", True)
            server.archive("app-1", False)
            self.assertEqual(server.archived_ids(), {"app-2"})
        f = root / "a.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(server.stat(str(root))["dir"], True)
        self.assertIsNotNone(server.stat(str(f))["mtime"])
        self.assertIsNone(server.stat(str(root / "none"))["mtime"])
        # the window's chats only, one hit per chat
        import sqlite3
        from contextlib import closing
        db = root / "index.db"
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("CREATE VIRTUAL TABLE session_search USING fts5(session_id UNINDEXED, ts UNINDEXED, role UNINDEXED, text, "
                      "cwd_hash UNINDEXED, msg_no UNINDEXED, file_path UNINDEXED, tokenize='unicode61')")
            c.executemany("INSERT INTO session_search(session_id,ts,role,text,cwd_hash,msg_no,file_path) VALUES(?,?,?,?,?,?,?)",
                          [("app-1", 0, "user", "какой процессор стоит", "h", 1, ""), ("app-1", 0, "assistant", "процессор Ryzen", "h", 2, ""),
                           ("20261101-1", 0, "user", "процессор в терминале", "h", 1, "")])
        with patch.object(server.util, "SESSION_INDEX", db), \
                patch.object(server.util, "_index_db", lambda: closing(sqlite3.connect(db))):
            hits = server.search("процессор")
        self.assertEqual([h["id"] for h in hits], ["app-1"])
        self.assertIn("‹", hits[0]["snippet"])

    def test_window_chats_and_terminal_sessions_are_listed_apart(self):
        import tempfile
        from pathlib import Path
        from maxedcode import util
        root = Path(tempfile.mkdtemp())
        d = root / "abc"
        d.mkdir()
        for sid in ("20261103-101010", "app-20261103-101011", "session-1f2e"):
            (d / f"{sid}.json").write_text(json.dumps({"id": sid, "ts": 1, "cwd": "C:\\w", "msgs": [
                {"role": "user", "content": "hello " + sid}, {"role": "assistant", "content": "hi"}]}), encoding="utf-8")
        with patch.object(util, "SESS", root):
            cli = [x["id"] for x in util.list_sessions(all_cwds=True)]
            window = sorted(x["id"] for x in util.list_sessions(all_cwds=True, window=True))
        self.assertEqual(cli, ["20261103-101010"])
        self.assertEqual(window, ["app-20261103-101011", "session-1f2e"])

    def test_a_named_chat_keeps_its_name_through_headless_saves(self):
        import tempfile
        from pathlib import Path
        from maxedcode import util
        f = Path(tempfile.mkdtemp()) / "s.json"
        with patch.object(util, "session_file", return_value=f):
            util.save_session([{"role": "user", "content": "first question"}], "app-x", "My name", index=False)
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["title"], "My name")

    def test_session_ids_are_file_name_safe(self):
        from maxedcode.webapp import server
        self.assertTrue(server.valid_session("20261102-214346"))
        for bad in ("", "../x", "a b", "x" * 101, None):
            self.assertFalse(server.valid_session(bad))


class AppCliTests(unittest.TestCase):
    """Settings -> CLI commands: rules and MCP servers go to the CLI's own files; a skill typed as /name expands."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from maxedcode import appcli
        self.c = appcli
        root = Path(tempfile.mkdtemp())
        self.perms, self.mcp = root / "permissions.json", root / "mcp.json"
        for p in (patch.object(appcli, "PERMS_FILE", self.perms), patch.object(appcli, "MCP_FILE", self.mcp),
                  patch.object(appcli.permissions, "PERMS_FILE", self.perms)):
            p.start()
            self.addCleanup(p.stop)

    def test_rules_and_mcp_servers(self):
        self.c.apply({"do": "rule-add", "kind": "allow", "rule": "mcp__docs__search"})
        self.c.apply({"do": "rule-add", "kind": "deny", "rule": "powershell(Format-Volume *)"})
        self.assertEqual(json.loads(self.perms.read_text(encoding="utf-8")),
                         {"allow": ["mcp__docs__search"], "deny": ["powershell(Format-Volume *)"], "ask": []})
        self.assertTrue(any(r["rule"] == "mcp__docs__search" and r["own"] for r in self.c.rules()["allow"]))
        self.c.apply({"do": "rule-remove", "kind": "allow", "rule": "mcp__docs__search"})
        self.assertEqual(json.loads(self.perms.read_text(encoding="utf-8"))["allow"], [])
        self.c.apply({"do": "mcp-add", "name": "my docs", "command": 'npx -y "@x/docs" --port 3'})
        self.c.apply({"do": "mcp-add", "name": "web", "command": "https://mcp.example.com"})
        servers = json.loads(self.mcp.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(servers["my-docs"], {"command": "npx", "args": ["-y", "@x/docs", "--port", "3"]})
        self.assertEqual(servers["web"], {"url": "https://mcp.example.com"})
        self.c.apply({"do": "mcp-remove", "name": "web"})
        self.assertEqual([s["name"] for s in self.c.mcp_servers()], ["my-docs"])
        for bad in ({"do": "rule-add", "kind": "maybe", "rule": "x"}, {"do": "mcp-add", "name": "", "command": "x"}, {"do": "nope"}):
            with self.assertRaises(ValueError):
                self.c.apply(bad)

    def test_a_skill_expands_as_the_tui_sends_it(self):
        out = self.c.expand("debug", "the test fails")
        self.assertTrue(out["text"].startswith('<skill name="debug" loaded_by="user">'))
        self.assertIn("the test fails", out["text"])
        self.assertIn("error", self.c.expand("no-such-skill"))


class NoticeTests(unittest.TestCase):
    def test_a_notice_is_an_event_in_stream_json(self):
        buf, old = io.StringIO(), headless.FORMAT
        headless.FORMAT = "stream-json"
        try:
            with redirect_stdout(buf), patch.object(headless.sys, "stderr", io.StringIO()):
                headless.notice("cut", "(cut)")
        finally:
            headless.FORMAT = old
        self.assertEqual(lines(buf), [{"type": "notice", "kind": "cut", "text": "(cut)"}])


class LiveDeltaTests(unittest.TestCase):
    def setUp(self):
        self.old = headless.FORMAT
        headless.FORMAT = "stream-json"
        self.addCleanup(setattr, headless, "FORMAT", self.old)

    def test_only_the_new_part_is_sent_and_a_retry_resets(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            live = headless.live_delta("r1")
            live.restart()                      # first attempt: nothing shown yet, no reset
            live("th", "")
            live("think", "")
            live("think", "Hel")
            live("think", "Hello")
            live.restart()                      # the request failed and is sent again
            live("", "Hi")
        ev = lines(buf)
        self.assertEqual([e.get("reasoning") for e in ev[:2]], ["th", "ink"])
        self.assertEqual([e.get("text") for e in ev[2:4]], ["Hel", "lo"])
        self.assertTrue(ev[4]["reset"])
        self.assertEqual(ev[5]["text"], "Hi")

    def test_no_deltas_outside_stream_json(self):
        headless.FORMAT = "text"
        self.assertIsNone(headless.live_delta("r1"))


class FakeStream:
    def __init__(self, text):
        self.chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason="stop", delta=SimpleNamespace(content=text, tool_calls=None, model_extra={}))])]

    def __aiter__(self):
        async def gen():
            for c in self.chunks:
                yield c
        return gen()


class SideAnswerTests(unittest.TestCase):
    def test_btw_asks_after_the_history_and_saves_nothing(self):
        sent = {}

        async def create(**kw):
            sent.update(kw)
            return FakeStream("It is notes.md.")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        history = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "make notes.md"},
                   {"role": "assistant", "content": "done"}]
        buf = io.StringIO()
        old = headless.FORMAT
        headless.FORMAT = "stream-json"
        try:
            # a Strata server (the cache pin is checked): not whatever server this PC's config.json names
            with patch.object(headless, "save_session") as save, patch.object(headless, "current_tool_specs", return_value=[]), \
                    patch.object(headless.config, "SERVER_KIND", "strata"), redirect_stdout(buf):
                asyncio.run(headless.side_answer(client, history, "s1", "which file?"))
        finally:
            headless.FORMAT = old
        save.assert_not_called()
        self.assertEqual(sent["messages"][:3], history)
        self.assertIn("which file?", sent["messages"][-1]["content"])
        self.assertEqual(sent["extra_body"]["strata_prefix"], {"messages": 3})
        result = [e for e in lines(buf) if e["type"] == "result"][-1]
        self.assertEqual(result["result"], "It is notes.md.")
        self.assertTrue(result["btw"])


class CliParityTests(unittest.TestCase):
    """The TUI's prompt features in headless (the window and --print): @file attachments, /run, /init, skills."""

    def test_at_path_attaches_the_file_body_and_the_chat_shows_only_the_text(self):
        import tempfile
        from pathlib import Path
        from maxedcode import util
        from maxedcode.tools.fs import attach_files
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "note with space.txt"
            f.write_text("alpha\nbeta\n", encoding="utf-8")
            text = attach_files(f'look at @"{f}" and mail me@example')
            self.assertIn("<file_attachment", text)
            self.assertIn("beta", text)
            self.assertEqual(text.count("<file_attachment"), 1)          # "me@example" is not a file
            self.assertEqual(util.visible_user_text(text), f'look at @"{f}" and mail me@example')
        self.assertEqual(attach_files("no files here"), "no files here")

    def test_prompt_commands_expand_like_the_tui(self):
        self.assertEqual(headless.expand_command("/run"), headless.RUN_PROMPT)
        self.assertEqual(headless.expand_command("/run only the tests"), "only the tests")
        self.assertEqual(headless.expand_command("/init"), headless.INIT_PROMPT)
        self.assertEqual(headless.expand_command("fix /etc/hosts"), "fix /etc/hosts")
        with patch.object(headless.skills, "command_text", return_value=(None, None)), \
                patch.object(headless.skills, "skill", return_value=({"name": "review"}, "BODY x")):
            self.assertEqual(headless.expand_command("/skill review x"),
                             '<skill name="review" loaded_by="user">\nBODY x\n</skill>')
        with patch.object(headless.skills, "command_text", return_value=({}, "CMD")):
            self.assertEqual(headless.expand_command("/mine a b"), "CMD")
        with patch.object(headless.skills, "command_text", return_value=(None, None)), \
                patch.object(headless.skills, "skill", return_value=(None, None)):
            self.assertEqual(headless.expand_command("/unknown x"), "/unknown x")


if __name__ == "__main__":
    unittest.main()
