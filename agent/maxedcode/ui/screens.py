"""Modal screens: picker, /btw, session history."""
from contextlib import suppress
from rich.markdown import Markdown
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
import asyncio
import os
from textual.screen import ModalScreen
from textual.widgets import Input, TextArea, Button, Checkbox
from textual.widgets import Static
from textual.widgets.option_list import Option
from textual.widgets import OptionList
from ..util import ago, delete_session, list_sessions
from ..state import CMDS
from .. import state

# Guard textual: dismiss() on a screen whose await was already cancelled (Esc here
# interrupts the turn, which cancels the pending future) raises InvalidStateError.
import textual.screen as _ts
def _safe_call(self, result):
    if self.future is not None and not self.future.done():
        self.future.set_result(result)
    if self.requester is not None and self.callback is not None:
        self.requester.call_next(self.callback, result)
    self.callback = None
_ts.ResultCallback.__call__ = _safe_call

# ───────────────────────── widgets ─────────────────────────
def keys_hint(*pairs):
    """Key hint for the bottom border of a window: "↑↓ select  esc close"."""
    t = Text()
    for key, what in pairs:
        t.append(f" {key}", style="bold #d4d4d4")
        t.append(f" {what} ", style="grey50")
    return t


def dialog(label, *keys, wide=False):
    """Common modal window frame (like Ctrl+P): window type at the top left, keys at the bottom right."""
    box = Vertical(classes="dialog wide" if wide else "dialog")
    box.border_title = Text(f" {label} ", style="bold #d97757")
    box.border_subtitle = keys_hint(*keys)
    return box


class Pick(ModalScreen):
    """Choose from options (+ a custom answer). Digits 1-9 — quick pick. Esc — cancel."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, options, custom=True, body=""):
        super().__init__()
        self.title_, self.opts, self.custom, self.body = title, options, custom, body

    def compose(self):
        with dialog("question", ("↑↓", "select"), ("1-9", "quick pick"), ("⏎", "choose"), ("esc", "cancel")):
            yield Static(Text(self.title_, style="bold #e8e8e8"), id="ptitle", classes="dialog-title")
            if self.body:
                with VerticalScroll(classes="dialog-scroll"):
                    yield Static(Text(self.body[:900], style="#9aa0a8"), id="pbody")
            items = [Option(Text.assemble((f"{i + 1}  ", "grey50"), (str(o), "#d4d4d4")), id=str(i))
                     for i, o in enumerate(self.opts)]
            if self.custom:
                items.append(Option(Text.assemble(("✎  ", "grey50"), ("Custom answer…", "#d4d4d4")), id="custom"))
            yield OptionList(*items, id="plist")
            yield Input(placeholder="Custom answer, Enter — send", id="pin")

    def on_mount(self):
        self.query_one("#pin").display = False
        self.query_one("#plist").focus()

    def on_option_list_option_selected(self, e):
        if e.option.id == "custom":
            self.query_one("#plist").display = False
            i = self.query_one("#pin"); i.display = True; i.focus()
        else:
            self.dismiss(self.opts[int(e.option.id)])

    def on_input_submitted(self, e):
        self.dismiss(e.value.strip() or None)

    def on_key(self, e):
        if e.character and e.character.isdigit() and 0 < int(e.character) <= len(self.opts) and self.query_one("#plist").display:
            self.dismiss(self.opts[int(e.character) - 1])

    def action_cancel(self):
        # Esc while the model is waiting for a decision: interrupt the turn, not just close the pick
        if getattr(self.app, "busy", None):
            self.app.action_cancel()
        self.dismiss(None)


EFFORT_HELP = {
    "off": "No reasoning: instant answers. For trivial questions and one-line edits.",
    "low": "Short reasoning. Fast; fine for simple, well-defined steps.",
    "medium": "Balanced reasoning (recommended). Most everyday tasks.",
    "high": "Deep reasoning on every step. Hard bugs and tricky logic; slower.",
    "ultra": ("high + staged run, for big multi-part tasks (a feature across several files, "
              "\"fix every place where…\"). The agent plans the work, does each part in its own subagent "
              "with a clean context, runs the check, and an independent reviewer looks for what is missing "
              "or only claimed; problems go to fix rounds. Several times slower. Questions and small tasks "
              "still run as one normal high-effort turn. Parts edit files without asking; /undo reverts the turn."),
}
# Purples from dark to light for the pixel field behind the "smarter" end of the scale.
_PURPLES = ("#2a1760", "#341d78", "#3f238f", "#4b2aa6", "#5832bd", "#6640d2", "#7550e2", "#8560ee")


class EffortScreen(ModalScreen):
    """Effort as a scale (Faster ↔ Smarter) with ultra past a divider; ←/→ move, Enter chooses."""
    BINDINGS = [Binding("left", "move(-1)", show=False), Binding("right", "move(1)", show=False),
                Binding("enter", "choose", show=False), Binding("escape", "cancel", show=False)]
    W = 86                   # inner width of a .dialog window: the field reaches its right edge
    COLS = {"off": 8, "low": 22, "medium": 37, "high": 52, "ultra": 73}
    SEP, FIELD = 62, 44      # divider column; the purple field starts around here

    def __init__(self, levels, current):
        super().__init__()
        self.levels = list(levels)
        self.at = self.levels.index(current) if current in self.levels else self.levels.index("medium")

    def compose(self):
        with dialog("effort", ("←→", "select"), ("1-5", "quick pick"), ("⏎", "choose"), ("esc", "cancel")):
            yield Static(id="effort-scale")
            yield Static(id="effort-help", classes="dialog-body")

    def on_mount(self):
        self.paint()

    def _field(self, row, col):
        """Background of one cell: a ragged-edged pixel field that gets lighter to the right."""
        edge = self.FIELD + (row * 37 + 11) % 7
        if col < edge:
            return None
        h = (row * 131 + (col // 2) * 7919) % 97
        idx = min(len(_PURPLES) - 1, h % 3 + (col - edge) // 6)
        return _PURPLES[idx]

    def paint(self):
        cur = self.levels[self.at]
        rows = [[" "] * self.W for _ in range(6)]
        fg = [["grey50"] * self.W for _ in range(6)]

        def put(r, c, text, style):
            for i, ch in enumerate(text):
                if 0 <= c + i < self.W:
                    rows[r][c + i], fg[r][c + i] = ch, style
        put(1, 4, "Faster", "#d4d4d4")
        put(1, self.W - 4 - len("Smarter"), "Smarter", "#ffffff")
        put(2, 4, "─" * (self.W - 8), "#8a8f98")
        put(2, self.SEP, "┆", "#d4d4d4")
        put(2, self.COLS[cur], "▲", "bold #ffffff")
        for name in self.levels:
            col = self.COLS[name] - len(name) // 2
            style = "bold #ffffff" if name == cur else ("#c9b8f5" if self._field(3, col) else "grey50")
            put(3, col, name, style)
        hint = "high + staged run"
        put(4, self.COLS["ultra"] - len(hint) // 2, hint, "#ffffff" if cur == "ultra" else "#c9b8f5")
        t = Text()
        for r in range(len(rows)):
            for c in range(self.W):
                bg = self._field(r, c)
                t.append(rows[r][c], style=f"{fg[r][c]} on {bg}" if bg else fg[r][c])
            if r < len(rows) - 1:
                t.append("\n")
        self.query_one("#effort-scale", Static).update(t)
        help_text = Text.assemble((cur, "bold #b69cff" if cur == "ultra" else "bold #e8e8e8"),
                                  ("  " + EFFORT_HELP.get(cur, ""), "#9aa0a8"))
        self.query_one("#effort-help", Static).update(help_text)

    def action_move(self, delta):
        self.at = max(0, min(len(self.levels) - 1, self.at + int(delta)))
        self.paint()

    def on_key(self, e):
        if e.character and e.character.isdigit() and 0 < int(e.character) <= len(self.levels):
            self.dismiss(self.levels[int(e.character) - 1])

    def action_choose(self):
        self.dismiss(self.levels[self.at])

    def action_cancel(self):
        self.dismiss(None)


class CloudApprovalScreen(ModalScreen):
    """Review the entire exact payload before sending it to a remote provider."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, task, source="parent"):
        super().__init__()
        self.payload = str(task or "")   # not self.task: that's a Textual property (MessagePump.task), assigning it fails
        self.source = source

    def compose(self):
        who = "Subagent" if self.source == "subagent" else "Local agent"
        with dialog("cloud request", ("tab", "switch"), ("⏎", "press"), ("esc", "send nothing"), wide=True):
            yield Static(Text(f"{who} wants to ask the cloud model", style="bold #e8e8e8"), id="ptitle", classes="dialog-title")
            yield Static(Text(
                "Only the text below is sent: no prior messages, files, logs or tools are attached.",
                style="#9aa0a8"), id="cloud-approval-warning", classes="dialog-body")
            yield TextArea(self.payload, id="cloud-task-review", read_only=True, soft_wrap=True, show_line_numbers=False)
            with Horizontal(id="cloud-actions", classes="dialog-actions"):
                yield Button("Cancel", id="cloud-reject")
                yield Button("Always send, don't ask", id="cloud-always")
                yield Button("Approve and send", id="cloud-approve", variant="primary")

    def on_mount(self):
        self.query_one("#cloud-approve", Button).focus()

    def on_button_pressed(self, event):
        if event.button.id == "cloud-approve":
            self.dismiss(True)
        elif event.button.id == "cloud-always":
            self.dismiss("always")
        elif event.button.id == "cloud-reject":
            self.dismiss(False)

    def action_cancel(self):
        self.dismiss(False)


class LinkScreen(ModalScreen):
    """The user clicked an address the agent wrote. Opening it is never automatic: Enter (or the
    button) opens it in the browser, Esc leaves it alone. Dismisses with True / False."""
    BINDINGS = [Binding("enter", "open", "Open"), Binding("escape", "cancel", "Cancel")]

    def __init__(self, url):
        super().__init__()
        self.url_ = str(url)

    def compose(self):
        with dialog("open link", ("⏎", "open"), ("esc", "cancel")):
            yield Static(Text("Open this address in the browser?", style="bold #e8e8e8"),
                         id="ptitle", classes="dialog-title")
            yield Static(Text(self.url_ if len(self.url_) < 400 else self.url_[:400] + "…",
                              style="underline #c9d1d9"), id="link-url", classes="dialog-body")
            with Horizontal(id="link-actions", classes="dialog-actions"):
                yield Button("Cancel", id="link-cancel")
                yield Button("Open in browser", id="link-open", variant="primary")

    def on_mount(self):
        self.query_one("#link-open", Button).focus()

    def close(self, result):
        """Enter on the focused button and the Enter binding can both arrive: dismiss once."""
        if self.is_active:
            self.dismiss(result)

    def on_button_pressed(self, event):
        self.close(event.button.id == "link-open")

    def action_open(self):
        self.close(True)

    def action_cancel(self):
        self.close(False)


def cloud_providers():
    from .. import cloud
    return list(cloud.PROVIDERS)


class CloudSetupScreen(ModalScreen):
    """Configure an OpenAI-compatible cloud endpoint without displaying/storing its key in plain text.
    Enter moves to the next field; Enter in the key field saves."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    FIELDS = ("cloud-base", "cloud-model", "cloud-tokens", "cloud-key")

    def __init__(self, add=False):
        super().__init__()
        from .. import cloud
        self.current = cloud.status()
        self.error = ""
        self.add = add        # True: one more key (account); the provider may come from the key itself

    def compose(self):
        cfg = self.current
        with dialog("cloud setup", ("⏎", "next field"), ("tab", "move"), ("esc", "cancel")):
            yield Static(Text("Add a cloud key" if self.add else "Cloud code assistant", style="bold #e8e8e8"),
                         id="ptitle", classes="dialog-title")
            yield Static(Text(
                "Only the task you approve is sent — never the conversation, files, logs, MCP data or environment. "
                + ("The key is stored with Windows DPAPI (OS keyring elsewhere)." if os.name == "nt" else
                   "The key is stored in the OS keyring, or in a file only you can read."),
                style="#9aa0a8"), id="cloud-privacy")
            yield Static(Text.assemble(("API base URL", "bold #c9d1d9"), ("  any OpenAI-compatible HTTPS endpoint", "grey50")), classes="dialog-label")
            if self.add:
                yield Static(Text("  or a provider: " + ", ".join(cloud_providers()) + "; empty = found from the key",
                                  style="grey50"), classes="dialog-label")
            yield Input(value="" if self.add else cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta/openai/",
                        placeholder="gemini / groq / https://.../v1/" if self.add else "https://.../v1/", id="cloud-base")
            yield Static(Text.assemble(("Model ID", "bold #c9d1d9"), ("  exact id from the provider dashboard", "grey50")), classes="dialog-label")
            yield Input(value="" if self.add else cfg.get("model") or "gemini-3.8-flash",
                        placeholder="empty = the provider's first model" if self.add else "gemini-3.8-flash", id="cloud-model")
            yield Static(Text.assemble(("Max output tokens", "bold #c9d1d9"), ("  thinking models spend part of it on reasoning", "grey50")), classes="dialog-label")
            yield Input(value=str(cfg.get("max_tokens") or ""), placeholder="8192", type="integer", id="cloud-tokens")
            key_hint = "  saved — leave empty to keep it" if cfg.get("has_key") else "  required"
            yield Static(Text.assemble(("API key", "bold #c9d1d9"), (key_hint, "grey50")), classes="dialog-label")
            yield Input(placeholder="Paste provider API key", password=True, id="cloud-key")
            yield Checkbox("Subagents may request cloud code too (each request still needs approval)",
                           value=bool(cfg.get("allow_subagents", True)), id="cloud-subagents")
            yield Static(Text(self.error, style="bold #e06c75"), id="cloud-error")
            with Horizontal(id="cloud-actions", classes="dialog-actions"):
                yield Button("Cancel", id="cloud-cancel")
                yield Button("Test API", id="cloud-test")
                yield Button("Save securely", id="cloud-save", variant="primary")

    def show(self, text, style="#9aa0a8"):
        self.query_one("#cloud-error", Static).update(Text(text, style=style))

    def close(self, result):
        """dismiss() pops the top screen of the stack, whatever it is: a second Save (double click,
        Enter + button) or Esc during saving popped the main screen → ScreenStackError."""
        if self.is_active:
            self.dismiss(result)

    def on_input_submitted(self, event):
        at = self.FIELDS.index(event.input.id) if event.input.id in self.FIELDS else -1
        if 0 <= at < len(self.FIELDS) - 1:
            self.query_one("#" + self.FIELDS[at + 1], Input).focus()
        elif at == len(self.FIELDS) - 1:
            self.run_worker(self.save(), exclusive=True)

    async def on_button_pressed(self, event):
        if event.button.id == "cloud-cancel":
            self.close(None)
        elif event.button.id == "cloud-test":
            await self.test()
        elif event.button.id == "cloud-save":
            await self.save()

    async def test(self):
        from .. import cloud
        base = self.query_one("#cloud-base", Input).value.strip()
        model = self.query_one("#cloud-model", Input).value.strip()
        key = self.query_one("#cloud-key", Input).value
        test_button = self.query_one("#cloud-test", Button)
        test_button.disabled = True
        self.show("Testing with a fixed probe; no conversation or project data is sent…")
        try:
            result = await asyncio.to_thread(lambda: asyncio.run(cloud.test_connection(base, model, key)))
            self.show(result, "#98c379" if result.startswith("Connection successful") else "bold #e06c75")
        except Exception as exc:
            self.show(f"{type(exc).__name__}: {exc}", "bold #e06c75")
        finally:
            test_button.disabled = False

    async def save(self):
        from .. import cloud
        save_button = self.query_one("#cloud-save", Button)
        if save_button.disabled:   # saving is already in progress
            return
        base = self.query_one("#cloud-base", Input).value.strip()
        model = self.query_one("#cloud-model", Input).value.strip()
        key = self.query_one("#cloud-key", Input).value
        allow_subagents = self.query_one("#cloud-subagents", Checkbox).value
        tokens = self.query_one("#cloud-tokens", Input).value.strip()
        save_button.disabled = True
        try:
            if self.add:
                entry = await asyncio.to_thread(cloud.add_account, base, key, model)
                self.close({"saved": True, "account": entry})
                return
            await asyncio.to_thread(cloud.configure, base, model, key, allow_subagents, tokens)
        except Exception as exc:
            self.show(f"{type(exc).__name__}: {exc}", "bold #e06c75")
            save_button.disabled = False
            return
        self.close({"saved": True})

    def action_cancel(self):
        self.close(None)


class BtwScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close"), Binding("enter", "close"), Binding("space", "close")]

    def __init__(self, q):
        super().__init__()
        self.q, self.think, self.text = q, "", ""
        self.status = "Thinking…"   # shown until the first token; a /btw asked mid-step waits for the step

    def compose(self):
        with dialog("btw · not saved to history", ("esc/⏎", "close"), wide=True):
            yield Static(Text(self.q[:200], style="bold #e8e8e8"), id="ptitle", classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static("", id="pbody")

    def on_mount(self):
        self.show()

    def show(self):
        with suppress(Exception):
            body = Markdown(self.text) if self.text else Text(("✻ " + self.think) if self.think else self.status, style="grey62 italic")
            self.query_one("#pbody", Static).update(body)

    def action_close(self):
        self.dismiss(None)


class HistoryScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("delete", "remove", "Delete")]

    def __init__(self, current=None):
        super().__init__()
        self.current, self.ids = str(current or ""), {}

    def compose(self):
        with dialog("history", ("↑↓", "select"), ("⏎", "open"), ("del", "delete"), ("esc", "close"), wide=True):
            yield Static(Text.assemble(("Sessions", "bold #e8e8e8"), ("   ● this project   ▶ open now", "grey50")),
                         id="ptitle", classes="dialog-title")
            yield OptionList(id="plist")

    def on_mount(self):
        self.fill()
        self.query_one("#plist").focus()

    def fill(self, at=0):
        ol = self.query_one("#plist", OptionList)
        ol.clear_options()
        # option id = file path: the same session id in two projects must not break the list
        self.ids = {}
        for x in list_sessions(all_cwds=True):
            if x.get("empty"):
                continue   # a chat that was opened and closed without a single message
            key = str(x["path"])
            self.ids[key] = (x["id"], x["path"])
            title = x["title"] or "(empty session)"
            row = Text()
            row.append("▶ " if x["id"] == self.current else ("● " if x["here"] else "  "),
                       style="#d97757" if x["id"] == self.current else "#98c379")
            row.append(f"{title[:60]:<62}", style="#e0e0e0" if x["here"] else "#b0b5bd")
            row.append(f"{ago(x['ts']):>10}  ", style="grey50")
            row.append(str(x["project"]), style="grey42")
            ol.add_option(Option(row, id=key))
        if ol.option_count:
            ol.highlighted = min(at, ol.option_count - 1)

    def on_option_list_option_selected(self, e):
        self.dismiss(self.ids[e.option.id][0])

    def action_remove(self):
        ol = self.query_one("#plist", OptionList)
        if ol.highlighted is None:
            return
        at = ol.highlighted
        sid, path = self.ids[ol.get_option_at_index(at).id]
        if sid == self.current:
            # the open session would be re-saved on the very next save_session and "come back to life"
            self.notify("This session is open now. Start a new one (Ctrl+N), then delete it here.", severity="warning")
            return
        if getattr(self, "_armed", None) != path:   # deleting can't be undone: the second Del confirms it
            self._armed = path
            self.notify("Press Del again to delete this session for good.", severity="warning", timeout=4)
            return
        self._armed = None
        for gone in delete_session(path):
            with suppress(ValueError):
                state.OPEN_SESSIONS.remove(gone)
            state.CHILD_SESSIONS.pop(gone, None)
        self.fill(at)

    def action_close(self):
        self.dismiss(None)


# Palette groups; commands not listed here (new, custom) go to "other"
PALETTE_GROUPS = (
    ("session", ("/new", "/history", "/sessions", "/resume", "/save", "/search", "/rewind", "/undo", "/redo", "/copy", "/quit")),
    ("context", ("/compact", "/context", "/cache", "/btw", "/memory", "/init")),
    ("model", ("/model", "/models", "/effort", "/sampling", "/cloud")),
    ("modes", ("/act", "/plan", "/auto", "/run")),
    ("tools", ("/tools", "/mcp", "/skill", "/commands", "/agents", "/hooks", "/allow", "/deny", "/permissions", "/editor", "/help")),
)


def _count(n, word):
    return f"{n} {word}" + ("s" if n != 1 else "")


def command_badge(app, name):
    """(text, style) — the current value of the setting the command refers to, straight from the live
    state (the same functions that build the request). Shared by Ctrl+P and /help; empty — nothing to show."""
    from .. import config, permissions, skills
    s = state.S
    try:
        mode = app.cur_mode()
        now = {"/act": mode == "manual", "/plan": mode == "plan", "/auto": mode == "auto"}
        if name in now:
            return ("● active", "bold #98c379") if now[name] else ("", "")
        if name in ("/model", "/models"):
            return (str(s.get("model") or ""), "#c8c8c8")
        if name == "/effort":
            return (str(s.get("effort") or "medium"), "#b69cff" if s.get("effort") == "ultra" else "#d97757")
        if name == "/sampling":
            cur = config.sampling_for(s["model"])
            text = f"temp {cur['temperature']:g} · top_p {cur['top_p']:g} · top_k {config.top_k_for(s['model'])}"
            return (text + (" · override" if config.RUNTIME else ""), "#e5c07b" if config.RUNTIME else "#c8c8c8")
        if name == "/cloud":
            from .. import cloud
            cfg = cloud.status()
            return (str(cfg.get("model")), "#7ec69a") if cfg.get("configured") else ("off", "grey42")
        if name == "/mcp":
            servers = getattr(app.mcp, "srv", {}) or {}
            if not servers:
                return ("none", "grey42")
            up = sum(1 for x in servers.values() if x.get("client"))
            return (f"{up}/{len(servers)} connected", "#98c379" if up == len(servers) else "#e5c07b")
        if name == "/context":
            return (f"{app.fill_pct()}% used", "#e06c75" if app.fill_pct() >= 80 else "grey62")
        if name == "/permissions":
            r = permissions.load()
            n = sum(len(r.get(k) or []) for k in ("allow", "deny", "ask"))
            return (_count(n, "rule"), "grey62") if n else ("none", "grey42")
        if name == "/hooks":
            n = sum(len(h.get("hooks") or []) for hs in (config.SETTINGS.get("hooks") or {}).values() for h in hs)
            return (_count(n, "hook"), "grey62") if n else ("none", "grey42")
        if name == "/commands":
            n = len(skills.commands())
            return (_count(n, "command"), "grey62") if n else ("none", "grey42")
        if name == "/agents":
            n = len(skills.agents())
            return (_count(n, "agent"), "grey62") if n else ("none", "grey42")
    except Exception:
        pass
    return ("", "")


def fuzzy_spans(query, name):
    """Positions of the query letters in name, in order (subsequence), or None."""
    at, spans = 0, []
    for ch in query:
        at = name.find(ch, at)
        if at < 0:
            return None
        spans.append(at)
        at += 1
    return spans


def rank_commands(query, cmds=None):
    """[(name, description, positions of matched name letters)]: name start → substring → letters in order → description."""
    cmds = CMDS if cmds is None else cmds
    q = (query or "").strip().lower().lstrip("/")
    if not q:
        return [(n, d, []) for n, d in cmds.items()]
    tiers = ([], [], [], [])
    for name, description in cmds.items():
        n = name.lower()
        pos = n.find(q, 1)
        if pos == 1:
            tiers[0].append((name, description, list(range(1, 1 + len(q)))))
        elif pos > 1:
            tiers[1].append((name, description, list(range(pos, pos + len(q)))))
        elif (spans := fuzzy_spans(q, n[1:])) is not None:
            tiers[2].append((name, description, [x + 1 for x in spans]))
        elif q in description.lower():
            tiers[3].append((name, description, []))
    return tiers[0] + tiers[1] + tiers[2] + tiers[3]


class CommandPalette(ModalScreen):
    """Command deck (Ctrl+P): commands by group with their current state, fuzzy search, full help of the
    selected command below. Focus stays in the search field: ↑/↓, Ctrl+P/Ctrl+N, PgUp/PgDn — select;
    Enter — run; Tab — insert into the prompt to add arguments; Esc — close."""
    BINDINGS = [Binding("escape", "close", "Close"),
                Binding("up", "move(-1)", show=False, priority=True),
                Binding("down", "move(1)", show=False, priority=True),
                Binding("ctrl+p", "move(-1)", show=False, priority=True),
                Binding("ctrl+n", "move(1)", show=False, priority=True),
                Binding("pageup", "move(-8)", show=False, priority=True),
                Binding("pagedown", "move(8)", show=False, priority=True),
                Binding("tab", "pick('insert')", show=False, priority=True),
                Binding("shift+tab", "move(-1)", show=False, priority=True)]
    WIDTH = 80

    def compose(self):
        with Vertical(id="deck"):
            with Horizontal(id="deck-search"):
                yield Static(Text("❯", style="bold #d97757"), id="deck-caret")
                yield Input(placeholder="type a command, letters may skip: cmp → /compact", id="palette-input")
            ol = OptionList(id="palette-list")
            ol.can_focus = False   # focus always stays in the search: you can type without going back to the field
            yield ol
            yield Static("", id="deck-detail")

    def on_mount(self):
        keys = Text()
        for key, what in (("↑↓", "move"), ("⏎", "run"), ("tab", "args"), ("esc", "close")):
            keys.append(f" {key}", style="bold #d4d4d4")
            keys.append(f" {what} ", style="grey50")
        self.query_one("#deck").border_subtitle = keys
        self.fill("")
        self.query_one("#palette-input", Input).focus()

    def badge(self, name):
        return command_badge(self.app, name)

    def row(self, name, description, spans, width, inner):
        badge, badge_style = self.badge(name)
        room = max(8, inner - width - (len(badge) + 2 if badge else 0))
        if len(description) > room:
            description = description[:room - 1].rstrip() + "…"
        t = Text()
        for i, ch in enumerate(name):
            t.append(ch, style="bold underline #ffb38a" if i in spans else "bold #e8e8e8")
        t.append(" " * (width - len(name)))
        t.append(description, style="#8f949c")
        if badge:
            t.append(" " * max(2, inner - t.cell_len - len(badge)))
            t.append(badge, style=badge_style)
        return t

    def recent(self):
        """Recent slash commands from the input history (no repeats), newest first."""
        seen = []
        for line in reversed(getattr(self.app, "hist", []) or []):
            cmd = str(line).strip().split(" ", 1)[0]
            if cmd in CMDS and cmd not in seen:
                seen.append(cmd)
            if len(seen) == 3:
                break
        return seen

    def fill(self, query):
        ol = self.query_one("#palette-list", OptionList)
        ol.clear_options()
        typed = bool((query or "").strip())
        found = rank_commands(query)
        inner = max(30, min(self.WIDTH, self.app.size.width * 95 // 100) - 8)   # border, padding, scrollbar
        width = max((len(n) for n, _, _ in found), default=8) + 2
        if typed:
            groups = [("", found)]
        else:
            # no query — by groups, recent ones on top
            by_name = {n: (n, d, sp) for n, d, sp in found}
            groups = [("recent", [by_name[n] for n in self.recent()])]
            listed = set()
            for title, names in PALETTE_GROUPS:
                groups.append((title, [by_name[n] for n in names if n in by_name]))
                listed.update(names)
            groups.append(("other", [x for x in found if x[0] not in listed]))
        seen = set()
        for title, items in groups:
            if not items:
                continue
            if title:
                # group header: "◆ session ─────" across the full width
                head = Text.assemble(("◆ ", "#d97757"), (title, "bold #a3a9b3"), (" " + "─" * max(0, inner - len(title) - 3), "#2a2e35"))
                ol.add_option(Option(head, id=f"#group-{title}", disabled=True))
            for name, description, spans in items:
                oid = name if name not in seen else f"{name}#{title}"   # a command can be both in recent and in a group
                seen.add(name)
                ol.add_option(Option(self.row(name, description, spans, width, inner), id=oid))
        self.query_one("#deck").border_title = Text.assemble(
            (" commands ", "bold #d97757"), (f"{len(found)}/{len(CMDS)} " if typed else f"{len(CMDS)} ", "grey50"))
        if not found:
            self.query_one("#deck-detail", Static).update(Text("nothing matches — keep typing or Esc", style="#d97757"))
        self.select(0, 1)

    def command_at(self, index):
        option = self.query_one("#palette-list", OptionList).get_option_at_index(index)
        return None if option.disabled else option.id.split("#", 1)[0]

    def select(self, start, step):
        """Highlight the first command from start in the step direction, skipping group headers."""
        ol = self.query_one("#palette-list", OptionList)
        n = ol.option_count
        for k in range(n):
            i = (start + k * step) % n
            if not ol.get_option_at_index(i).disabled:
                ol.highlighted = i
                return

    def on_option_list_option_highlighted(self, event):
        name = self.command_at(event.option_index)
        if not name:
            return
        detail = Text()
        detail.append(name, style="bold #d97757")
        badge, badge_style = self.badge(name)
        if badge:
            detail.append("  " + badge, style=badge_style)
        detail.append("\n" + CMDS.get(name, ""), style="#c8c8c8")
        self.query_one("#deck-detail", Static).update(detail)

    def on_input_changed(self, event):
        if event.input.id == "palette-input":
            self.fill(event.value)

    def action_move(self, delta):
        ol = self.query_one("#palette-list", OptionList)
        if not ol.option_count:
            return
        at = 0 if ol.highlighted is None else ol.highlighted
        step = 1 if delta > 0 else -1
        if abs(delta) == 1:
            self.select(at + step, step)   # arrows wrap around
            return
        # PgUp/PgDn stop at the edge instead of jumping to the other end
        target = max(0, min(ol.option_count - 1, at + delta))
        self.select(target, step if 0 < at + delta < ol.option_count - 1 else -step)

    def action_pick(self, how="run"):
        ol = self.query_one("#palette-list", OptionList)
        if ol.option_count and ol.highlighted is not None and self.command_at(ol.highlighted):
            self.dismiss((how, self.command_at(ol.highlighted)))

    def on_input_submitted(self, event):
        self.action_pick("run")

    def on_option_list_option_selected(self, event):
        if self.command_at(event.option_index):
            self.dismiss(("run", self.command_at(event.option_index)))   # mouse click

    def action_close(self):
        self.dismiss(None)


class EditorScreen(ModalScreen):
    """Prompt editor (/editor, Ctrl+G in the input): a large multiline field for writing a long prompt.
    Enter is a plain new line here; Ctrl+S puts the text back into the input, Esc discards the changes."""
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("ctrl+s", "save", "Use draft", priority=True),
                Binding("ctrl+enter", "save", "Use draft", show=False, priority=True)]

    def __init__(self, initial=""):
        super().__init__()
        self.initial = initial or ""

    def compose(self):
        with dialog("prompt editor", ("ctrl+s", "put into the input"), ("esc", "discard"), wide=True):
            yield Static(Text.assemble(("Write a long prompt", "bold #e8e8e8"),
                                       ("   Enter — new line, nothing is sent until you press Enter in the input", "grey50")),
                         id="ptitle", classes="dialog-title")
            yield TextArea(self.initial, id="editor-area", soft_wrap=True, show_line_numbers=True)
            yield Static("", id="editor-count")

    def on_mount(self):
        area = self.query_one("#editor-area", TextArea)
        area.focus()
        area.move_cursor(area.document.end)
        self.count()

    def on_text_area_changed(self, event):
        self.count()

    def count(self):
        text = self.query_one("#editor-area", TextArea).text
        self.query_one("#editor-count", Static).update(
            Text(f"{len(text.splitlines()) or 1} lines · {len(text):,} chars", style="grey50", justify="right"))

    def action_save(self):
        self.dismiss(self.query_one("#editor-area", TextArea).text)

    def action_cancel(self):
        self.dismiss(None)

