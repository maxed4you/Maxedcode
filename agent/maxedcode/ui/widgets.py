"""Widgets and rendering (selection, tool card, prompt, chat)."""
from rich.cells import cell_len
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text
from textual.containers import VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.events import Paste
from textual.strip import Strip
from textual.style import Style
from textual.visual import RichVisual
from textual.visual import Visual
from textual.widgets import Collapsible
from textual.widgets import Static
from textual.widgets import TextArea
from textual.widgets import OptionList
import json
import os
import re
from ..state import TITLES
from ..tools.fs import diff_text, prev_content
from ..tools.todo import MARK, copy_tasks, validate as validate_todos
from ..util import shift_down, strip_ansi
from .links import clean_url, unlinkify

def _extract_text(r):
    """Plain text from any renderable (Panel, Group, Markdown, …)."""
    if r is None:
        return ""
    if isinstance(r, str):
        return r
    if isinstance(r, (Text, Content)):
        return r.plain
    if isinstance(r, Group):
        parts = []
        for item in r._renderables:
            t = _extract_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts)
    if isinstance(r, Panel):
        inner = getattr(r, "_renderable", None)
        return _extract_text(inner) if inner else ""
    if isinstance(r, Markdown):
        # Copied as the agent wrote it: without the [address](<address>) wrapping the chat adds.
        return unlinkify(getattr(r, "markup", "") or getattr(r, "_source", "") or "")
    try:
        return r.plain
    except AttributeError:
        return ""
_BOX = ("╭", "╰")
def _render_plain(r, width):
    """Text exactly as it is drawn on the screen (wrapped to the width), for partial mouse selection."""
    import io
    from rich.console import Console
    if isinstance(r, Content):
        return r.plain
    if isinstance(r, str):
        r = Text(r)
    c = Console(width=max(10, width or 80), file=io.StringIO(), force_terminal=False, color_system=None)
    with c.capture() as cap:
        c.print(r, end="")
    return "\n".join(l.rstrip() for l in cap.get().split("\n"))
def _unframe(text):
    """Removes the command card frames (╭─╮ │ │ ╰─╯) and the result indentation, keeping only the text."""
    out = []
    for l in text.split("\n"):
        s = l.lstrip()
        if s[:1] in _BOX:
            continue
        if s.startswith("│"):
            l = s[1:]
            l = l[1:] if l.startswith(" ") else l
            l = re.sub(r"\s*│\s*$", "", l)
        elif l.startswith("  "):
            l = l[2:]
        out.append(l.rstrip())
    return "\n".join(out)
class SelVisual(Visual):
    """Wrapper over the Rich visual (Markdown, Panel, Group). Rich knows nothing about selection:
    it does not draw highlighting and does not provide offsets inside the text, so such content
    was selected "as a whole" and there was no highlight at all. The wrapper gives every character a meta offset
    (from it the screen understands what exactly was selected with the mouse) and the highlight style."""

    def __init__(self, widget, visual):
        self._widget, self._visual = widget, visual

    def get_height(self, rules, width):
        return self._visual.get_height(rules, width)

    def get_optimal_width(self, rules, container_width):
        return self._visual.get_optimal_width(rules, container_width)

    def _base(self, width, height, style, options):
        """Rendered lines with offsets, cached on the widget: a mouse drag changes only the selection,
        so re-rendering Markdown and rebuilding every style on each mouse move made the UI lag."""
        key = (width, height, style)
        cache = getattr(self._widget, "_sel_cache", None)
        if cache is not None and cache[0] is self._visual and cache[1] == key:
            return cache[2]
        lines = []
        for y, strip in enumerate(self._visual.render_strips(width, height, style, options)):
            segs, x = [], 0
            for seg in strip._segments:
                segs.append((seg.text, Style.from_rich_style(seg.style) if seg.style is not None else Style(),
                             x, seg.control, seg.cell_length))
                x += seg.cell_length
            plain = Strip([Segment(t, b.rich_style_with_offset(sx, y), c) for t, b, sx, c, _ in segs],
                          strip.cell_length)
            lines.append((segs, plain))
        self._widget._sel_cache = (self._visual, key, lines)
        return lines

    def render_strips(self, width, height, style, options):
        sel, sel_style = options.selection, options.selection_style
        out = []
        for y, (segs, plain) in enumerate(self._base(width, height, style, options)):
            span = sel.get_span(y) if sel is not None else None
            if span is None:
                out.append(plain)
                continue
            lo, hi = span[0], plain.cell_length if span[1] == -1 else span[1]
            new = []
            for text, base, x, control, w in segs:
                if x + w <= lo or x >= hi:
                    new.append(Segment(text, base.rich_style_with_offset(x, y), control))
                    continue
                # Split the segment at the selection edges only (not per character).
                parts, cur, cx, inside = [], "", x, lo <= x < hi
                for ch in text:
                    now = lo <= cx < hi
                    if now != inside and cur:
                        parts.append((cur, inside, cx - cell_len(cur)))
                        cur = ""
                    cur, inside = cur + ch, now
                    cx += cell_len(ch)
                if cur:
                    parts.append((cur, inside, cx - cell_len(cur)))
                for t, on, px in parts:
                    new.append(Segment(t, (base + sel_style if on else base).rich_style_with_offset(px, y), control))
            out.append(Strip(new, plain.cell_length))
        return out
class SelStatic(Static):
    """A Static that has "clean" text (_sel_text): without frames or markup. A fully selected
    widget (double/triple click, /copy) returns this text; partial mouse selection takes what is drawn.
    An address the agent wrote (a link style) is clickable: the app asks before opening it."""

    class LinkClicked(Message):
        """Bubbled to the app: the user clicked an address, opening it needs confirmation."""
        def __init__(self, url):
            self.url = url
            super().__init__()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        first = args[0] if args else kwargs.get("content")
        self._src = first
        self._sel_text = _extract_text(first) if first is not None else ""

    def update(self, content=""):
        super().update(content)
        self._src = content
        self._sel_text = _extract_text(content)
        self._sel_cache = self._shown_cache = None   # content may be the same object, changed in place

    def render(self):
        v = self.visual
        return SelVisual(self, v) if isinstance(v, RichVisual) else v

    def selection_updated(self, selection):
        """The screen calls this for every selected widget on each mouse move; repaint only when
        this widget's own part of the selection has changed."""
        if selection != getattr(self, "_last_sel", None):
            self._last_sel = selection
            super().selection_updated(selection)

    def _shown(self):
        """_render_plain for the current content and width, cached (the clipboard timer asks often)."""
        key = (self._src, self.size.width)
        cache = getattr(self, "_shown_cache", None)
        if cache is None or cache[0] is not key[0] or cache[1] != key[1]:
            cache = self._shown_cache = (key[0], key[1], _render_plain(key[0], key[1]))
        return cache[2]

    def get_selection(self, selection):
        if any(isinstance(a, Collapsible) and a.collapsed for a in self.ancestors):
            return None  # do not copy collapsed reasoning: only what is visible gets copied
        if not self._sel_text:
            return super().get_selection(selection)
        if selection.start is None and selection.end is None:
            return self._sel_text, "\n\n"
        shown = self._shown() if self._src is not None else self._sel_text
        try:
            part = selection.extract(shown)
        except TypeError:
            part = selection.extract(shown, "\n")
        return (_unframe(part) if self.has_class("card") else part), "\n"

    def on_mouse_down(self, event):
        """Where the button went down: Textual also sends a click after a drag that ends on this widget."""
        self._down_at = event.screen_offset

    def on_click(self, event):
        """A plain single click on an address asks the app. A drag selects text and a double click
        selects a word, so neither opens anything: the ask waits out the double-click time first."""
        timer, self._link_timer = getattr(self, "_link_timer", None), None
        if timer is not None:
            timer.stop()   # the second click of a double click
        if (event.button != 1 or event.chain != 1 or event.ctrl or event.meta
                or event.screen_offset != getattr(self, "_down_at", None)):
            return
        style = event.style
        if style is not None and not isinstance(style, Style):
            style = Style.from_rich_style(style)
        # Only http(s)/ftp: a [text](file:///x.exe) written by the model must not reach os.startfile.
        url = clean_url(style.link) if style is not None and style.link else None
        if url:
            self._link_timer = self.set_timer(self.app.CLICK_CHAIN_TIME_THRESHOLD,
                                              lambda: self.post_message(self.LinkClicked(url)))

class Chrome(Static):
    """Service lines (status, "copied", spinner): they must not be selected or copied."""
    ALLOW_SELECT = False

    def get_selection(self, selection):
        return None  # "select all" skips ALLOW_SELECT, so we disable it here too
EDIT_ISSUE = re.compile(r"error|warning|failed|invalid|\bF\d{3}\b|\bE\d{3}\b|lint:", re.I)
EDIT_FINE = re.compile(r"ast\.parse: OK|lint: (skipped|OK|clean)", re.I)


def short_path(path):
    """Path for the card header: relative to the current folder, otherwise "…\\folder\\file"."""
    import os
    from pathlib import Path
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path(os.getcwd()).resolve()))
    except (ValueError, OSError):
        parts = p.parts
        return str(p) if len(parts) <= 3 else "…" + os.sep + os.sep.join(parts[-2:])


def color_unified_diff(path, diff, limit=60):
    """apply_diff: the same look as other edits — a "path +N −M" header and colored lines."""
    lines = diff.replace("\r\n", "\n").splitlines()
    add = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    t = Text()
    t.append(f"{path}  ", style="bold")
    t.append(f"+{add}", style="#7ec69a")
    t.append(f" −{rem}\n", style="#e06c75")
    for l in lines[:limit]:
        if l.startswith(("+++", "---")):
            continue
        style = ("#7ec69a on #16271c" if l.startswith("+") else "#e06c75 on #2a1619" if l.startswith("-")
                 else "grey50" if l.startswith("@@") else "#9aa0a8")
        t.append(l + "\n", style=style)
    if len(lines) > limit:
        t.append(f"… {len(lines) - limit} more lines\n", style="grey50")
    return t


CARD_LIMIT = 120          # diff lines drawn in an edit card; the rest is "… N more lines"
PLACE_LIMIT = 300_000     # a bigger file is not diffed whole just to draw the card


def edit_blocks(name, a):
    """(old_text, new_text) pairs an edit_file / patch call asks for; None when the arguments
    hold no usable block (a malformed or cut call keeps the plain JSON view)."""
    raw = [a] if name == "edit_file" else a.get("blocks") if isinstance(a.get("blocks"), list) else []
    pairs = [(b["old_text"].replace("\r\n", "\n"), b["new_text"].replace("\r\n", "\n")) for b in raw
             if isinstance(b, dict) and isinstance(b.get("old_text"), str) and isinstance(b.get("new_text"), str)]
    return pairs if pairs and len(pairs) == len(raw) else None


def apply_blocks(text, pairs, reverse=False):
    """Text with the blocks applied, or None when a block is not found exactly once.
    reverse=True undoes them: the card of an edit already on disk (/history, a subagent tab)."""
    for old, new in (reversed(pairs) if reverse else pairs):
        if reverse:
            old, new = new, old
        if not old or text.count(old) != 1:
            return None
        text = text.replace(old, new, 1)
    return text


def blocks_diff(path, pairs, limit=CARD_LIMIT):
    """The change as the call states it, block by block: used when it cannot be placed in the file
    (the file changed since). Real lines with -/+, no file line numbers."""
    import difflib
    lines = []
    for old, new in pairs:
        for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""):
            if l.startswith(("---", "+++")):
                continue
            if l.startswith("@@"):
                if lines:
                    lines.append("@@ …")
                continue
            lines.append(l)
    return color_unified_diff(path, "\n".join(lines), limit)


def edit_diff(name, a, shown):
    """Diff an edit card draws from the call alone (display only, nothing reaches the model):
    placed in the file when the blocks match it before or after the edit, so line numbers are real."""
    if name == "apply_diff":
        return color_unified_diff(shown, str(a.get("diff") or ""), CARD_LIMIT)
    if name == "write_file":
        old, new = prev_content(a["path"]), str(a.get("content", ""))
        # already written (/history, a subagent tab): the content itself, as a new file
        return diff_text(shown, "" if old == new else old, new, limit=CARD_LIMIT)
    pairs = edit_blocks(name, a)
    if pairs is None:
        return None
    text = prev_content(a["path"])
    if len(text) <= PLACE_LIMIT:
        new = apply_blocks(text, pairs)
        if new is not None:
            return diff_text(shown, text, new, limit=CARD_LIMIT)
        old = apply_blocks(text, pairs, reverse=True)
        if old is not None:
            return diff_text(shown, old, text, limit=CARD_LIMIT)
    return blocks_diff(shown, pairs)


class Card(SelStatic):
    """Tool call card: ● running, ✓ done, ✗ error/rejected."""

    def __init__(self, name, args):
        super().__init__(classes="card")
        self.tool, self.args, self.st, self.res, self.diff, self.summary = name, args, "●", None, None, None
        self.max_res = 12   # tool response lines under the card
        a = args
        # Only render a file diff when the model supplied a usable path. A malformed
        # write_file call must not look like a successful "+N lines" write to "".
        has_path = isinstance(a.get("path"), str) and bool(a.get("path", "").strip())
        if has_path and name in ("apply_diff", "write_file", "patch", "edit_file"):
            try:
                self.diff = edit_diff(name, a, short_path(a["path"]))
            except Exception:   # odd arguments: the plain JSON view below, never a crash
                self.diff = None
        self.draw()

    def draw(self):
        a = self.args
        # A tool that changed a file shows the full diff (line numbers, word-level highlight),
        # not just the "+N −M" header.
        if self.diff is not None:
            body, full = self.diff.copy(), str(self.diff)
            body.rstrip()   # no empty line at the bottom of the border
        else:
            body = self.summary if self.summary is not None else (a.get("command") or a.get("query") or a.get("url")
                                                                  or a.get("question") or json.dumps(a, ensure_ascii=False))
            full = strip_ansi(str(body))
            body = "\n".join(full.splitlines()[:10])
            body = Text(body)
        col = {"●": "#d97757", "✓": "#7ec69a", "✗": "#e06c75"}[self.st]
        title = TITLES.get(self.tool, self.tool.replace("mcp__", "MCP: ").replace("__", " › "))
        parts = [Panel(body, title=f"[{col}]{self.st}[/] {title}", title_align="left",
                       border_style="grey35" if self.st == "✓" else col)]
        res = self.res
        if res and self.diff is not None and self.st == "✓":
            # the edit is visible in the diff; the tool response ("OK, edited … Lines 1-6 now: …") is for the model.
            # The user only needs the code check remarks from it.
            res = "\n".join(l for l in res.splitlines() if EDIT_ISSUE.search(l) and not EDIT_FINE.search(l))
        if res:
            lines = res.splitlines()
            limit = self.max_res
            more = f"\n… {len(lines) - limit} more lines" if len(lines) > limit else ""
            parts.append(Text("  " + "\n  ".join(lines[:limit]) + more, style="grey62"))
        self.update(Group(*parts))
        self._sel_text = f"[{title}] {full}" + (f"\n{self.res}" if self.res else "")

    def finish(self, ok, res=None):
        # Raw escape codes go straight to the terminal past Textual and break the frame and the scrollbar.
        self.st, self.res = ("✓" if ok else "✗"), strip_ansi(res) if isinstance(res, str) else res
        self.draw()

    def disk_change(self, before, after):
        """Replace the diff predicted from the call arguments with what really changed on disk
        (real line numbers). Returns False when the file is byte-for-byte the same as before."""
        if before is None or after is None or not isinstance(self.args.get("path"), str):
            return True
        shown = short_path(self.args["path"])
        if before == after:
            self.summary = self.diff = Text(f"{shown}  no changes on disk", style="bold #e5c07b")
            return False
        self.summary = diff_text(shown, before, after, show=False)
        self.diff = diff_text(shown, before, after, limit=CARD_LIMIT)
        return True

    def expand(self):
        """Show the whole output (a user command via "!": its output is the answer)."""
        self.max_res = 400
        self.draw()

def _size(n):
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{n / 1000:.1f} KB" if n >= 1000 else f"{n} B"


class ChangeCard(SelStatic):
    """A file that a non-edit tool (powershell, py_run, subagent, MCP) created, changed or deleted:
    the same diff an edit card shows, so no change on disk goes unseen."""

    LIMIT = 120   # diff lines; a long new file is cut with "… N more lines"

    def __init__(self, tool, kind, path, old, new):
        super().__init__(classes="card")
        shown = short_path(path)
        if kind == "deleted":
            body = Text(f"{shown}  deleted", style="bold #e06c75")
            if old:
                body.append(f"  ({len(old.splitlines())} lines)", style="grey50")
        elif new is None or (kind == "changed" and old is None):
            import os
            try:
                size = ", " + _size(os.path.getsize(path))
            except OSError:
                size = ""
            why = "binary or large file" if new is None else "previous text unknown"
            body = Text(f"{shown}  {'new' if kind == 'new' else 'changed'}, {why}{size}", style="bold #e5c07b")
        else:
            body = diff_text(shown, old, new, limit=self.LIMIT)
            body.rstrip()
        label = {"new": "New file", "changed": "File changed", "deleted": "File deleted"}[kind]
        title = TITLES.get(tool, tool.replace("mcp__", "MCP: ").replace("__", " › "))
        self.update(Panel(body, title=f"[#7ec69a]✓[/] {label} · {title}", title_align="left", border_style="grey35"))
        self._sel_text = f"[{label}] {body.plain}"


class TodoPanel(SelStatic):
    """Task plan: updated in place while the model works. Not a card — a live list."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.items, self.st, self.diff = [], "✓", None

    def update_list(self, items):
        self.items = items
        self.draw()

    def finish(self, ok, res=None):
        self.st = "✓" if ok else "✗"
        self.draw()

    def draw(self):
        t = Text()
        t.append(f"{self.st} Tasks\n", style="bold grey62")
        for it in self.items:
            st = it["status"]
            col = {"pending": "#8a8f98", "in_progress": "#e5c07b", "done": "#7ec69a"}[st]
            t.append(MARK[st] + " ", style=f"bold {col}")
            t.append(str(it["content"]), style="grey62 strike" if st == "done" else "grey70")
            t.append("\n")
        self.update(t)
        self._sel_text = "Tasks\n" + "\n".join(f"{MARK[i['status']]} {i['content']}" for i in self.items)


class TaskProgress(Static):
    """Live task list generated by the model via todo_write, not a fixed phase template."""

    MARKS = {"pending": "○", "in_progress": "▶", "done": "✓"}
    COLORS = {"pending": "grey50", "in_progress": "#e5c07b", "done": "#7ec69a"}

    def __init__(self, *args, **kwargs):
        self.items = []
        super().__init__(*args, **kwargs)
        self.display = False
        self.draw()

    def reset(self):
        self.items = []
        self.display = False
        self.draw()

    def set_tasks(self, items):
        """Accept only the same validated list the todo_write tool accepts."""
        err = validate_todos(items)
        if err:
            return False
        self.items = copy_tasks(items)
        self.display = True
        self.draw()
        return True

    def snapshot(self):
        return [dict(item) for item in self.items]

    def restore(self, items):
        if not isinstance(items, list) or not items:
            self.reset()
            return
        if not self.set_tasks(items):
            self.reset()

    def draw(self):
        if not self.items:
            self.update("")
            return
        t = Text()
        finished = sum(item["status"] == "done" for item in self.items)
        t.append(f"TASK PLAN {finished}/{len(self.items)}\n", style="bold grey62")
        for i, item in enumerate(self.items, 1):
            state = item["status"]
            t.append(f"{self.MARKS[state]} {i}. {item['content']}\n",
                     style=f"bold {self.COLORS[state]}" if state == "in_progress" else self.COLORS[state])
        self.update(t)

IMAGE_EXT = re.compile(r"\.(png|jpe?g|webp|gif)$", re.I)


def dropped_paths(raw):
    """File paths, if the paste consists only of them (that's how the terminal passes files dropped onto the window:
    space-separated, quoted when they contain spaces, sometimes file:///). Otherwise None — plain text."""
    from pathlib import Path
    from urllib.parse import unquote
    text = (raw or "").strip()
    if not text or len(text) > 8000:
        return None
    paths = []
    for m in re.finditer(r'"([^"]+)"|\'([^\']+)\'|(\S+)', text):
        token = m.group(1) or m.group(2) or m.group(3)
        if token.lower().startswith("file:///"):
            token = unquote(token[8:] if os.name == "nt" else token[7:])
        p = Path(token)
        try:
            if not (p.is_absolute() and p.exists()):
                return None
        except OSError:
            return None
        paths.append(p)
    return paths or None


class Prompt(TextArea):
    class Submitted(Message):
        pass

    PLACEHOLDER = ('Try "refactor <filepath>" · / — commands, /settings — all settings, /local — run a model · '
                   'Shift+Enter — new line')

    def __init__(self):
        # no cursor line highlight: it drew an extra bar across the whole input
        super().__init__(id="inp", show_line_numbers=False, soft_wrap=True, placeholder=self.PLACEHOLDER,
                         highlight_cursor_line=False)

    def on_mouse_up(self, _):
        """Mouse selection in the input is copied right away, as in the chat (TextArea has its own selection,
        the screen's TextSelected doesn't see it)."""
        if self.selected_text.strip():
            self.app.copy_now(self.selected_text, preview=False)

    def insert_pasted_text(self, raw: str) -> int:
        """Insert the entire clipboard payload, regardless of visible input height."""
        from ..tool_validation import normalize_paste_text
        text = normalize_paste_text(raw)
        if not text:
            return 0
        self.insert(text)
        self.scroll_cursor_visible(animate=False)
        return len(text)

    def insert_dropped(self, paths) -> None:
        """Files dropped onto the window: an image — as a quoted path (build_content attaches it),
        anything else — as @path, so the file goes as an attachment (expand_attachments)."""
        refs = []
        for p in paths:
            s = str(p)
            if IMAGE_EXT.search(s):
                refs.append(f'"{s}"')
            else:
                refs.append(f'@"{s}"' if " " in s else "@" + s)
        row, col = self.cursor_location
        before = self.document.get_line(row)[:col]
        self.insert((" " if before and not before.endswith(" ") else "") + " ".join(refs) + " ")
        self.scroll_cursor_visible(animate=False)
        what = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
        self.app.notify(f"Attached {what}", timeout=2)

    def take_paste(self, raw: str) -> None:
        paths = dropped_paths(raw)
        if paths:
            self.insert_dropped(paths)
            return
        length = self.insert_pasted_text(raw)
        if length > 10000:
            self.app.notify(f"Pasted {length:,} characters; full text is retained in the input.", timeout=2)

    def on_paste(self, event: Paste) -> None:
        """Handle paste at the focused TextArea so the bubbling event is inserted only once."""
        if not self.has_focus:
            return
        event.stop()
        event.prevent_default()
        self.take_paste(event.text)

    async def _on_key(self, e):
        app, k = self.app, e.key
        if app.slash_open() and k in ("up", "down", "tab", "enter"):
            e.stop(); e.prevent_default()
            if k in ("up", "down"):
                app.query_one("#slash", OptionList).action_cursor_up() if k == "up" else app.query_one("#slash", OptionList).action_cursor_down()
            else:
                await app.slash_accept(submit=(k == "enter"))
            return
        if k == "enter":
            e.stop(); e.prevent_default()
            if shift_down():
                self.insert("\n")
            elif self.text.rstrip().endswith("\\"):
                self.text = self.text.rstrip()[:-1] + "\n"
                self.move_cursor(self.document.end)
            else:
                self.post_message(self.Submitted())
        elif k in ("ctrl+j", "shift+enter", "ctrl+enter", "alt+enter"):
            e.stop(); e.prevent_default(); self.insert("\n")
        elif k in ("ctrl+g", "alt+g"):   # VS Code keeps Ctrl+G (Go to Line) for itself
            e.stop(); e.prevent_default(); app.open_editor()
        elif k == "alt+v":
            e.stop(); e.prevent_default(); app.paste_image()
        elif k == "shift+tab":
            e.stop(); e.prevent_default(); app.cycle_mode(1)
        elif k == "ctrl+shift+tab":
            e.stop(); e.prevent_default(); app.cycle_mode(-1)
        elif k == "escape":
            if self.text.startswith("!") and not app.busy:
                e.stop(); e.prevent_default(); self.text = ""; return
            e.stop(); e.prevent_default(); app.action_back()
        elif k == "ctrl+x" and app.busy:
            e.stop(); e.prevent_default()
            app.queue_input = True
            app.notify("Next Enter will queue a message for after the current turn.", timeout=2)
        elif k in ("pageup", "pagedown"):
            e.stop(); e.prevent_default()
            chat = app.shown_chat
            chat.scroll_page_up(animate=False) if k == "pageup" else chat.scroll_page_down(animate=False)
        elif k == "up" and self.cursor_location[0] == 0 and app.recall(-1):
            e.stop(); e.prevent_default()
        elif k == "down" and self.cursor_location[0] == self.document.line_count - 1 and app.recall(1):
            e.stop(); e.prevent_default()
        else:
            await super()._on_key(e)
# ───────────────────────── app ─────────────────────────
class Chat(VerticalScroll):
    """Follow the output only while the user has not scrolled up themselves."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.follow = True

    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        if new == self.max_scroll_y:
            self.follow = True
        elif self.follow:
            self.follow = False
