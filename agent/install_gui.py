"""maxedcode setup window (tkinter): the steps of the console wizard in install.py - install folder, where the model
runs, model and context, options - then the install itself with its log.

install.py opens it when it runs without --yes / --console; with no tkinter (or no window possible) the console
wizard runs instead. All the work is done by install.py (run_install, probe, candidates ...): this file only asks."""
import os
import queue
import subprocess
import sys
import threading
import traceback
from contextlib import suppress
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, font, messagebox, ttk
except ImportError:      # Python without Tcl/Tk: the console wizard
    tk = None

BG, SIDE, SIDE_FG, SIDE_ON, MUTED = "#ffffff", "#1e2330", "#8a93a6", "#ffffff", "#6b7280"
OK, WARN, ERR, ACCENT = "#15803d", "#b45309", "#b91c1c", "#2563eb"
# Marks of done steps and results; a Tk without Xft (the Python uv installs on Linux) draws them as boxes
MARK, CROSS = "✓", "✗"


def available():
    return tk is not None


def run(mod):
    """Opens the setup window -> exit code (0 done or cancelled, 1 finished with errors), None = no window."""
    with suppress(Exception):
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # sharp text on scaled displays
    try:
        app = Setup(mod)
    except tk.TclError as e:
        mod.log(f"setup window not available: {e}")
        mod.UI["sink"] = None
        return None
    console(6)               # minimize the console of Maxedcode_setup.exe; the window is the setup now
    try:
        app.root.mainloop()
    finally:
        mod.UI["sink"] = None
    if mod.UI["errors"]:
        console(9)
        print("\n  Setup finished with problems:")
        for e in mod.UI["errors"]:
            print(f"   x {e}")
        print(f"\n  The whole log: {mod.LOG}")
        return 1
    return 0


def console(cmd):
    """ShowWindow on the console of Maxedcode_setup.exe (6 minimize, 9 restore); a console of the user's own is left alone."""
    if not os.environ.get("MX_SETUP_BAT"):
        return
    with suppress(Exception):
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, cmd)


class Setup:
    """The wizard: a sidebar with the steps, a header, the page, and Back / Next / Cancel at the bottom."""

    def __init__(self, mod):
        self.m, self.a = mod, mod.ARGS
        self.configure = bool(self.a.configure)
        self.q = queue.Queue()
        self.live = False                  # the install page shows what setup says
        self.busy_now = False
        self.done = False
        mod.UI["sink"] = self.sink
        self.steps = (["Model server", "Model", "Save"] if self.configure else
                      ["Install folder", "Model server", "Model", "Install"])
        self.server = None                 # (base, key, models | None, kind)
        self.found = None                  # servers.scan() result
        self.cur = {}

        self.root = tk.Tk()
        self.root.withdraw()
        self.k = self.root.winfo_fpixels("1i") / 96
        self.root.title("maxedcode - model server" if self.configure else "maxedcode setup")
        with suppress(Exception):
            if mod.WINDOWS:
                ico = mod.SRC_DESKTOP / "maxedcode.ico"
                if not ico.exists():
                    ico = mod.HERE / "desktop" / "maxedcode.ico"
                self.root.iconbitmap(default=str(ico))
            else:                        # Tk on Linux / macOS takes no .ico: the png
                png = mod.SRC_DESKTOP / "maxedcode.png"
                self.icon = tk.PhotoImage(file=str(png if png.exists() else mod.HERE / "desktop" / "maxedcode.png"))
                self.root.iconphoto(True, self.icon)
        w, h = self.px(860), self.px(600)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(self.px(720), self.px(520))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.on_error
        self.style()
        self.layout()
        self.root.deiconify()
        self.tick = self.root.after(60, self.pump)
        if self.configure:
            self.dest = mod.HERE
            self.cur = self.quiet(mod.load_cur, self.dest)
            self.page_server()
        else:
            self.page_folder()

    # ---------- look ----------

    def px(self, n):
        return int(n * self.k)

    def style(self):
        s = ttk.Style(self.root)
        with suppress(tk.TclError):
            s.theme_use("vista")
        global MARK, CROSS
        with suppress(tk.TclError):
            if self.root.tk.call("::tk::pkgconfig", "get", "fontsystem") == "x11":
                MARK, CROSS = "+", "x"
        base = font.nametofont("TkDefaultFont")
        if os.name == "nt":
            sans, semi, mono, bold = "Segoe UI", "Segoe UI Semibold", "Consolas", "normal"
        else:
            # Segoe UI / Consolas exist only on Windows: Tk fell back to a wide fixed font, the text was cut off
            # on the right and the check marks were boxes (a fresh Ubuntu 24.04)
            sans = next((f for f in ("Ubuntu", "Cantarell", "DejaVu Sans", "Helvetica Neue", "Helvetica")
                         if f in set(font.families(self.root))), "Helvetica")
            semi, bold = sans, "bold"
            mono = next((f for f in ("Ubuntu Mono", "DejaVu Sans Mono", "Menlo", "Courier")
                         if f in set(font.families(self.root))), "Courier")
        base.configure(family=sans, size=10)
        font.nametofont("TkTextFont").configure(family=sans, size=10)
        self.f_title = font.Font(family=semi, size=16, weight=bold)
        self.f_bold = font.Font(family=semi, size=10, weight=bold)
        self.f_small = font.Font(family=sans, size=9)
        self.f_brand = font.Font(family=semi, size=14, weight=bold)
        self.f_mono = font.Font(family=mono, size=9)
        for name in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton", "TLabelframe", "TLabelframe.Label"):
            s.configure(name, background=BG)
        s.configure("Muted.TLabel", foreground=MUTED, font=self.f_small)
        s.configure("Title.TLabel", font=self.f_title)
        s.configure("Bold.TLabel", font=self.f_bold)
        s.configure("Ok.TLabel", foreground=OK)
        s.configure("Warn.TLabel", foreground=WARN)
        s.configure("Err.TLabel", foreground=ERR)
        s.configure("Foot.TFrame", background="#f3f4f6")
        rh = self.f_bold.metrics("linespace") + self.px(12)
        s.configure("Treeview", rowheight=rh, font=base)
        s.configure("TButton", padding=(self.px(8), self.px(2)))

    def layout(self):
        side = tk.Frame(self.root, bg=SIDE, width=self.px(200))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="maxedcode", bg=SIDE, fg=SIDE_ON, font=self.f_brand, anchor="w").pack(
            fill="x", padx=self.px(20), pady=(self.px(24), self.px(22)))
        self.step_labels = []
        for name in self.steps:
            lb = tk.Label(side, text=name, bg=SIDE, fg=SIDE_FG, font=self.f_bold, anchor="w")
            lb.pack(fill="x", padx=self.px(20), pady=self.px(5))
            self.step_labels.append(lb)
        tk.Label(side, text="Your chats and settings\nare kept on every update.", bg=SIDE, fg=SIDE_FG,
                 font=self.f_small, justify="left", anchor="w").pack(side="bottom", fill="x", padx=self.px(20),
                                                                     pady=self.px(20))
        main = ttk.Frame(self.root)
        main.pack(side="left", fill="both", expand=True)
        head = ttk.Frame(main, padding=(self.px(28), self.px(22), self.px(28), self.px(6)))
        head.pack(fill="x")
        self.h_title = ttk.Label(head, style="Title.TLabel")
        self.h_title.pack(anchor="w")
        self.h_sub = ttk.Label(head, style="Muted.TLabel", wraplength=self.px(600), justify="left")
        self.h_sub.pack(anchor="w", pady=(self.px(4), 0))
        foot = ttk.Frame(main, style="Foot.TFrame", padding=(self.px(20), self.px(12)))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(main).pack(side="bottom", fill="x")
        self.b_cancel = ttk.Button(foot, text="Cancel", command=self.on_close)
        self.b_cancel.pack(side="right")
        self.b_next = ttk.Button(foot, text="Next", default="active")
        self.b_next.pack(side="right", padx=(0, self.px(10)))
        self.b_back = ttk.Button(foot, text="Back")
        self.b_back.pack(side="right", padx=(0, self.px(6)))
        self.b_extra = ttk.Button(foot)
        self.spin = ttk.Progressbar(foot, mode="indeterminate", length=self.px(120))
        self.status = tk.Label(foot, bg="#f3f4f6", fg=MUTED, anchor="w", font=self.f_small)
        self.status.pack(side="left", fill="x", expand=True)
        self.body = ttk.Frame(main, padding=(self.px(28), self.px(10), self.px(28), self.px(14)))
        self.body.pack(fill="both", expand=True)
        self.root.bind("<Return>", lambda e: self.b_next.invoke() if str(self.b_next["state"]) != "disabled" else None)

    def page(self, n, title, sub, back=None, nxt=None, next_text="Next"):
        """Clears the body and sets the header, the sidebar step and the buttons for a page."""
        for w in self.body.winfo_children():
            w.destroy()
        for i, lb in enumerate(self.step_labels):
            lb.configure(fg=SIDE_ON if i == n else SIDE_FG, text=(MARK + "  " if i < n else "") + self.steps[i])
        self.h_title.configure(text=title)
        self.h_sub.configure(text=sub)
        self.can = {self.b_back: bool(back), self.b_next: bool(nxt)}
        self.b_back.configure(command=back or (lambda: None), state="normal" if back else "disabled")
        self.b_next.configure(text=next_text, command=nxt or (lambda: None), state="normal" if nxt else "disabled")
        self.b_extra.pack_forget()
        self.say_status("")
        return self.body

    def say_status(self, text, kind=""):
        self.status.configure(text=text, fg={"ok": OK, "warn": WARN, "err": ERR}.get(kind, MUTED))

    def busy(self, text=""):
        """While a check runs: the buttons wait, a spinner turns. busy() without text ends it."""
        self.busy_now = bool(text)
        for b, on in self.can.items():
            b.configure(state="normal" if on and not text else "disabled")
        if text:
            self.spin.pack(side="left", padx=(0, self.px(10)), before=self.status)
            self.spin.start(12)
        else:
            self.spin.stop()
            self.spin.pack_forget()
        self.say_status(text)

    # ---------- plumbing ----------

    def sink(self, msg, kind):
        """say() of install.py: shown in the install log; before the install it only goes to the log file."""
        if self.live:
            self.q.put(("log", msg, kind))

    def bg(self, work, done):
        """work() in a thread (network checks, the install), then done(result) in the window; an error -> result."""
        def go():
            try:
                r = work()
            except Exception as e:       # shown to the user by done(); the traceback is in the log file
                self.m.log(traceback.format_exc())
                r = e
            self.q.put(("call", lambda: done(r)))
        threading.Thread(target=go, daemon=True).start()

    def pump(self):
        with suppress(queue.Empty):
            while True:
                item = self.q.get_nowait()
                if item[0] == "call":
                    item[1]()
                else:
                    self.append(item[1], item[2])
        self.tick = self.root.after(60, self.pump)

    def quit(self):
        with suppress(tk.TclError):
            self.root.after_cancel(self.tick)
        self.root.destroy()

    def quiet(self, fn, *args):
        """Calls fn with say() going only to the log file."""
        live, self.live = self.live, False
        try:
            return fn(*args)
        finally:
            self.live = live

    def on_error(self, *exc):
        self.m.log("".join(traceback.format_exception(*exc)))
        messagebox.showerror("maxedcode setup", f"{exc[1]}\n\nDetails: {self.m.LOG}", parent=self.root)

    def on_close(self):
        if self.live and not self.done:
            messagebox.showinfo("maxedcode setup", "Setup is still working; please wait until it finishes.",
                                parent=self.root)
            return
        if not self.done and not messagebox.askyesno("maxedcode setup", "Cancel setup? Nothing has been changed.",
                                                     parent=self.root):
            return
        self.quit()

    def failed(self, r):
        if isinstance(r, Exception):
            self.busy()
            self.say_status(f"{type(r).__name__}: {r}", "err")
            return True
        return False

    # ---------- 1. install folder ----------

    def page_folder(self):
        b = self.page(0, "Install maxedcode", "A coding agent for your own model server: on this computer, "
                      "on another one in the network, or in the cloud.", nxt=self.folder_next)
        ttk.Label(b, text="This installs", style="Bold.TLabel").pack(anchor="w")
        for name, hint in self.m.COMPONENTS:
            row = ttk.Frame(b)
            row.pack(fill="x", pady=(self.px(6), 0))
            ttk.Label(row, text=MARK, style="Ok.TLabel", width=2).pack(side="left")
            ttk.Label(row, text=name, style="Bold.TLabel").pack(side="left")
            ttk.Label(row, text="  " + hint, style="Muted.TLabel").pack(side="left")
        ttk.Label(b, text="Install folder", style="Bold.TLabel").pack(anchor="w", pady=(self.px(26), self.px(6)))
        row = ttk.Frame(b)
        row.pack(fill="x")
        self.v_dir = getattr(self, "v_dir", None) or tk.StringVar(
            value=str(Path(self.a.dir).expanduser()) if self.a.dir else str(self.m.DEFAULT_DIR))
        ttk.Entry(row, textvariable=self.v_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(self.px(8), 0))
        # exactly where things go, for the folder typed right now
        where = ttk.Label(b, style="Muted.TLabel", wraplength=self.px(600), justify="left", font=self.f_mono)
        where.pack(anchor="w", pady=(self.px(10), 0))
        found = ttk.Label(b, style="Muted.TLabel", wraplength=self.px(600), justify="left")
        found.pack(anchor="w", pady=(self.px(8), 0))

        def show(*_):
            text = self.v_dir.get().strip().strip('"')
            root = Path(text).expanduser() if text else None
            if not root:
                where.configure(text="")
                found.configure(text="")
                return
            if self.m.WINDOWS:
                where.configure(text=f"program:  {root / self.m.PROGRAM}\n"
                                     f"app:      {root / 'maxedcode app.bat'}\n"
                                     f"CLI:      {root / 'maxedcode CLI.bat'}\n"
                                     f"data:     {self.m.DATA_DIR}")
            else:
                where.configure(text=f"program:  {root / self.m.PROGRAM}\n"
                                     f"launcher: {root / self.m.PROGRAM / 'bin' / 'maxed'} (maxed app: the window)\n"
                                     f"command:  {self.m.local_bin() / 'maxed'}\n"
                                     f"data:     {self.m.DATA_DIR}")
            there = (root / self.m.PROGRAM / "install.py").exists()
            places = ("Shortcuts \"maxedcode app\" and \"maxedcode CLI\" go on the desktop and in Start, maxed "
                      "works in any folder" if self.m.WINDOWS else
                      "maxedcode.app goes to ~/Applications, maxed works in any terminal" if self.m.MACOS else
                      "\"maxedcode\" and \"maxedcode CLI\" go to the app menu, maxed works in any terminal")
            found.configure(text=("maxedcode is already installed here: setup updates it and keeps the model server. "
                                  if there else "Already installed somewhere else? Pick that folder to update it. ") +
                            places + "; chats, memory and settings (data) are never removed.")
        self.v_dir.trace_add("write", show)
        show()

    def browse(self):
        d = filedialog.askdirectory(parent=self.root, initialdir=self.v_dir.get() or str(Path.home()),
                                    title="Install folder")
        if d:
            self.v_dir.set(str(Path(d)))

    def folder_next(self):
        text = self.v_dir.get().strip().strip('"')
        if not text:
            self.say_status("choose a folder", "err")
            return
        try:
            root = Path(text).expanduser().resolve()
        except (OSError, ValueError) as e:
            self.say_status(f"this folder cannot be used: {e}", "err")
            return
        self.dest = root / self.m.PROGRAM
        self.cur = self.quiet(self.m.load_cur, self.dest)
        self.page_server()

    # ---------- 2. model server ----------

    def page_server(self):
        n = 0 if self.configure else 1
        b = self.page(n, "Where does the model run?",
                      "Servers found on this computer come first. The server does not have to run now: its address "
                      "is saved, and maxedcode connects once it runs.",
                      back=None if self.configure else self.page_folder, nxt=self.server_next)
        self.key = self.a.api_key or self.cur.get("api_key") or "local"
        self.had = self.cur.get("base_url")
        box = ttk.Frame(b)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("hint",), show="tree", selectmode="browse", height=8)
        self.tree.column("#0", width=self.px(300), stretch=False)
        self.tree.column("hint", width=self.px(300), stretch=True)
        self.tree.tag_configure("muted", foreground=MUTED)
        self.tree.tag_configure("on", foreground=OK)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.server_fields())
        self.tree.bind("<Double-1>", lambda e: self.b_next.invoke())
        tools = ttk.Frame(b)
        tools.pack(fill="x", pady=(self.px(8), 0))
        ttk.Button(tools, text="Look again", command=self.rescan).pack(side="left")
        ttk.Button(tools, text="Scan the local network", command=self.scan_lan).pack(side="left",
                                                                                     padx=(self.px(8), 0))
        self.fields = ttk.Frame(b)
        self.fields.pack(fill="x", pady=(self.px(10), 0))
        self.v_addr = tk.StringVar()
        self.v_key = tk.StringVar()
        self.v_cloud = tk.StringVar()
        self.lan = []
        if self.found is None:
            self.rescan()
        else:
            self.fill_servers()

    def rescan(self):
        self.busy("looking for model servers on this computer ...")
        self.bg(lambda: self.m.servers.scan(self.m.strata_key() or self.key), self.scanned)

    def scanned(self, r):
        if self.failed(r):
            r = []
        self.busy()
        self.found = r
        self.fill_servers()
        running = [x for x in r if x.get("running")]
        self.say_status(f"running: {', '.join(x['label'] for x in running)}" if running else
                        "no model server runs right now", "ok" if running else "")

    def scan_lan(self):
        self.busy("scanning the local network (5-15 s) ...")

        def work():
            nets = self.m.local_nets()
            if not nets:
                return []
            targets = [(str(ip), p) for net in nets for ip in net.hosts() for p in self.m.PORTS]
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(256) as ex:
                opened = [t for t, ok in zip(targets, ex.map(lambda t: self.m.port_open(*t), targets)) if ok]
            urls = [f"http://{ip}:{p}/v1" for ip, p in opened]
            return [(u, m) for u, ok, m in self.m.probe_all(urls, self.key) if ok] if urls else []

        def done(r):
            if self.failed(r):
                return
            self.busy()
            self.lan = r
            self.fill_servers()
            self.say_status(f"found on the network: {len(r)}" if r else "no model servers found on the network",
                            "ok" if r else "warn")
        self.bg(work, done)

    def fill_servers(self):
        m, had = self.m, self.had
        opts = [("found", x, x["label"] + " on this computer",
                 m.found_hint(x) if x.get("running") else "installed, not running now") for x in self.found]
        if not any(x["kind"] == "strata" for x in self.found):
            opts.append(("strata", None, "Strata on this computer",
                         ("installed, not running" if m.strata_installed() else "not installed, get it later") +
                         # Linux / macOS: Strata is no default there; LM Studio / Ollama show up above when present
                         ("  (recommended)" if os.name == "nt" or m.strata_installed() else "")))
        for url, models in self.lan:
            opts.append(("lan", (url, models), url, f"on the network, {len(models or [])} models"))
        if had and not m.is_strata(had) and not any(m.same_url(had, x["base"]) for x in self.found):
            opts.append(("keep", None, "Keep the current server", had))
        opts += [("ip", None, "Another computer on the network", "type its IP, e.g. 192.168.0.10"),
                 ("url", None, "Enter the API URL", "http://... or https://..."),
                 ("cloud", None, "Cloud API", self.m.CLOUD_HINT)]
        self.opts = opts
        self.tree.delete(*self.tree.get_children())
        for i, (what, x, label, hint) in enumerate(opts):
            tag = "on" if what == "found" and x.get("running") else "muted" if what not in ("found", "strata") else ""
            self.tree.insert("", "end", iid=str(i), text="  " + label, values=(hint,), tags=(tag,))
        first = m.default_row(opts, had)
        self.tree.selection_set(str(first))
        self.tree.focus(str(first))
        self.tree.see(str(first))
        self.server_fields()

    def chosen(self):
        sel = self.tree.selection()
        return self.opts[int(sel[0])] if sel else None

    def server_fields(self):
        """Under the list: the address and / or API key the chosen row needs."""
        for w in self.fields.winfo_children():
            w.destroy()
        o = self.chosen()
        if not o:
            return
        what, x = o[0], o[1]
        need_addr = what in ("ip", "url")
        need_key = what in ("url", "cloud") or (what == "found" and x.get("need_key"))
        r = 0
        if what == "cloud":
            names = list(self.m.CLOUD)
            if self.v_cloud.get() not in names:
                self.v_cloud.set(next((n for n in names if self.m.same_url(self.had, self.m.CLOUD[n])), names[0]))
            ttk.Label(self.fields, text="Provider").grid(row=r, column=0, sticky="w")
            box = ttk.Combobox(self.fields, textvariable=self.v_cloud, values=names, state="readonly")
            box.grid(row=r, column=1, sticky="we", padx=(self.px(10), 0))
            box.bind("<<ComboboxSelected>>", lambda e: self.server_fields())
            ttk.Label(self.fields, style="Muted.TLabel", text=self.m.CLOUD[self.v_cloud.get()] +
                      "  - the API key comes from the provider's site").grid(
                row=r + 1, column=1, sticky="w", padx=(self.px(10), 0))
            r += 2
            url = self.m.CLOUD[self.v_cloud.get()]
            self.v_key.set(self.key if self.had and self.m.same_url(self.had, url) and self.key != "local" else "")
        elif need_key and self.v_key.get() == "" and self.key != "local":
            self.v_key.set(self.key)
        if need_addr:
            ttk.Label(self.fields, text="Address" if what == "ip" else "API URL").grid(row=r, column=0, sticky="w")
            e = ttk.Entry(self.fields, textvariable=self.v_addr)
            e.grid(row=r, column=1, sticky="we", padx=(self.px(10), 0))
            ttk.Label(self.fields, style="Muted.TLabel",
                      text="192.168.0.10 or 192.168.0.10:1234 - the port and /v1 are found by themselves"
                      if what == "ip" else "e.g. http://192.168.0.10:8080/v1 or https://api.example.com/v1").grid(
                row=r + 1, column=1, sticky="w", padx=(self.px(10), 0))
            e.focus_set()
            r += 2
        if need_key:
            ttk.Label(self.fields, text="API key").grid(row=r, column=0, sticky="w", pady=(self.px(6), 0))
            ttk.Entry(self.fields, textvariable=self.v_key, show="•").grid(
                row=r, column=1, sticky="we", padx=(self.px(10), 0), pady=(self.px(6), 0))
            if what == "url":
                ttk.Label(self.fields, text="leave empty when the server needs no key", style="Muted.TLabel").grid(
                    row=r + 1, column=1, sticky="w", padx=(self.px(10), 0))
        if what == "found" and not x.get("running"):
            ttk.Label(self.fields, style="Muted.TLabel", wraplength=self.px(600), justify="left",
                      text=f"{x['label']} is installed but not running. Setup saves its address; start it yourself "
                           f"later: {x.get('how') or 'open ' + x['label'] + ' and start its server'}").grid(
                row=r, column=0, columnspan=2, sticky="w")
        self.fields.columnconfigure(1, weight=1)

    def server_next(self):
        o = self.chosen()
        if not o:
            self.say_status("choose where the model runs", "err")
            return
        what, x = o[0], o[1]
        addr = self.v_addr.get().strip()
        key = self.v_key.get().strip()
        if what in ("ip", "url") and not addr:
            self.say_status("type the address first", "err")
            return
        if what == "cloud":
            addr = self.m.CLOUD.get(self.v_cloud.get()) or ""
        if what == "cloud" and not key:
            self.say_status("this cloud needs an API key", "err")
            return
        key = key or (self.key if what != "cloud" else "")
        self.busy(f"checking {addr or 'the server'} ...")
        self.bg(lambda: self.resolve(what, x, addr, key or "local"), self.resolved)

    def resolve(self, what, x, addr, key):
        """The chosen row -> ("ok", (base, key, models|None, kind), note) | ("need_key", None, why) |
        ("down", (base, key, None, ""), [reasons]) | ("error", None, why)."""
        m = self.m
        if what == "found":
            if not x.get("running"):
                return "ok", (x["base"], key, None, x["kind"]), f"{x['label']} is saved; start it later"
            if not x.get("need_key"):
                return "ok", (x["base"], key, x.get("models"), x["kind"]), ""
            ok, models = m.probe(x["base"], key)
            if ok and models is not None:
                return "ok", (x["base"], key, models, x["kind"]), ""
            return "need_key", None, "the server asks for an API key (or did not accept this one)"
        if what == "lan":
            url, models = x
            return "ok", (url, key, models, m.servers.kind_of(url, key) if models is not None else ""), ""
        if what == "strata":
            key = m.strata_key() or key
            ok, models = m.probe(m.STRATA_URL, key, 3)
            if ok and models is None:
                return "need_key", None, "Strata asks for an API key (Strata app: Serving -> API key)"
            note = "" if ok else ("Strata is not answering yet; start it later: strata launch" if m.strata_installed()
                                  else f"Strata is not installed yet: {m.STRATA_HOME}")
            return "ok", (m.STRATA_URL, key, models if ok else None, "strata"), note
        target = self.had if what == "keep" else addr      # ip / url / cloud: read on the Tk thread
        urls = m.candidates(target)
        if not urls:
            return "error", None, f"cannot read the address '{target}'. Example: 192.168.0.10 or " \
                                  "http://192.168.0.10:1234/v1"
        res = m.probe_all(urls, key)
        for url, ok, models in res:
            if ok and models is None:
                return "need_key", None, f"{url} asks for an API key (or the key is wrong)"
            if ok:
                return "ok", (url, key, models, m.servers.kind_of(url, key)), ""
        return "down", (urls[0], key, None, ""), list(dict.fromkeys(r for *_, r in res))

    def resolved(self, r):
        if self.failed(r):
            return
        self.busy()
        status, server, info = r
        if status == "error":
            self.say_status(info, "err")
            return
        if status == "need_key":
            self.say_status(info, "warn")
            o = self.chosen()
            if o and o[0] == "found":
                o[1]["need_key"] = True
            elif o and o[0] in ("ip", "keep"):     # an address that wants a key: ask it as an API URL
                self.v_addr.set(self.v_addr.get() or self.had or "")
                self.tree.selection_set(str(next(i for i, op in enumerate(self.opts) if op[0] == "url")))
            self.server_fields()
            return
        if status == "down":
            reasons = "\n".join(f"  - {x}" for x in info)
            if not messagebox.askyesno(
                    "The server does not answer",
                    f"No answer from {server[0]}:\n{reasons}\n\nIs the server running, and does it listen on the "
                    "network (not only on 127.0.0.1)? Is the port open in the firewall of that computer?\n\n"
                    "Save this address anyway? maxedcode connects once the server runs.", parent=self.root):
                self.say_status("not saved: check the address, or choose another server", "warn")
                return
            info = ""
        self.server = server
        self.note = info
        self.page_model()

    # ---------- 3. model, context, options ----------

    def page_model(self):
        n = 1 if self.configure else 2
        b = self.page(n, "Model and options", "", back=self.page_server)
        self.busy("asking the server about its model and context ...")
        base, key, models, kind = self.server

        def work():
            online = models is not None
            loaded = self.m.servers.loaded_model(base, key, kind) if online and models and len(models) > 1 else None
            real = self.m.servers.context(base, key, kind, self.a.model or loaded) if online and kind else None
            deps = subprocess.run([sys.executable, "-c", self.m.CHECK_IMPORTS],
                                         capture_output=True).returncode != 0
            return loaded, real, deps

        self.bg(work, lambda r: self.model_fill(b, r))

    def model_fill(self, b, r):
        if self.failed(r):
            r = (None, None, True)
        self.busy()
        loaded, real, deps_missing = r
        m, cur = self.m, self.cur
        base, key, models, kind = self.server
        label = m.servers.LABELS.get(kind, kind) if kind else ""
        self.h_sub.configure(text=f"Server: {base}" + (f"  ({label})" if label else "") +
                             ("" if models is not None else "  - not running now, checked when maxedcode starts"))
        if self.note:
            ttk.Label(b, text=self.note, style="Warn.TLabel", wraplength=self.px(600), justify="left").pack(
                anchor="w", pady=(0, self.px(10)))
        grid = ttk.Frame(b)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.auto_model = not self.a.model and (kind == "strata" or (kind == "llamacpp" and models and
                                                                       len(models) == 1))
        ttk.Label(grid, text="Model", style="Bold.TLabel").grid(row=0, column=0, sticky="w", pady=self.px(6))
        self.v_model = tk.StringVar()
        if self.auto_model:
            now = f" (now: {models[0]})" if models and len(models) == 1 else ""
            ttk.Label(grid, text=f"auto - the one loaded in {label}{now}; switch it there, no setup needed").grid(
                row=0, column=1, sticky="w", padx=(self.px(14), 0))
        else:
            was = cur.get("model") or "local"
            pref = self.a.model or (was if models and was in models else loaded if models and loaded in models else
                                    models[0] if models else was)
            self.v_model.set(pref)
            ttk.Combobox(grid, textvariable=self.v_model, values=list(models or [])).grid(
                row=0, column=1, sticky="we", padx=(self.px(14), 0))
            if not models:
                ttk.Label(grid, text="as it is called on the server; LM Studio / llama.cpp accept 'local'",
                          style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=(self.px(14), 0))
        self.auto_ctx = not self.a.ctx and kind in ("strata", "llamacpp")
        ttk.Label(grid, text="Context", style="Bold.TLabel").grid(row=2, column=0, sticky="w", pady=self.px(6))
        best = self.a.ctx or real or cur.get("ctx") or 32768
        self.v_ctx = tk.StringVar(value=str(best))
        if self.auto_ctx:
            ttk.Label(grid, text=f"auto - read from {label} at every start" + (f" (now {real} tokens)" if real else
                                                                                "")).grid(
                row=2, column=1, sticky="w", padx=(self.px(14), 0))
        else:
            sizes = sorted(set(m.CTX_SIZES) | {best})
            ttk.Combobox(grid, textvariable=self.v_ctx, values=[str(s) for s in sizes], width=12).grid(
                row=2, column=1, sticky="w", padx=(self.px(14), 0))
            hint = "tokens; reported by the server" if real else \
                "tokens; Ollama: the size it should use (set on the server)" if kind == "ollama" else "tokens"
            ttk.Label(grid, text=hint, style="Muted.TLabel").grid(row=3, column=1, sticky="w",
                                                                  padx=(self.px(14), 0))
        self.v_ollama = tk.BooleanVar(value=False)
        tips = m.servers.advice(kind, real, best) if models is not None else []
        if tips:
            tipbox = ttk.LabelFrame(b, text=" Prompt cache ", padding=self.px(10))
            tipbox.pack(fill="x", pady=(self.px(14), 0))
            for t in tips:
                ttk.Label(tipbox, text="• " + t, style="Warn.TLabel", wraplength=self.px(560),
                          justify="left").pack(anchor="w")
            if kind == "ollama" and m.servers.is_local(m.split_addr(base)[1]):
                self.v_ollama.set(True)
                ttk.Checkbutton(tipbox, variable=self.v_ollama,
                                text="Set OLLAMA_CONTEXT_LENGTH and OLLAMA_KEEP_ALIVE=30m for my user").pack(
                    anchor="w", pady=(self.px(6), 0))
        self.opts_v = {}
        rows = []
        if not self.a.no_deps and (not self.configure or deps_missing):
            rows.append(("deps", "Install / update the Python packages (needs internet, 1-2 minutes)", True))
        if not self.configure and not self.a.no_shortcut:
            rows.append(("link", "Shortcuts on the desktop and in Start: maxedcode app and maxedcode CLI"
                         if self.m.WINDOWS else self.m.option_label("link"), True))
        if not self.configure and not self.a.no_path:
            rows.append(("path", "Type maxed in any CMD / PowerShell window (adds program\\bin to PATH)"
                         if self.m.WINDOWS else self.m.option_label("path"), True))
        if models is not None:
            rows.append(("test", "Send a short test request to the model (loads it now)", False))
        if rows:
            ttk.Label(b, text="Also", style="Bold.TLabel").pack(anchor="w", pady=(self.px(18), self.px(4)))
        for k, text, on in rows:
            v = tk.BooleanVar(value=on)
            self.opts_v[k] = v
            ttk.Checkbutton(b, text=text, variable=v).pack(anchor="w", pady=self.px(2))
        self.can[self.b_next] = True
        self.b_next.configure(text="Save" if self.configure else "Install", command=self.start_install,
                              state="normal")

    # ---------- 4. install ----------

    def start_install(self):
        base, key, models, kind = self.server
        model = None if self.auto_model else (self.v_model.get().strip() or "local")
        try:
            ctx = int(self.v_ctx.get().strip())
            if ctx < 2048:
                raise ValueError
        except ValueError:
            self.say_status("context: a number of tokens, at least 2048 (e.g. 32768)", "err")
            return
        want = {k: v.get() for k, v in self.opts_v.items()}
        want["ollama_env"] = bool(self.v_ollama.get())
        n = len(self.steps) - 1
        b = self.page(n, "Saving ..." if self.configure else "Installing ...",
                      "This takes a minute or two; the Python packages come from the internet.")
        self.b_cancel.configure(state="disabled")
        self.text = tk.Text(b, height=12, wrap="word", font=self.f_mono, relief="flat", bg="#f8fafc",
                            padx=self.px(10), pady=self.px(8), borderwidth=0, highlightthickness=1,
                            highlightbackground="#e5e7eb")
        sb = ttk.Scrollbar(b, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set, state="disabled")
        for tag, color in (("ok", OK), ("warn", WARN), ("err", ERR), ("note", MUTED)):
            self.text.tag_configure(tag, foreground=color)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.busy("working ...")
        self.live = True

        def work():
            self.m.run_install(self.dest, self.cur, base, key, models, kind, model, ctx, want)
            self.m.finish_hints(self.dest, models, kind, want)

        self.want = want
        self.bg(work, self.finished)

    def append(self, msg, kind):
        if not hasattr(self, "text"):
            return
        sym = {"ok": MARK + " ", "warn": "! ", "err": CROSS + " "}.get(kind, "   ")
        self.text.configure(state="normal")
        self.text.insert("end", sym + msg + "\n", kind or "note")
        self.text.see("end")
        self.text.configure(state="disabled")
        if kind != "":
            self.say_status(msg[:120], kind)

    def finished(self, r):
        if isinstance(r, Exception):
            self.m.say(f"setup stopped: {type(r).__name__}: {r}", "err")
            self.pump_now()
        self.busy()
        self.done = True
        errors = self.m.UI["errors"]
        root = self.dest.parent
        if errors:
            self.h_title.configure(text="Setup finished with problems")
            self.h_sub.configure(text=f"See the red lines below. The whole log: {self.m.LOG}")
            self.say_status(errors[-1][:140], "err")
        else:
            self.h_title.configure(text="Saved" if self.configure else "maxedcode is installed")
            want = getattr(self, "want", {}) or {}
            if self.m.WINDOWS:
                how = ("\"maxedcode app\" (window) and \"maxedcode CLI\" on the desktop and in Start" if want.get("link")
                       else "start it with maxedcode app.bat (window) or maxedcode CLI.bat in that folder")
            else:
                how = "maxed app (window) or maxed (terminal)"
                if want.get("link"):
                    how += ", maxedcode in Launchpad" if self.m.MACOS else ", maxedcode in the app menu"
            how += ", maxed in a new terminal" if want.get("path") else ""
            self.h_sub.configure(text=f"In {root}: {how}. Change the model server later: Settings - maxedcode in "
                                      "the app, or maxed configure." if not self.configure else
                                 "maxedcode uses the new server from its next start.")
            self.say_status("done", "ok")
        for i, lb in enumerate(self.step_labels):
            lb.configure(fg=SIDE_FG, text=MARK + "  " + self.steps[i])
        self.b_back.configure(state="disabled")
        self.b_cancel.configure(state="normal", text="Close")
        app = root / "maxedcode app.bat" if self.m.WINDOWS else self.dest / "bin" / "maxed"
        if not self.configure and app.exists() and not errors:
            self.b_next.configure(text="Open maxedcode", state="normal", command=lambda: self.open_app(app))
            self.b_next.focus_set()
        elif errors:
            self.b_next.configure(text="Open the log", state="normal", command=lambda: self.start(self.m.LOG))
        else:
            self.b_next.configure(text="Close", state="normal", command=self.quit)

    def pump_now(self):
        with suppress(queue.Empty):
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.append(item[1], item[2])

    def start(self, path):
        with suppress(OSError):
            if os.name == "nt":
                os.startfile(str(path))
            else:                    # Linux / macOS: the default program for a file (the log)
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def open_app(self, app):
        if self.m.WINDOWS:
            self.start(app)
        else:                        # program/bin/maxed app, on its own after setup closes
            with suppress(OSError):
                subprocess.Popen([str(app), "app"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(Path.home()))
        self.root.after(400, self.quit)
