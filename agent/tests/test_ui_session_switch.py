"""Regression tests for persistence of an empty fresh session without importing Textual."""
import ast
import json
import re
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace, MethodType

APP_SOURCE = Path(__file__).resolve().parents[1] / "maxedcode" / "ui" / "app.py"
sys.path.insert(0, str(APP_SOURCE.parents[2]))
from maxedcode.util import close_open_calls, session_title, visible_user_text  # noqa: E402


def extract_method(name):
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
    func = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"suppress": suppress, "json": json, "time": time, "Path": Path, "re": re,
                 "session_file": None, "index_session": lambda *a, **k: None, "session_title": session_title,
                 "close_open_calls": close_open_calls}
    exec(compile(module, str(APP_SOURCE), "exec"), namespace)
    return namespace[name]


class EmptySessionPersistenceTests(unittest.TestCase):
    def test_saving_fresh_session_creates_a_loadable_json_file(self):
        save = extract_method("save_session")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_path = root / "fresh-session.json"
            save_globals = save.__globals__
            save_globals["session_file"] = lambda sid, cwd=None: session_path
            obj = SimpleNamespace(
                sid="fresh-session", title_=None, name_="", msgs=[{"role": "system", "content": "system"}],
                head=1, sess_path=None, sess_cwd=temp, session_cost=0.0, progress_w=None,
            )
            save.__get__(obj, type(obj))()
            payload = json.loads(session_path.read_text(encoding="utf-8")) if session_path.exists() else None
            self.assertIsNotNone(payload, "empty sessions must be persisted before switching away")
            self.assertEqual(payload["id"], "fresh-session")
            self.assertEqual(payload["msgs"], [])
            self.assertEqual(payload["title"], "(untitled)")

    def test_save_keeps_compacted_archive_draft_and_closes_open_tool_calls(self):
        save = extract_method("save_session")
        with tempfile.TemporaryDirectory() as temp:
            session_path = Path(temp) / "s.json"
            save.__globals__["session_file"] = lambda sid, cwd=None: session_path
            call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            obj = SimpleNamespace(
                sid="s", title_=None, name_="", head=1, sess_path=None, sess_cwd=temp, session_cost=0.0,
                progress_w=None, archive=[{"role": "user", "content": "first question"},
                                         {"role": "assistant", "content": "first answer"}],
                msgs=[{"role": "system", "content": "system"}, {"role": "user", "content": "go"},
                      {"role": "assistant", "content": "", "tool_calls": [call]}])
            obj._draft = lambda: "typed but not sent"
            save.__get__(obj, type(obj))()
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["archive"][1]["content"], "first answer")
            self.assertEqual(payload["draft"], "typed but not sent")
            self.assertEqual(payload["title"], "first question")
            self.assertEqual(payload["msgs"][-1]["role"], "tool")
            self.assertEqual(payload["msgs"][-1]["tool_call_id"], "c1")
            self.assertEqual(len(obj.msgs), 3, "saving must not change the live history")

    def test_save_keeps_messages_queued_during_work(self):
        save = extract_method("save_session")
        with tempfile.TemporaryDirectory() as temp:
            session_path = Path(temp) / "q.json"
            save.__globals__["session_file"] = lambda sid, cwd=None: session_path
            queue, steer = ["expanded next task"], []
            obj = SimpleNamespace(
                sid="q", title_="t", name_="", head=1, sess_path=None, sess_cwd=temp, session_cost=0.0,
                progress_w=None, msgs=[{"role": "system", "content": "system"}],
                _pending_input=[{"q": queue, "text": queue[0], "orig": "next task", "w": []},
                                {"q": steer, "text": "already read", "orig": "already read", "w": []}])
            save.__get__(obj, type(obj))()
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["queued"], ["next task"], "only unread messages, as the person typed them")
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["q.json"], "no temporary file left behind")

    def test_closing_a_tab_never_deletes_the_session(self):
        source = APP_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        close = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "close_session_tab")
        self.assertNotIn("delete_session", ast.get_source_segment(source, close))

    def test_close_open_calls_keeps_existing_results(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                {"role": "tool", "tool_call_id": "a", "content": "ok"},
                {"role": "user", "content": "next"}]
        out = close_open_calls(msgs)
        self.assertEqual([m.get("tool_call_id") for m in out if m["role"] == "tool"], ["a", "b"])
        self.assertEqual(out[-1]["content"], "next")
        self.assertEqual(close_open_calls(out), out)

    def test_interrupted_turn_is_saved_before_any_await_in_finally(self):
        # A second Esc cancels the task again inside finally: the save must come before every await.
        source = APP_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        turn = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "turn")
        outer = next(node for node in turn.body if isinstance(node, ast.Try) and node.finalbody)
        first_await = next((i for i, st in enumerate(outer.finalbody)
                            if any(isinstance(n, ast.Await) for n in ast.walk(st))), len(outer.finalbody))
        early = [ast.get_source_segment(source, st) for st in outer.finalbody[:first_await]]
        self.assertTrue(any("save_session" in s for s in early), early)

    def test_save_session_has_no_early_return_for_empty_history(self):
        source = APP_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        save = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "save_session")
        saved = ast.get_source_segment(source, save)
        self.assertNotIn("if len(self.msgs) <= self.head", saved)
        self.assertIn("tmp.replace(f)", saved)

    def test_resume_command_uses_the_safe_session_switch(self):
        source = APP_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        # command() is a wrapper (output header), the command branches live in _command()
        command = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_command")
        resume_branch = next(node for node in ast.walk(command)
                             if isinstance(node, ast.If) and ast.get_source_segment(source, node.test) == 'cmd == "/resume"')
        calls = [node.func.attr for node in ast.walk(resume_branch)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertIn("open_session_safely", calls)
        self.assertNotIn("load", calls)


class SessionTitleAndDeleteTests(unittest.TestCase):
    def test_title_skips_service_messages(self):
        msgs = [{"role": "user", "content": "<env>\nOS: Windows</env>"},
                {"role": "user", "content": "<repo_map budget=1>x</repo_map>"},
                {"role": "user", "content": "fix   the\ntests"}]
        self.assertEqual(session_title(msgs), "fix the tests")
        wrapped = [{"role": "user", "content": '<edit_strategy>Use apply_diff.</edit_strategy>\n'
                                               '<user_input mode="act" think_in="English">add tests</user_input>'}]
        self.assertEqual(session_title(wrapped), "add tests")

    def test_reopened_chat_shows_only_what_the_person_typed(self):
        self.assertEqual(visible_user_text('<mode_notice>Mode switched to act.</mode_notice>\n'
                                           '<user_input mode="act" think_in="English">fix it'
                                           '\n\nAttached file context:\n<file>secret body</file></user_input>'), "fix it")
        for service in ("<env>\nOS: Windows</env>", "<repo_map budget=1>x</repo_map>", "<pending_work>\n- a</pending_work>",
                        "<tool_validation_feedback>bad</tool_validation_feedback>", "<thinking_cut>x</thinking_cut>"):
            self.assertIsNone(visible_user_text(service), service)
        self.assertEqual(visible_user_text("steer: also check B"), "steer: also check B")
        self.assertEqual(visible_user_text('<direct_shell command="dir">\n<output>x</output></direct_shell>'), "! dir")
        self.assertEqual(visible_user_text([{"type": "text", "text": '<user_input mode="act">look</user_input>'},
                                            {"type": "image_url", "image_url": {"url": "data:"}}]), "look")

    def test_history_hides_empty_sessions(self):
        import maxedcode.util as util
        with tempfile.TemporaryDirectory() as temp:
            old = util.SESS
            util.SESS = Path(temp)
            try:
                d = Path(temp) / util.hash_cwd(temp)
                d.mkdir(parents=True)
                sessions = {
                    "empty": {"ts": 3, "msgs": [{"role": "user", "content": "<env>\nOS</env>"}]},
                    "real": {"ts": 2, "msgs": [{"role": "user", "content": '<user_input mode="act">hello</user_input>'}]},
                    "compacted": {"ts": 1, "msgs": [], "archive": [{"role": "user", "content": "old question"}]},
                    "queued_only": {"ts": 0, "msgs": [], "queued": ["never   sent\nyet"]},
                }
                for sid, body in sessions.items():
                    (d / f"{sid}.json").write_text(json.dumps({"id": sid, "cwd": temp, **body}), encoding="utf-8")
                found = {x["id"]: x["empty"] for x in util.list_sessions(temp)}
                self.assertEqual(found, {"empty": True, "real": False, "compacted": False, "queued_only": False})
                titles = {x["id"]: x["title"] for x in util.list_sessions(temp)}
                self.assertEqual(titles["queued_only"], "never sent yet")
                self.assertEqual(util.last_session(temp), "real")
            finally:
                util.SESS = old

    def test_delete_removes_children_and_keeps_other_projects(self):
        import maxedcode.util as util
        with tempfile.TemporaryDirectory() as temp:
            old = util.SESS, util.SESSION_INDEX
            util.SESS, util.SESSION_INDEX = Path(temp), Path(temp) / "index.db"
            try:
                a, b = Path(temp) / "aaa", Path(temp) / "bbb"
                a.mkdir(), b.mkdir()
                parent, other = a / "s1.json", b / "s1.json"
                child = a / "s2.json"
                for f, extra in ((parent, {}), (other, {}), (child, {"parent_sid": "s1"})):
                    f.write_text(json.dumps({"id": f.stem, "msgs": [], **extra}), encoding="utf-8")
                self.assertEqual(sorted(util.delete_session(parent)), ["s1", "s2"])
                self.assertFalse(parent.exists() or child.exists())
                self.assertTrue(other.exists())
            finally:
                util.SESS, util.SESSION_INDEX = old

    def test_key_actions_do_not_wait_for_dismiss_outside_a_worker(self):
        # Binding actions and message handlers are not workers: wait_for_dismiss=True raises NoActiveWorker.
        source = APP_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentApp")
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name.startswith("action_") or node.name.startswith("on_")):
                waits = [kw for call in ast.walk(node) if isinstance(call, ast.Call)
                         for kw in call.keywords if kw.arg == "wait_for_dismiss"]
                self.assertEqual(waits, [], node.name)


if __name__ == "__main__":
    unittest.main()
