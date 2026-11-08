"""Chat links: a bare address becomes a real link, and one click asks before the browser opens."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.markdown import Markdown  # noqa: E402
from textual import events  # noqa: E402
from textual.app import App  # noqa: E402

from maxedcode.ui import links  # noqa: E402
from maxedcode.ui.links import clean_url, linkify, unlinkify  # noqa: E402
from maxedcode.ui.screens import LinkScreen  # noqa: E402
from maxedcode.ui.widgets import SelStatic, _extract_text  # noqa: E402

ANSWER = ("Docs at [MDN](https://developer.mozilla.org/Web) and https://example.com/x?a=1 or google.com. "
          "Code stays plain: `https://in.code.com` and a path like util.py or bob@gmail.com. "
          "Not this one: [setup](file:///C:/Windows/System32/calc.exe) at all.")
WAIT = App.CLICK_CHAIN_TIME_THRESHOLD + 0.2   # the ask waits out the double-click time


class LinkifyTests(unittest.TestCase):
    def test_bare_addresses_get_a_link(self):
        out = linkify("see https://example.com/x?a=1 and google.com or www.example.com/page")
        self.assertIn("[https://example.com/x?a=1](<https://example.com/x?a=1>)", out)
        self.assertIn("[google.com](<https://google.com>)", out)
        self.assertIn("[www.example.com/page](<https://www.example.com/page>)", out)
        self.assertIn("[google.com](<https://google.com>).", linkify("go to google.com."))

    def test_existing_links_and_code_are_untouched(self):
        md = "[Google](https://google.com) `https://in.code.com` and <https://autolink.com/x>\n" \
             "```\nhttps://in.fence.com\n```"
        self.assertEqual(linkify(md), md)

    def test_fence_still_being_streamed_is_untouched(self):
        md = "Run this:\n```\ncurl https://x.com/api\n"
        self.assertEqual(linkify(md), md)

    def test_line_breaks_the_model_wrote_stay_and_copy_drops_them(self):
        import io
        from rich.console import Console
        from maxedcode.ui.links import chat_md
        md = ("Run these:\nGet-Process\ncd C:\\x\n\n```powershell\nGet-Date  \nGet-Host\n```\n\n"
              "| a | b |\n|---|---|\n| 1 | 2 |")
        buf = io.StringIO()
        Console(file=buf, width=60, color_system=None).print(Markdown(chat_md(md)))
        drawn = [line.strip() for line in buf.getvalue().splitlines()]
        self.assertIn("Get-Process", drawn)          # its own line, not "Run these: Get-Process cd C:\x"
        self.assertIn("cd C:\\x", drawn)
        self.assertIn("Get-Date", drawn)             # fenced code untouched
        self.assertEqual(unlinkify(chat_md(md)), md)

    def test_code_in_prose_is_not_an_address(self):
        for text in ("call logging.information here", "use self.app.notify", "example.community",
                     "run scripts\\install.sh", "see MANIFEST.in", "the self.app attribute"):
            self.assertEqual(linkify(text), text)

    def test_paths_and_emails_are_not_addresses(self):
        for text in ("util.py", "state.json", "docs/BEHAVIOR.md", "bob@gmail.com", "127.0.0.1:8080",
                     "C:\\Users\\alex\\x.txt", "run_all_tests.py"):
            self.assertIsNone(clean_url(text), text)

    def test_openable_address_only(self):
        self.assertEqual(clean_url("google.com"), "https://google.com")
        self.assertEqual(clean_url("https://example.com/a."), "https://example.com/a")
        self.assertEqual(clean_url("https://en.wikipedia.org/wiki/Foo_(bar)"), "https://en.wikipedia.org/wiki/Foo_(bar)")
        self.assertEqual(clean_url("https://x.com/a)"), "https://x.com/a")
        for bad in ("mailto:bob@gmail.com", "file:///C:/Windows/System32/calc.exe", "ms-settings:", "javascript:alert(1)"):
            self.assertIsNone(clean_url(bad), bad)

    def test_open_url_refuses_other_schemes(self):
        with mock.patch.object(links.webbrowser, "open", return_value=True) as browser:
            self.assertFalse(links.open_url("file:///C:/Windows/System32/calc.exe"))
            browser.assert_not_called()
            self.assertTrue(links.open_url("google.com"))
            browser.assert_called_once_with("https://google.com")

    def test_copy_gives_the_text_as_written(self):
        md = "see google.com and [MDN](https://developer.mozilla.org) `x.com`"
        self.assertEqual(unlinkify(linkify(md)), md)
        self.assertEqual(_extract_text(Markdown(linkify(md))), md)


class _Harness(App):
    """The same wiring the chat has: a click on an address asks the user, and only then opens."""
    def __init__(self):
        super().__init__()
        self.clicked, self.opened, self.clicks = [], [], 0

    def on_click(self, e):   # every click Textual delivered, link or not
        self.clicks += 1

    def compose(self):
        yield SelStatic(Markdown(linkify(ANSWER)), classes="ans")

    def on_sel_static_link_clicked(self, e):   # Textual names it after the nested message
        self.clicked.append(e.url)
        self.push_screen(LinkScreen(e.url), lambda ok: self.opened.append((e.url, ok)))


async def link_spot(app, needle):
    """(column, row) of the answer text where `needle` is drawn, so the click lands on it."""
    widget = app.query(SelStatic).first()
    for row, text in enumerate(widget._shown().splitlines()):  # _shown() is one joined string of drawn rows
        if needle in text:
            return text.index(needle) + 1, row
    raise AssertionError(f"{needle!r} is not drawn")


class ClickTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_asks_and_the_answer_opens_the_browser(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "google.com")
            await pilot.click(SelStatic, offset=(x, y))
            await pilot.pause(WAIT)
            self.assertEqual(app.clicked, ["https://google.com"])
            self.assertEqual(app.opened, [])
            self.assertIsInstance(app.screen, LinkScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.opened, [("https://google.com", True)])

    async def test_escape_leaves_the_address_untouched(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "https://example.com/x?a=1")
            await pilot.click(SelStatic, offset=(x, y))
            await pilot.pause(WAIT)
            self.assertEqual(app.clicked, ["https://example.com/x?a=1"])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.opened, [("https://example.com/x?a=1", False)])
            self.assertNotIsInstance(app.screen, LinkScreen)

    async def test_a_click_on_plain_text_does_nothing(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "Docs at")
            await pilot.click(SelStatic, offset=(x, y))
            await pilot.pause(WAIT)
            self.assertEqual(app.clicked, [])
            self.assertNotIsInstance(app.screen, LinkScreen)

    async def test_a_file_link_written_by_the_model_is_not_clickable(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "setup")
            await pilot.click(SelStatic, offset=(x, y))
            await pilot.pause(WAIT)
            self.assertEqual(app.clicked, [])
            self.assertNotIsInstance(app.screen, LinkScreen)

    async def test_double_click_selects_and_does_not_ask(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "google.com")
            await pilot.double_click(SelStatic, offset=(x, y))
            await pilot.pause(WAIT)
            self.assertEqual(app.clicked, [])
            self.assertNotIsInstance(app.screen, LinkScreen)

    async def test_drag_that_ends_on_an_address_does_not_ask(self):
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            x, y = await link_spot(app, "google.com")
            region = app.query(SelStatic).first().content_region
            # Pilot's mouse_up skips App.on_event, where Textual turns a MouseUp into a Click, so the
            # events go through the app the way the terminal driver sends them.
            for cls, (cx, cy) in ((events.MouseDown, (1, 0)), (events.MouseMove, (x, y)), (events.MouseUp, (x, y))):
                sx, sy = region.x + cx, region.y + cy
                app.post_message(cls(None, sx, sy, 0, 0, 1, False, False, False, screen_x=sx, screen_y=sy))
                await pilot.pause()
            await pilot.pause(WAIT)
            self.assertEqual(app.clicks, 1)   # Textual did send the click
            self.assertEqual(app.clicked, [])
            self.assertNotIsInstance(app.screen, LinkScreen)


if __name__ == "__main__":
    unittest.main()
