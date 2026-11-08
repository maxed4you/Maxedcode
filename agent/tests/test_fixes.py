"""Regression tests for context recovery and the prompt task indicator.

Run from the agent folder with: python -m unittest discover -s tests -v
The agent's normal dependencies (install.py) are required.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Keep test state isolated from the user's real Maxedcode sessions/settings.
_TEST_HOME = tempfile.TemporaryDirectory(prefix="maxedcode-tests-")
os.environ["AGENT_HOME"] = _TEST_HOME.name

from textual.app import App, ComposeResult
from maxedcode.ui.widgets import Prompt, TaskProgress


class _ProgressHarness(App):
    def compose(self) -> ComposeResult:
        yield TaskProgress(id="task-progress")


class TaskProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_generated_tasks_update_and_round_trip(self):
        app = _ProgressHarness()
        async with app.run_test():
            progress = app.query_one("#task-progress", TaskProgress)
            self.assertEqual(progress.snapshot(), [])
            self.assertFalse(progress.display)
            initial = [
                {"content": "Inspect the relevant files", "status": "in_progress"},
                {"content": "Implement the requested change", "status": "pending"},
                {"content": "Run focused regression tests", "status": "pending"},
            ]
            self.assertTrue(progress.set_tasks(initial))
            self.assertTrue(progress.display)
            self.assertEqual(progress.snapshot(), initial)
            updated = [
                {"content": "Inspect the relevant files", "status": "done"},
                {"content": "Implement the requested change", "status": "in_progress"},
                {"content": "Run focused regression tests", "status": "pending"},
            ]
            self.assertTrue(progress.set_tasks(updated))
            self.assertEqual(progress.snapshot(), updated)
            saved = progress.snapshot()
            progress.reset()
            self.assertEqual(progress.snapshot(), [])
            progress.restore(saved)
            self.assertEqual(progress.snapshot(), saved)

    async def test_invalid_task_list_is_not_shown(self):
        app = _ProgressHarness()
        async with app.run_test():
            progress = app.query_one("#task-progress", TaskProgress)
            # Two active steps in one language: validate() rejects this, so nothing is drawn.
            self.assertFalse(progress.set_tasks([{"content": "Проверить код", "status": "in_progress"},
                                                  {"content": "Запустить тесты", "status": "in_progress"}]))
            self.assertEqual(progress.snapshot(), [])
            self.assertFalse(progress.display)


class _PromptHarness(App):
    def compose(self) -> ComposeResult:
        yield Prompt()


class PromptPasteTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_multiline_paste_is_fully_present_in_prompt(self):
        app = _PromptHarness()
        payload = ("first line\n" + "x" * 220 + "\n") * 180
        async with app.run_test(size=(80, 24)):
            prompt = app.query_one(Prompt)
            inserted = prompt.insert_pasted_text(payload)
            self.assertEqual(inserted, len(payload))
            self.assertEqual(prompt.text, payload)
            self.assertGreater(prompt.document.line_count, 10)


class AgentStartupPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_mount_initializes_status_without_a_prior_user_turn(self):
        # Regression for the startup traceback: refresh_status() calls est_tokens(),
        # which calls tools(), before turn() has ever initialized the tool policy.
        from maxedcode.ui import app as app_module

        agent = app_module.AgentApp()
        # The catalog is part of the cached prefix from the first request, so it must already be
        # present at startup; only tool_choice is off until a prompt needs a tool call.
        self.assertTrue(agent._use_tool_schemas)
        self.assertFalse(agent._allow_tool_calls)
        self.assertTrue(agent.tools())

        session_start = {
            "event": "SessionStart", "decision": None, "input": None,
            "context": "", "message": "", "stop": False,
        }
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(120, 40)):
                # on_mount has completed, including the first refresh_status().
                self.assertIsNotNone(agent.query_one("#status"))
                self.assertIsNotNone(agent.query_one("#modes"))
                self.assertGreaterEqual(agent.est_tokens(), 0)


class BottomBarLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_bottom_bar_never_wraps_or_scrolls_the_screen(self):
        # Regression: the right-aligned hotkey hint was padded with spaces from App.size, came out a few
        # cells wider than #modes, wrapped, grew the bottom bar and made the whole screen scroll (ghost rows).
        import time
        from maxedcode.ui import app as app_module

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(253, 50)) as pilot:
                agent.show_home(False)
                agent.set_busy("Running: PowerShell…")
                agent.t0 = time.monotonic() - 66
                for width in (253, 121, 80, 60):
                    await pilot.resize_terminal(width, 50)
                    agent.refresh_status()
                    await pilot.pause()
                    self.assertEqual(agent.query_one("#modes").region.height, 1, width)
                    self.assertEqual(agent.query_one("#status").region.height, 2, width)
                    self.assertEqual(agent.screen.max_scroll_y, 0, width)
                    self.assertFalse([w for w in agent.screen.walk_children() if w.region.right > width], width)
                agent.set_busy(None)


class _SelectionHarness(App):
    def compose(self) -> ComposeResult:
        from rich.markdown import Markdown
        from textual.containers import VerticalScroll
        from maxedcode.ui.widgets import SelStatic
        with VerticalScroll():
            for _ in range(3):
                yield SelStatic(Markdown("Hello **world** and a second line\n\n```py\nx = 1\n```"), classes="ans")


class SelectionSpeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_drag_does_not_rerender_markdown(self):
        """A mouse drag changes only the highlight: the Markdown is rendered once, not on every move."""
        from textual.visual import RichVisual
        from maxedcode.ui.widgets import SelStatic

        calls = []
        real = RichVisual.render_strips

        def counted(self, *a, **k):
            calls.append(self)
            return real(self, *a, **k)

        app = _SelectionHarness()
        with patch.object(RichVisual, "render_strips", counted):
            async with app.run_test(size=(80, 40)) as pilot:
                await pilot.pause()
                first = app.query(SelStatic).first()
                calls.clear()
                await pilot.mouse_down(first, offset=(1, 0))
                for x in range(3, 15):
                    await pilot.hover(first, offset=(x, 0))
                await pilot.mouse_up(first, offset=(14, 0))
                self.assertEqual(app.screen.get_selected_text(), "ello world and")
                self.assertEqual(calls, [])   # every render reused the cached lines


class EscTakeBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_esc_returns_unanswered_input_and_mouse_up_copies(self):
        from rich.markdown import Markdown
        from maxedcode.ui import app as app_module
        from maxedcode.ui.widgets import SelStatic

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        answered = {"v": False}

        async def slow_stream(think_off=False, no_think=False):   # the server is still reading the prompt
            if answered["v"]:
                agent._retract["answered"] = True
            await asyncio.sleep(100)

        def users():
            return [w._sel_text for w in agent.chat.children if isinstance(w, SelStatic) and w.has_class("user")]

        copied = []
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(app_module, "to_clip", side_effect=copied.append), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)), \
             patch.object(agent, "stream_once", new=slow_stream), \
             patch.object(agent, "save_session"):
            async with agent.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                prompt = agent.query_one(Prompt)
                # mouse up copies at once: a click into the input right after used to drop the selection
                w = await agent.put(SelStatic(Markdown("Hello world this is an answer line"), classes="ans"))
                await pilot.pause()
                await pilot.mouse_down(w, offset=(2, 0))
                await pilot.hover(w, offset=(15, 0))
                await pilot.mouse_up(w, offset=(15, 0))
                await pilot.click(prompt)
                self.assertEqual(copied, ["llo world this"])
                prompt.text = "Йоу hello"   # the input has its own TextArea selection
                await pilot.pause()
                await pilot.mouse_down(prompt, offset=(0, 0))
                await pilot.hover(prompt, offset=(3, 0))
                await pilot.mouse_up(prompt, offset=(3, 0))
                self.assertEqual(copied[-1], "Йоу")
                self.assertFalse(prompt.highlight_cursor_line)

                async def until(cond, timeout=10):
                    """Wait for a state, not a fixed time: a slow CI runner needs longer than 0.2 s."""
                    for _ in range(int(timeout / 0.05)):
                        if cond():
                            return
                        await pilot.pause(0.05)

                n0, marks0 = len(agent.msgs), len(agent.marks)
                prompt.text = "first"
                await pilot.press("enter")
                await until(lambda: agent.busy and users() == ["❯ first"])
                prompt.text = "second"   # steer while the model works
                await pilot.press("enter")
                await until(lambda: list(agent.steer_queue))
                await pilot.press("escape")   # takes back only the unread steer, the turn keeps running
                await until(lambda: prompt.text == "second")
                self.assertEqual(prompt.text, "second")
                self.assertTrue(agent.busy)
                self.assertEqual(list(agent.steer_queue), [])
                self.assertEqual(users(), ["❯ first"])
                await pilot.press("escape")   # no answer yet: the turn is rolled back
                await until(lambda: agent.busy is None and prompt.text == "first\nsecond")
                self.assertEqual(prompt.text, "first\nsecond")
                self.assertIsNone(agent.busy)
                self.assertEqual(users(), [])
                self.assertEqual((len(agent.msgs), len(agent.marks)), (n0, marks0))

                answered["v"] = True   # the answer has started: Esc is a plain interrupt
                prompt.text = "third"
                await pilot.press("enter")
                await until(lambda: (getattr(agent, "_retract", None) or {}).get("answered"))   # the stream began
                await pilot.press("escape")
                await until(lambda: not agent.busy)
                self.assertEqual(prompt.text, "")
                self.assertEqual(users(), ["❯ third"])
                self.assertEqual(agent.msgs[-1]["role"], "user")


class ChatOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_is_shown_once_and_help_shows_live_values(self):
        import json
        from maxedcode import state as state_module
        from maxedcode.ui import app as app_module
        from maxedcode.ui.widgets import Card, SelStatic

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        with tempfile.TemporaryDirectory() as d, \
             patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)), \
             patch.object(agent, "save_session", lambda *a, **k: None), \
             patch.dict(state_module.S, {"auto": True}):
            target = Path(d) / "calc.py"
            target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            async with agent.run_test(size=(140, 60)) as pilot:
                await pilot.pause()
                args = {"path": str(target), "old_text": "    return a + b\n", "new_text": "    return b + a\n"}
                await agent.run_call({"id": "e1", "name": "edit_file", "args": json.dumps(args)})
                await pilot.pause()
                shown = [w._sel_text for w in agent.chat.children if isinstance(w, SelStatic)]
                self.assertEqual(sum("return b + a" in t for t in shown), 1, shown)   # the diff, once
                card = next(w for w in agent.chat.children if isinstance(w, Card))
                self.assertNotIn("now:", str(card.render()))   # model-facing "Lines 1-2 now:" stays hidden

                await agent.command("/help")
                await pilot.pause()
                help_text = agent.chat.children[-1]._sel_text
                self.assertIn(f"top_k {app_module.top_k_for(state_module.S['model'])}", help_text)
                self.assertIn(str(state_module.S["model"]), help_text)


class ModalScreensTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_dialog_opens_and_the_editor_round_trips(self):
        # CloudApprovalScreen used to crash on construction: self.task collides with Textual's MessagePump.task.
        from maxedcode.ui import app as app_module, screens

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        with patch.object(app_module.config, "resolve_server", return_value={}),              patch.object(agent.mcp, "cfg", return_value={}),              patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(140, 44)) as pilot:
                for screen in (screens.Pick("q?", ["Yes", "No"], False, "body"), screens.CloudSetupScreen(),
                               screens.CloudApprovalScreen("exact task text"), screens.HistoryScreen(agent.sid),
                               screens.EditorScreen("x"), screens.BtwScreen("why?")):
                    agent.push_screen(screen)
                    await pilot.pause(0.3)   # CloudSetupScreen reads the stored key on a cold start
                    self.assertIs(agent.screen, screen)
                    self.assertTrue(screen.query(".dialog"))
                    screen.dismiss(None)
                    await pilot.pause()
                prompt = agent.query_one(Prompt)
                prompt.text = "draft"
                await pilot.press("ctrl+g")
                await pilot.pause()
                self.assertEqual(agent.screen.query_one("#editor-area").text, "draft")
                agent.screen.query_one("#editor-area").insert(" more")
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(prompt.text, "draft more")


class DroppedFilesTests(unittest.TestCase):
    def test_dropped_paths_only_when_every_token_is_an_existing_path(self):
        from maxedcode.ui.widgets import dropped_paths
        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "a.py"
            spaced = Path(d) / "my dir" / "b c.png"
            spaced.parent.mkdir()
            plain.write_text("x", encoding="utf-8")
            spaced.write_bytes(b"png")
            self.assertEqual(dropped_paths(f'{plain} "{spaced}"'), [plain, spaced])
            self.assertEqual(dropped_paths(plain.as_uri()), [plain])
            self.assertIsNone(dropped_paths(f"look at {plain}"))      # ordinary text stays text
            self.assertIsNone(dropped_paths(str(Path(d) / "missing.py")))
            self.assertIsNone(dropped_paths("a.py"))                  # relative: not a drop


class CommandPaletteKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrows_move_list_while_search_keeps_focus(self):
        from maxedcode.ui import app as app_module
        from textual.widgets import OptionList

        agent = app_module.AgentApp()
        ran = []

        async def fake_submit(_event):
            ran.append(agent.query_one(Prompt).text)

        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)), \
             patch.object(agent, "on_prompt_submitted", fake_submit):
            async with agent.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+p")
                await pilot.pause()
                palette = agent.screen
                ol = palette.query_one("#palette-list", OptionList)
                current = lambda: palette.command_at(ol.highlighted)
                first = current()
                self.assertIsNotNone(first)   # group headers are never selected
                await pilot.press("down")
                second = current()
                await pilot.press("down", "up")
                self.assertEqual(current(), second)
                self.assertEqual(agent.focused.id, "palette-input")
                await pilot.press("up", "up")   # wraps around to the last command
                self.assertEqual(ol.highlighted, ol.option_count - 1)
                self.assertIsNotNone(current())
                for ch in "hist":
                    await pilot.press(ch)
                self.assertEqual(ol.get_option_at_index(0).id, "/history")   # name prefix ranks first
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(ran, ["/history"])
                await pilot.press("ctrl+p")
                await pilot.pause()
                for ch in "model":
                    await pilot.press(ch)
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(agent.query_one(Prompt).text, "/model ")
                self.assertEqual(ran, ["/history"])   # Tab inserts, does not run



class VSCodeKeysTests(unittest.IsolatedAsyncioTestCase):
    async def test_alt_keys_open_palette_and_editor(self):
        # VS Code's terminal keeps Ctrl+P / Ctrl+G for itself; Alt+P / Alt+G must reach the app.
        from maxedcode.ui import app as app_module, screens

        self.assertEqual(app_module.key_label("alt+p"), "Alt+P")
        self.assertEqual(app_module.key_label("ctrl+shift+c"), "Ctrl+Shift+C")
        self.assertIn(app_module.key_label(app_module.PALETTE_KEY), app_module.help_text().plain)
        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(120, 40)) as pilot:
                await pilot.press("alt+p")
                await pilot.pause()
                self.assertIsInstance(agent.screen, screens.CommandPalette)
                await pilot.press("escape")
                await pilot.pause()
                agent.query_one(Prompt).text = "draft"
                await pilot.press("alt+g")
                await pilot.pause()
                self.assertIsInstance(agent.screen, screens.EditorScreen)
                self.assertEqual(agent.query_one(Prompt).text, "draft")   # no stray "g" typed


class AskUserAutoModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_user_still_opens_question_in_auto_mode(self):
        import json
        from maxedcode.ui import app as app_module

        agent = app_module.AgentApp()
        previous_auto = app_module.S.get("auto")
        previous_mode = app_module.S.get("mode")
        agent.put = AsyncMock(return_value=SimpleNamespace(finish=Mock(), diff=None))
        agent.push_screen = AsyncMock(return_value="Option B")
        agent.set_busy = Mock()
        agent.note = AsyncMock()
        fake_hook = {"input": None, "message": "", "decision": None, "context": ""}
        try:
            app_module.S["auto"] = True
            app_module.S["mode"] = "act"
            with patch.object(app_module.hooks, "run", new=AsyncMock(return_value=fake_hook)):
                answer = await agent.run_call({
                    "id": "ask_1", "name": "ask_user",
                    "args": json.dumps({"question": "Which approach?", "options": ["A", "B"]}),
                })
            self.assertEqual(answer, "Option B")
            agent.push_screen.assert_awaited_once()
            self.assertFalse(agent.needs_approval("ask_user", {"question": "Which?"}))
        finally:
            app_module.S["auto"] = previous_auto
            app_module.S["mode"] = previous_mode


class CompactRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_uses_the_newly_compacted_messages(self):
        # Import lazily so the temporary AGENT_HOME is read before config loads.
        import openai
        from maxedcode.ui import app as app_module

        class FakeBadRequest(Exception):
            pass

        class EmptyStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class Completions:
            def __init__(self):
                self.requests = []

            async def create(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    raise FakeBadRequest("context length exceeded")
                return EmptyStream()

        agent = app_module.AgentApp()
        completions = Completions()
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        agent.msgs = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "oversized old history"},
        ]
        agent.head = 1

        async def compact(keep=None, auto=False):
            agent.msgs = [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "<compact>fresh compressed summary</compact>"},
            ]
            return True

        async def no_note(*args, **kwargs):
            return None

        async def no_paint(*args, **kwargs):
            return None

        agent.compact = compact
        agent.note = no_note
        agent.paint = no_paint
        original = openai.BadRequestError
        try:
            with patch.object(openai, "BadRequestError", FakeBadRequest):
                await agent.stream_once()
        finally:
            # Avoid leaking the patched exception type if the test runner reuses modules.
            openai.BadRequestError = original

        self.assertEqual(len(completions.requests), 2)
        self.assertIn("oversized old history", str(completions.requests[0]["messages"]))
        self.assertIn("fresh compressed summary", str(completions.requests[1]["messages"]))
        self.assertNotIn("oversized old history", str(completions.requests[1]["messages"]))


class ThinkLoopTests(unittest.IsolatedAsyncioTestCase):
    """A reasoning stuck on "Hmm, hmm." is stopped early, and the step is redone without thinking."""

    def agent_with(self, chunks):
        from maxedcode.ui import app as app_module

        class Stream:
            def __init__(self):
                self.sent, self.closed = 0, False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent >= len(chunks):
                    raise StopAsyncIteration
                self.sent += 1
                delta = SimpleNamespace(content=None, tool_calls=None, model_extra={"reasoning_content": chunks[self.sent - 1]})
                return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=None, delta=delta)])

            async def aclose(self):
                self.closed = True

        class Completions:
            def __init__(self):
                self.requests, self.streams = [], []

            async def create(self, **kwargs):
                self.requests.append(kwargs)
                self.streams.append(Stream())
                return self.streams[-1]

        async def no_paint(*args, **kwargs):
            return None, None, False

        agent = app_module.AgentApp()
        agent.completions = Completions()
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=agent.completions))
        agent.msgs = [{"role": "system", "content": "system"}, {"role": "user", "content": "decode GAME.TAP"}]
        agent.head = 1
        agent.paint = no_paint
        return agent

    async def test_loop_stops_the_stream_and_squashes_the_reasoning(self):
        agent = self.agent_with(["Block 3 holds sprites. "] + ["Hmm, hmm. "] * 2000)
        _content, reasoning, calls, _tw, _xw, cut = await agent.stream_once()
        stream = agent.completions.streams[0]
        self.assertTrue(agent._think_looped)
        self.assertTrue(stream.closed)
        self.assertLess(stream.sent, 100)   # not 2000 chunks until the thinking budget runs out
        self.assertLess(reasoning.lower().count("hmm"), 5)
        self.assertTrue(cut and not calls)

    async def test_no_think_step_keeps_the_prompt_and_thinks_briefly(self):
        # After a looped reasoning the step gets a short budget. Switching reasoning_effort to none (or
        # enable_thinking off) changed the system turn the template renders it into, and the server
        # re-read the whole conversation.
        from maxedcode import config
        agent = self.agent_with(["Normal reasoning about the header."])
        normal = dict(agent.extra())
        await agent.stream_once(no_think=True)
        body = agent.completions.requests[0]["extra_body"]
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(body.get("reasoning_effort"), normal.get("reasoning_effort"))
        self.assertEqual(body["reasoning_budget_tokens"], config.LOOP_RETRY_THINK)
        self.assertFalse(agent._think_looped)


class PlainRequestTests(unittest.IsolatedAsyncioTestCase):
    """tui_stream / subagent_stream off: one plain request per step, read by the same code as a stream."""

    def plain_response(self):
        call = SimpleNamespace(id="c1", type="function",
                               function=SimpleNamespace(name="read_file", arguments='{"path": "C:\\\\a.txt"}'))
        message = SimpleNamespace(content="", tool_calls=[call], reasoning_content="Read the file first.",
                                  model_extra={"reasoning_content": "Read the file first."})
        usage = SimpleNamespace(prompt_tokens=900, completion_tokens=40,
                                model_dump=lambda: {"prompt_tokens": 900, "completion_tokens": 40,
                                                    "prompt_tokens_details": {"cached_tokens": 850}})
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")], usage=usage)

    async def test_tui_sends_a_plain_request_and_reads_reasoning_calls_and_usage(self):
        from maxedcode import config
        from maxedcode.ui import app as app_module
        requests, response = [], self.plain_response()

        async def create(**kwargs):
            requests.append(kwargs)
            return response

        async def no_paint(*args, **kwargs):
            return None, None, False

        agent = app_module.AgentApp()
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent.msgs = [{"role": "system", "content": "system"}, {"role": "user", "content": "read a.txt"}]
        agent.head = 1
        agent.paint = no_paint
        with patch.object(config, "TUI_STREAM", False):
            _content, reasoning, calls, _tw, _xw, cut = await agent.stream_once()
        self.assertIs(requests[0]["stream"], False)
        self.assertNotIn("stream_options", requests[0])
        self.assertEqual(reasoning, "Read the file first.")
        self.assertEqual([c["name"] for c in calls], ["read_file"])
        self.assertFalse(cut)
        self.assertEqual(app_module.S["cached"], 850)

    async def test_tui_does_not_run_a_write_from_a_reply_cut_by_the_output_limit(self):
        from maxedcode import config
        from maxedcode.ui import app as app_module
        response = self.plain_response()
        call = response.choices[0].message.tool_calls[0]
        call.function = SimpleNamespace(name="write_file", arguments='{"path": "C:\\\\s.py", "content": "def f():\\n    re"}')
        response.choices[0].finish_reason = "length"

        async def create(**kwargs):
            return response

        async def no_paint(*args, **kwargs):
            return None, None, False

        agent = app_module.AgentApp()
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent.msgs = [{"role": "system", "content": "system"}, {"role": "user", "content": "write s.py"}]
        agent.head = 1
        agent.paint = no_paint
        with patch.object(config, "TUI_STREAM", False):
            _content, _reasoning, calls, _tw, _xw, _cut = await agent.stream_once()
        self.assertIn("cut off", calls[0].get("args_error", ""))

    async def test_subagent_sends_a_plain_request(self):
        from maxedcode import config, subagent
        requests, response = [], self.plain_response()

        async def create(**kwargs):
            requests.append(kwargs)
            return response

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(config, "SUBAGENT_STREAM", False):
            res, looped = await subagent._complete(client, {"model": "m", "messages": []})
        self.assertIs(res, response)
        self.assertFalse(looped)
        self.assertNotIn("stream", requests[0])


class SessionTabTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_message_survives_a_slow_session_open(self):
        """A message queued in another tab while its session opens slowly: the agent counts as busy (so the 0.5 s
        tick does not start show_tab, which cancelled the start), and a start cancelled anyway keeps the message."""
        from maxedcode.ui import app as app_module

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        opening = asyncio.Event()

        async def slow_open(sid):          # a slow PC: opening the session takes longer than one tick
            opening.set()
            await asyncio.sleep(100)

        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)), \
             patch.object(agent, "open_session_safely", new=slow_open), \
             patch.object(app_module.AgentApp, "_file_queue") as filed:
            async with agent.run_test(size=(120, 30)) as pilot:
                await pilot.pause()
                agent._session_queue = [("other-tab", "Add dark mode")]
                start = asyncio.ensure_future(agent.drain_session_queue())
                await asyncio.wait_for(opening.wait(), 5)
                self.assertEqual(agent._session_queue, [])
                self.assertTrue(agent._live_busy(), "a starting queued message must keep the tick away")
                start.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start
                self.assertEqual(agent._session_queue, [("other-tab", "Add dark mode")])
                self.assertFalse(agent._live_busy())
                filed.assert_called_with("other-tab", "Add dark mode", add=True)   # back in its session file

    async def test_previous_tab_keeps_its_title_after_new_session(self):
        from maxedcode.ui import app as app_module

        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}

        def labels():
            return {str(b.id)[len("session-tab-"):]: str(b.label) for b in agent.query("#session-tabs Button")
                    if str(b.id or "").startswith("session-tab-")}

        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                first = agent.sid
                self.assertIn("New session", labels()[first])   # the label cached while untitled
                # what a turn leaves behind: the first user message, then a save
                agent.msgs += [{"role": "user", "content": "Fix the login page"},
                               {"role": "assistant", "content": "ok"}]
                agent.save_session()
                await pilot.click("#new-session-tab")
                await pilot.pause()
                self.assertNotEqual(agent.sid, first)
                self.assertIn("Fix the login page", labels()[first])
                second = agent.sid
                await pilot.click(f"#session-tab-{first}")
                await pilot.pause()
                self.assertEqual(agent.sid, first)
                self.assertIn("Fix the login page", labels()[first])
                self.assertIn("New session", labels()[second])

    async def test_tabs_while_agent_works(self):
        """While a turn runs: a subagent tab is a read-only chat, a new tab queues its message until
        the running session fully finishes, idle tabs can be deleted, and the counter sees hidden tabs."""
        from maxedcode import state, subagent
        from maxedcode.ui import app as app_module

        state.OPEN_SESSIONS.clear()   # tabs left by other tests would push these off the strip
        state.CHILD_SESSIONS.clear()
        agent = app_module.AgentApp()
        session_start = {"event": "SessionStart", "decision": None, "input": None,
                         "context": "", "message": "", "stop": False}
        sent = []

        async def fake_turn(text, shown=None, retract=None):
            sent.append((agent.sid, text))

        task = "<delegation>\nrules\n</delegation>\n\nTask:\nFind the tab strip"
        child = [{"role": "user", "content": task},
                 {"role": "assistant", "content": "Searching.", "reasoning_content": "Look for the tab strip first.",
                  "tool_calls": [
                     {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": '{"pattern": "tabs"}'}}]}]
        with patch.object(app_module.config, "resolve_server", return_value={}), \
             patch.object(agent.mcp, "cfg", return_value={}), \
             patch.object(app_module.hooks, "run", new=AsyncMock(return_value=session_start)):
            async with agent.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                agent.turn = fake_turn
                live = agent.sid
                agent.msgs += [{"role": "user", "content": "Old idle work"}, {"role": "assistant", "content": "ok"}]
                agent.save_session()
                await agent.action_new_session()
                idle = live
                live = agent.sid
                agent.set_busy("Running: subagent…")
                subagent._persist_child_session("agent-t1", "Agent 1: Find the tab strip", live, "Find",
                                                msgs=child, status="running", step=1, activity="grep", index=1)
                await agent.refresh_session_tabs(force=True)
                await pilot.pause(0.2)   # let the freshly mounted tabs settle before clicking one
                await pilot.click("#session-tab-agent-t1")
                await pilot.pause()
                self.assertEqual((agent.sid, agent.view_sid), (live, "agent-t1"))
                peek = agent.query_one("#peek")
                text = "\n".join(getattr(w, "_sel_text", "") for w in peek.children)
                self.assertIn("task from the parent agent", text)
                self.assertIn("Find the tab strip", text)
                self.assertNotIn("<delegation>", text)
                self.assertTrue(any(w.has_class("sub") for w in peek.children))
                # the subagent's reasoning is shown (folded), and the reply it is writing now, live
                folds = [w for w in peek.children if isinstance(w, app_module.Collapsible)]
                self.assertTrue(any("Reasoning" in str(w.title) for w in folds))
                state.upsert_child_session("agent-t1", live_think="Now reading app.py", live_text="")
                await agent.refresh_peek()
                self.assertIn("Now reading app.py", str(agent._peek_live.inner.render()))
                self.assertIs(list(peek.children)[-1], agent._peek_live)
                # the input takes /btw (a side question to this subagent); anything else is refused
                asked = []

                async def fake_child_btw(sid, q):
                    asked.append((sid, q))

                agent.child_btw = fake_child_btw
                inp = agent.query_one("#inp")
                self.assertFalse(inp.read_only)
                inp.text = "keep going"
                await pilot.press("enter")
                await pilot.pause()
                inp.text = "/btw what file are you reading?"
                await agent.on_prompt_submitted(None)
                await pilot.pause()
                self.assertEqual(asked, [("agent-t1", "what file are you reading?")])
                self.assertEqual(agent._session_queue, [])
                await pilot.press("escape")   # leaves the view, does not interrupt the agent
                await pilot.pause()
                self.assertIsNone(agent.view_sid)
                self.assertTrue(agent.busy)

                await pilot.press("ctrl+n")
                await pilot.pause()
                queued_sid = agent.view_sid
                self.assertTrue(queued_sid and queued_sid != live)
                agent.query_one("#inp").text = "Add dark mode"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(agent._session_queue, [(queued_sid, "Add dark mode")])
                self.assertEqual(sent, [])
                # the queued message is on disk too: a closed CLI must not lose it
                saved = json.loads(app_module.session_path(queued_sid).read_text(encoding="utf-8"))
                self.assertEqual(saved["queued"], ["Add dark mode"])

                idle_file = app_module.session_path(idle)
                await pilot.click(f"#close-session-{idle}")   # an idle tab closes while the agent works
                await pilot.pause()
                self.assertNotIn(idle, state.OPEN_SESSIONS)
                self.assertTrue(idle_file.exists(), "closing a tab must keep the session on disk")
                self.assertTrue(agent.query_one(f"#close-session-{live}").disabled)

                await pilot.resize_terminal(60, 30)
                await pilot.pause(0.3)
                self.assertIn("+", str(agent.query_one("#tab-count").label))   # hidden tabs are counted
                await pilot.resize_terminal(160, 40)

                await pilot.pause()
                agent.set_busy(None)
                await agent.peek_tick()
                # the queued tab opens, then its message is sent (peek_tick retries every 0.5 s); up to 15 s,
                # because a slow CI runner needed more than 3
                for _ in range(150):
                    if sent and agent.view_sid is None:
                        break
                    await pilot.pause(0.1)
                errors = app_module.HOME / "ui_errors.log"
                why = (f"busy={agent.busy!r} queue={agent.queue!r} draining={agent._draining_queue!r} "
                       f"worker={agent.worker and agent.worker.state} session_queue={agent._session_queue!r} "
                       f"input={agent.query_one('#inp').text!r}" + chr(10)
                       + (errors.read_text(encoding="utf-8")[-3000:] if errors.exists() else "no ui_errors.log"))
                self.assertEqual(sent, [(queued_sid, "Add dark mode")], why)   # a failure says what held it
                self.assertEqual((agent.sid, agent.view_sid), (queued_sid, None))
                # sent once: not also restored into the input from the session file
                self.assertNotIn("Add dark mode", agent.query_one("#inp").text)
                saved = json.loads(app_module.session_path(queued_sid).read_text(encoding="utf-8"))
                self.assertEqual(saved.get("queued"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
