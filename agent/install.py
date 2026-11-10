"""maxedcode installer: install folder, where the model runs (Strata on this PC by default / another local server /
another PC on the network by IP / custom URL / cloud), model and context, config.json, Python packages, shortcut
and PATH (maxed from any folder).

  python install.py                     interactive wizard: arrow keys + Enter (Maxedcode_setup.exe from the setup zip runs it)
  python install.py --configure         only change server/model (config.json next to this file)
  python install.py --yes               no questions: Strata on this PC (http://127.0.0.1:8080/v1), model and context auto
  python install.py --yes --dir D:\\Maxedcode --base-url 192.168.0.10:1234 --model qwen3   no questions, another server

The model server does not have to be running during setup: the address is saved and checked when the agent starts.
The server address can be typed in any form: 192.168.0.10, 192.168.0.10:1234, http://pc-name:8080,
https://api.example.com/v1, .../v1/chat/completions; the installer finds the port and /v1 itself.
"""
import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from maxedcode import servers  # noqa: E402  (stdlib only: works before the packages are installed)

# the program: <root>\program; root has only the two launchers and README.txt. The data (sessions, memory,
# settings.json) is in DATA_DIR, as always: an update or another install folder never touches it.
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "maxedcode"
DATA_DIR = Path.home() / ".maxedcode"
LEGACY_DIR = DATA_DIR             # older setups put the program here too, next to the data
# the app window's files: next to this file in an install / the setup zip, ../desktop in the git repository
SRC_DESKTOP = HERE / "desktop" if (HERE / "desktop" / "app").is_dir() else HERE.parent / "desktop"
DEPS = ["openai>=2.0", "ddgs>=3.0", "textual>=2.0", "mcp>=2", "pillow", "rich"]
CHECK_IMPORTS = "import openai, ddgs, textual, rich, PIL, mcp"
PORTS = {8080: "Strata / llama.cpp", 1234: "LM Studio", 11434: "Ollama", 8000: "vLLM", 5001: "KoboldCpp",
         5000: "text-generation-webui"}
# cloud APIs: label -> OpenAI-compatible base URL (servers.CLOUD: OpenAI, Claude, Gemini, DeepSeek, Groq ...)
CLOUD = {label: url for label, (_, url) in servers.CLOUD.items()}
CLOUD_HINT = "OpenAI, Claude, Gemini, DeepSeek, Groq, Mistral, xAI, OpenRouter ..."
STRATA_URL = "http://127.0.0.1:8080/v1"
STRATA_HOME = "https://github.com/Niko1221/Strata/releases"
# user settings.json written on a first install with Strata (an existing one is never touched):
# sampling profiles of the models Strata serves, matched by the model id the agent reads from the server
STRATA_SETTINGS = {
    "max_turns": 30,
    "model_settings": {
        "qwen": {"edit_format": "exact", "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "max_turns": 30},
        "deepseek": {"edit_format": "udiff", "temperature": 0.3, "top_k": 20, "max_turns": 30},
    },
}
CTX_SIZES = [8192, 16384, 32768, 65536, 131072]
UA = {"User-Agent": "maxedcode-installer"}
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ARGS = argparse.Namespace(yes=False)
# vt: arrow-key menus; sink: the setup window's log (install_gui); errors: what went wrong, for the last screen
UI = {"vt": False, "sink": None, "errors": []}
# everything setup says also goes here: the first thing to ask for when it fails on another PC
LOG = Path(os.environ.get("TEMP") or os.environ.get("TMP") or Path.home()) / "maxedcode-setup.log"
ESC = "\x1b["
PAGE = 10   # rows of a long list shown at once


# ---------- console UI: arrow-key menus; numbered questions when the console cannot do that ----------

def vt_on():
    """Interactive Windows console with ANSI escapes (Windows 10+) -> True: arrow-key menus."""
    if not (msvcrt and sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    with suppress(Exception):
        import ctypes
        k = ctypes.windll.kernel32
        h, mode = k.GetStdHandle(-11), ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            return bool(k.SetConsoleMode(h, mode.value | 4))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return False


def c(text, code):
    """Colored text (only when the console understands colors)."""
    return f"{ESC}{code}m{text}{ESC}0m" if UI["vt"] and code else text


def log(line):
    """One line into the setup log file (never fails)."""
    with suppress(OSError), open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {line}\n")


def say(msg, kind=""):
    """Status line: ok / warn / err / plain note; also into the log file and the setup window."""
    sym, code = {"ok": ("+", "32;1"), "warn": ("!", "33;1"), "err": ("x", "31;1")}.get(kind, (" ", ""))
    log(f"{sym} {msg}")
    if kind == "err":
        UI["errors"].append(str(msg))
    if UI["sink"]:
        UI["sink"](str(msg), kind)
        return
    print(f"  {c(sym, code)} {msg if kind else c(msg, '90')}")


def section(title):
    print("\n" + c(title, "36;1"))


def fit(*parts):
    """One colored line cut to the console width: a wrapped line would break the redraw of a menu."""
    room, out = shutil.get_terminal_size((100, 30)).columns - 1, []
    for text, code in parts:
        text = text[:max(0, room)]
        room -= len(text)
        out.append(c(text, code))
    return "".join(out)


KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "I": "pgup", "Q": "pgdn"}


def key():
    """One key press: 'up', 'down', 'enter', 'esc', 'space', 'back', ... or the typed character."""
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return KEYS.get(msvcrt.getwch(), "")
    if ch == "\x03":
        raise KeyboardInterrupt
    return {"\r": "enter", "\n": "enter", "\x1b": "esc", "\x08": "back", " ": "space", "\t": "down"}.get(ch, ch)


def numbered(q, rows, default, back):
    """Fallback without arrow keys: numbered list, the number (or the exact label) is typed."""
    print(f"\n{q}")
    for i, (label, hint) in enumerate(rows, 1):
        print(f"   {i}. {label}{'  - ' + hint if hint else ''}")
    while True:
        a = ask("Number" + (" (0 = back)" if back else ""), str(default + 1))
        if back and a == "0":
            return None
        if a.isdigit() and 1 <= int(a) <= len(rows):
            return int(a) - 1
        same = [i for i, (label, _) in enumerate(rows) if a.lower() == label.lower()]
        if same:
            return same[0]
        print(f"  enter a number from 1 to {len(rows)}")


def select(q, items, default=0, back=False, marks=None, hot=None):
    """Arrow-key list. items: [label | (label, hint)]. marks: checkbox states -> multi-select (Space toggles).

    -> index of the chosen row (None after Esc when back=True), or the list of marks. With --yes: the default.
    A long list filters as you type; short lists also take the row number or a hot key."""
    rows = [(i, "") if isinstance(i, str) else i for i in items]
    cur = max(0, min(default, len(rows) - 1))
    if ARGS.yes:
        return marks if marks is not None else cur
    if not UI["vt"]:
        if marks is None:
            return numbered(q, rows, cur, back)
        return [yes(label, m) for (label, _), m in zip(rows, marks)]
    filt = len(rows) > 9 and marks is None
    tips = "Up/Down move, " + ("Space toggle, Enter confirm" if marks is not None else "Enter select") + \
        (", type to filter" if filt else "") + (", Esc back" if back else "")
    query, top, drawn, out = "", 0, 0, sys.stdout
    out.write(f"{ESC}?25l")
    try:
        while True:
            view = [i for i, (lb, h) in enumerate(rows) if query.lower() in f"{lb} {h}".lower()]
            pos = view.index(cur) if cur in view else 0
            if view:
                cur = view[pos]
            top = min(max(top, pos - PAGE + 1), pos)
            lines = [fit(("? ", "32;1"), (q, "1"), ("   " + tips, "90"))]
            if filt:
                lines.append(fit(("  filter: ", "90"), (query + "_", "36")))
            for i in view[top:top + PAGE]:
                label, hint = rows[i]
                box = ("[x] " if marks[i] else "[ ] ") if marks is not None else ""
                on = i == cur
                lines.append(fit(("  > " if on else "    ", "36;1"), (box + label, "36;1" if on else ""),
                                 (f"  {hint}" if hint else "", "90")))
            if not view:
                lines.append(fit(("    nothing matches; Backspace to edit the filter", "33")))
            elif len(view) > PAGE:
                lines.append(fit((f"    {pos + 1} of {len(view)}", "90")))
            out.write((f"{ESC}{drawn}F" if drawn else "") + f"{ESC}J" + "\n".join(lines) + "\n")
            out.flush()
            drawn = len(lines)
            k = key()
            if k in ("up", "down", "left", "right", "home", "end", "pgup", "pgdn") and view:
                n = len(view)
                pos = {"up": pos - 1, "left": pos - 1, "down": pos + 1, "right": pos + 1, "home": 0, "end": n - 1,
                       "pgup": max(0, pos - PAGE), "pgdn": min(n - 1, pos + PAGE)}[k] % n
                cur = view[pos]
            elif k == "space" and marks is not None:
                marks[cur] = not marks[cur]
            elif k == "enter" and view:
                break
            elif k == "esc" and filt and query:
                query = ""
            elif k == "esc" and back:
                cur = None
                break
            elif filt and k == "back":
                query = query[:-1]
            elif filt and len(k) == 1 and k.isprintable():
                query += k
            elif marks is None and hot and k.lower() in hot:
                cur = hot[k.lower()]
                break
            elif marks is None and len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(rows):
                cur = int(k) - 1
                break
    finally:
        out.write((f"{ESC}{drawn}F" if drawn else "") + f"{ESC}J{ESC}?25h")
        out.flush()
    if marks is not None:
        picked = ", ".join(rows[i][0] for i, m in enumerate(marks) if m) or "nothing"
        print(fit(("+ ", "32;1"), (q, "1"), ("  " + picked, "36")))
        return marks
    if cur is not None:
        print(fit(("+ ", "32;1"), (q, "1"), ("  " + rows[cur][0], "36")))
    return cur


def ask(q, default="", shown=None):
    """Text question with a default answer (Enter). With --yes returns the default. shown: how to display it."""
    if ARGS.yes:
        return default
    hint = shown if shown is not None else default
    prompt = (c("? ", "32;1") + c(q, "1") + (c(f" ({hint})", "90") if hint else "") + " ") if UI["vt"] else \
        f"{q}{f' [{hint}]' if hint else ''}: "
    try:
        a = input(prompt).strip()
    except EOFError:
        return default
    return a or default


def yes(q, default=True):
    """Yes/no: arrows, or the y / n key."""
    if UI["vt"] or ARGS.yes:
        return select(q, ["Yes", "No"], 0 if default else 1, hot={"y": 0, "n": 1}) == 0
    a = ask(f"{q} ({'Y/n' if default else 'y/N'})", "", "").lower()
    return default if not a else a[0] in "y1"


# ---------- network ----------

def is_local(host):
    """Address on the own network (localhost, 192.168.x.x, 10.x ...): reached without the system proxy."""
    if host in ("localhost", ""):
        return True
    with suppress(ValueError):
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    return "." not in host


def split_addr(addr):
    """'192.168.0.10:1234', 'http://host/v1/chat/completions' -> (scheme, host, port|None, path) or None."""
    return servers.split_addr(addr)


def candidates(addr, ports=None):
    """URLs to try: no port -> known ports (and 80), no path -> /v1 and the root."""
    return servers.candidates(addr, ports, PORTS)


def why(e):
    """Human-readable reason why the server did not answer."""
    e = getattr(e, "reason", e)
    if isinstance(e, ConnectionRefusedError):
        return "port closed: the server is not running or listens on 127.0.0.1 only"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "no answer: the computer is off, wrong IP, or a firewall blocks it"
    if isinstance(e, socket.gaierror):
        return "host name / address not found"
    if isinstance(e, json.JSONDecodeError):
        return "answers, but it is not an OpenAI-compatible API"
    return f"{type(e).__name__}: {e}"


def http(url, key="", body=None, timeout=4):
    """GET/POST JSON. Local addresses bypass the system proxy."""
    head = {**UA, **(servers.auth_headers(url, key) if key != "local" else {"Authorization": "Bearer local"}),
            **({"Content-Type": "application/json"} if body is not None else {})}
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=head)
    host = urllib.parse.urlsplit(url).hostname or ""
    with (NOPROXY.open if is_local(host) else urllib.request.urlopen)(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe(base, key="", timeout=4):
    """GET base/models -> (True, [models]) | (True, None): server is there but wants a key | (False, reason)."""
    try:
        data = http(base.rstrip("/") + "/models", key, timeout=timeout)
    except urllib.error.HTTPError as e:
        return (True, None) if e.code in (401, 403) else (False, f"HTTP {e.code}")
    except Exception as e:
        return False, why(e)
    items = data.get("data", data.get("models")) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False, "answers, but it is not an OpenAI-compatible API"
    return True, [m.get("id") or m.get("name") for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]


def probe_all(urls, key="", timeout=4):
    """Checks the URLs in parallel, keeping their order: [(url, ok, models|reason)]."""
    with cf.ThreadPoolExecutor(max(1, min(32, len(urls)))) as ex:
        return [(u, *r) for u, r in zip(urls, ex.map(lambda u: probe(u, key, timeout), urls))]


def hints(host):
    """What to check when the server does not answer."""
    say("What to check:")
    if host in ("localhost", "127.0.0.1", "::1"):
        say(" - is the model server running? Strata: strata launch (from the Strata folder: bin\\strata.cmd launch)")
        say("   LM Studio: Developer -> Start Server; Ollama: tray icon / ollama serve; llama.cpp: llama-server -m model.gguf")
        return
    say(f" - is the computer reachable?  ping {host}")
    say(" - the server on that computer listens on the network, not only on 127.0.0.1:")
    say("     Strata:    Serving -> allow other devices (port 8080)")
    say("     LM Studio: Developer -> Settings -> Serve on Local Network (port 1234)")
    say("     Ollama:    environment variable OLLAMA_HOST=0.0.0.0, then restart Ollama (port 11434)")
    say("     llama.cpp: llama-server --host 0.0.0.0 --port 8080")
    say(" - Windows Firewall on that computer allows incoming connections on that port")


def ask_key(key, need=False):
    """Asks for the API key; a saved key is never shown in full."""
    shown = "" if key in ("", "local") else "saved ..." + key[-4:]
    while True:
        k = ask("API key" + ("" if need else " (Enter = no key)"), key if shown else "", shown)
        if k or not need or ARGS.yes:
            return k or "local"
        say("this server requires a key", "warn")


def later(what, url):
    """Server did not answer: continue with its address (default) or go back. -> True = save it."""
    if ARGS.yes:
        say(f"saving {url}; the agent connects when {what} is running", "warn")
        return True
    n = select("What now?", [(f"Continue: save {url}", f"I will start {what} later"),
                             ("Check again", ""), ("Back", "choose another server")], 0)
    if n == 0:
        say(f"saved; when {what} is running just start maxedcode (change it later: maxed configure)")
    return {0: True, 1: None}.get(n, False)


def connect(addr, key, ports=None):
    """Finds a working URL for the typed address -> (url, key, models|None) or None (try another one).

    A server that does not answer can still be saved: it may simply not be started yet."""
    urls = candidates(addr, ports)
    if not urls:
        say(f"cannot parse address '{addr}'. Example: 192.168.0.10 or http://192.168.0.10:1234/v1", "err")
        return None
    while True:
        say(f"checking {addr} ...")
        res = probe_all(urls, key)
        for url, ok, models in res:
            for _ in range(3):
                if not ok or models is not None or ARGS.yes:
                    break
                say(f"{url} answers but asks for an API key (or the key is wrong)", "warn")
                key = ask_key("", need=True)
                ok, models = probe(url, key)
            if ok:
                say(f"server answers: {url}" + (f" ({len(models)} models)" if models else ""), "ok")
                return url, key, models
        say("no answer:", "warn")
        for reason in dict.fromkeys(r for _, _, r in res):
            say(f" - {reason}")
        hints(split_addr(addr)[1])
        go = later("the server", urls[0])
        if go is not None:
            return (urls[0], key, None) if go else None


def pick_found(found, key):
    """Choice among the servers found [(url, models)]."""
    if len(found) == 1:
        url, models = found[0]
        say(f"found: {url}" + (f" ({len(models)} models)" if models else ""), "ok")
        return url, key, models
    n = select("Several servers found", [(url, f"{PORTS.get(split_addr(url)[2], 'server')}, {len(models or [])} models")
                                         for url, models in found], 0, back=True)
    return None if n is None else (found[n][0], key, found[n][1])


def strata_installed():
    """Strata app / terminal on this PC (its install folder, its data folder or the strata command)."""
    return bool(shutil.which("strata")) or bool(servers.strata_source()) or (Path.home() / ".strata").is_dir() or \
        any(Path(os.environ.get(v, "-"), "Programs", "Strata").exists() for v in ("LOCALAPPDATA", "ProgramFiles"))


def strata_key():
    """API key the Strata app serves with (Serving -> API key); None = no key (the Strata default)."""
    with suppress(Exception):
        d = json.loads((Path.home() / ".strata" / "data" / "state.json").read_text(encoding="utf-8-sig"))
        k = (d.get("serving") or {}).get("apiKey")
        return str(k) if k else None
    return None


def pick_strata(key):
    """Strata on this PC: fixed address, model and context are taken from Strata when the agent starts.

    Setup does not need Strata running; it only tells how to start it."""
    key = strata_key() or key
    while True:
        ok, models = probe(STRATA_URL, key, 3)
        if ok and models is None and not ARGS.yes:
            say("Strata answers but asks for an API key (Strata app: Serving -> API key)", "warn")
            key = ask_key("", need=True)
            continue
        if ok:
            say(f"Strata answers: {STRATA_URL}" + (f" (model {models[0]})" if models and len(models) == 1 else ""), "ok")
            return STRATA_URL, key, models
        say(f"Strata is not answering on {STRATA_URL} yet", "warn")
        if strata_installed():
            say("start it: strata launch (in the Strata folder: bin\\strata.cmd launch)")
        else:
            say(f"Strata is not installed on this PC; get it here: {STRATA_HOME}")
        go = later("Strata", STRATA_URL)
        if go is not None:
            return (STRATA_URL, key, None) if go else None


def install_lmstudio():
    """Nothing is installed: LM Studio via winget (free, runs models locally)."""
    subprocess.run(["winget", "install", "-e", "--id", "ElementLabs.LMStudio",
                    "--accept-package-agreements", "--accept-source-agreements"])
    say("In LM Studio: download a model (Discover) and load it; then choose LM Studio here.")


def found_hint(x):
    """Menu hint of a detected server: what runs, or that it is installed and how it starts."""
    if x.get("need_key"):
        return "running, asks for an API key"
    if x.get("running"):
        models = x.get("models") or []
        what = x.get("loaded") or (models[0] if len(models) == 1 else f"{len(models)} models")
        return f"running: {what}" + (f", context {x['ctx']}" if x.get("ctx") else "")
    return "installed, not running - the address is saved, start it yourself later"


def use_found(x, key):
    """A server from the scan -> (url, key, models|None). Setup never starts a server: one that is not running
    is saved as is, and the user is told how to start it."""
    if x.get("running"):
        if x.get("need_key"):
            return connect(x["base"], ask_key("", need=True))
        say(f"{x['label']}: {x['base']}", "ok")
        return x["base"], key, x.get("models")
    say(f"saved {x['base']}; {x['label']} is not running now, maxedcode connects once it is", "ok")
    say(f"to start it: {x.get('how') or 'open ' + x['label'] + ' and start its server'}")
    return x["base"], key, None


def local_nets():
    """Own IPv4 /24 subnets (192.168.0.0/24 etc.)."""
    ips = set()
    with suppress(OSError):
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
    nets = set()
    for ip in ips:
        with suppress(ValueError):
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback and not a.is_link_local:
                nets.add(ipaddress.ip_network(f"{ip}/24", strict=False))
    return sorted(nets, key=str)


def port_open(ip, port, timeout=0.5):
    with suppress(OSError), socket.create_connection((ip, port), timeout):
        return True
    return False


def pick_lan(key):
    """Scans the own subnets on the known ports and lists the OpenAI-compatible servers found."""
    nets = local_nets()
    if not nets:
        say("no local network found; enter the IP manually", "warn")
        return None
    say(f"scanning {', '.join(map(str, nets))}, ports {', '.join(map(str, PORTS))} (5-15 s) ...")
    targets = [(str(ip), p) for n in nets for ip in n.hosts() for p in PORTS]
    with cf.ThreadPoolExecutor(256) as ex:
        opened = [t for t, ok in zip(targets, ex.map(lambda t: port_open(*t), targets)) if ok]
    urls = [f"http://{ip}:{p}/v1" for ip, p in opened]
    found = [(u, m) for u, ok, m in probe_all(urls, key) if ok] if urls else []
    if found:
        return pick_found(found, key)
    say("no model servers found on the network", "warn")
    hints("<IP of the model computer>")
    return None


def is_strata(url):
    return (url or "").rstrip("/") in (STRATA_URL, STRATA_URL.replace("127.0.0.1", "localhost"))


def same_url(a, b):
    """The same server whatever the spelling (localhost / 127.0.0.1, a trailing slash)."""
    def norm(u):
        return str(u or "").rstrip("/").replace("://localhost", "://127.0.0.1").lower()
    return norm(a) == norm(b)


def default_row(opts, had):
    """Enter on the first screen: the current server if it runs, else what runs (Strata first), else the current
    server (kept even when another one is only installed here), else what is installed (Strata first), else Strata
    (recommended)."""
    rows = [(i, x) for i, (what, x, _, _) in enumerate(opts) if what == "found"]
    tests = (lambda x: had and x.get("running") and same_url(had, x["base"]),
             lambda x: x.get("running") and x["kind"] == "strata", lambda x: x.get("running"),
             lambda x: had and same_url(had, x["base"]),
             lambda x: not had and x["kind"] == "strata", lambda x: not had)
    for test in tests:
        for i, x in rows:
            if test(x):
                return i
    # a saved server (maybe on another PC) wins over one that is only installed here: --yes updates kept switching
    for want in (("keep", "strata") if had else ("strata",)):
        for i, o in enumerate(opts):
            if o[0] == want:
                return i
    return 0


def choose_server(cur):
    """The main question: where the model runs. -> (base_url, api_key, models|None, kind).

    What runs or is installed on this PC (Strata, LM Studio, Ollama, llama.cpp, Jan, vLLM ...) is found first and
    offered at the top; a server that is installed but not running is started from here."""
    key = ARGS.api_key or cur.get("api_key") or "local"
    if ARGS.base_url:
        if ARGS.yes and (not candidates(ARGS.base_url) or re.search(r"\s", ARGS.base_url.strip())):
            sys.exit(f"--base-url {ARGS.base_url!r} is not a server address (e.g. 192.168.0.10:1234 or "
                     "http://host:8080/v1)")
        strata = bool(candidates(ARGS.base_url)) and is_strata(candidates(ARGS.base_url)[0])
        res = pick_strata(key) if strata else connect(ARGS.base_url, key)
        if res or ARGS.yes:
            res = res or (ARGS.base_url, key, None)
            kind = "strata" if strata else servers.kind_of(res[0], res[1]) if res[2] is not None else ""
            return (*res, kind)
    had = cur.get("base_url")
    while True:
        say("looking for model servers on this computer ...")
        found = servers.scan(strata_key() or key)
        section("Where does the model run?")
        opts = [("found", x, x["label"] + " on this computer", found_hint(x)) for x in found]
        if not any(x["kind"] == "strata" for x in found):
            opts.append(("strata", None, "Strata on this computer",
                         ("installed, not running" if strata_installed() else "not installed, get it later") +
                         "  (recommended)"))
        if had and not is_strata(had) and not any(same_url(had, x["base"]) for x in found):
            opts.append(("keep", None, "Keep the current server", had))
        if not found and shutil.which("winget") and not ARGS.yes:
            opts.append(("lmstudio", None, "Install LM Studio (free, runs models on this PC)", "winget"))
        opts += [("rescan", None, "Look again on this computer", "after starting a server"),
                 ("ip", None, "Another computer on the network", "enter its IP, e.g. 192.168.0.10"),
                 ("scan", None, "Scan the local network for servers", "5-15 s"),
                 ("url", None, "Enter the API URL", "http://... or https://..."),
                 ("cloud", None, "Cloud API", CLOUD_HINT)]
        n = select("Model server", [(label, hint) for _, _, label, hint in opts], default_row(opts, had))
        what, item = opts[n][0], opts[n][1]
        res, kind = None, ""
        if what == "found":
            res, kind = use_found(item, key), item["kind"]
        elif what == "strata":
            res, kind = pick_strata(key), "strata"
        elif what == "rescan":
            continue
        elif what == "lmstudio":
            install_lmstudio()
            continue
        elif what == "keep":
            res = connect(had, key)
        elif what == "ip":
            addr = ask("IP or name of the model computer (port optional: 192.168.0.10:8080)")
            p = split_addr(addr) if addr else None
            ports = None
            if p and not p[2] and not p[3]:
                port = ask("Server port (Enter = try " + ", ".join(f"{k} {v}" for k, v in PORTS.items()) + ")")
                ports = [int(port)] if port.isdigit() else None
            res = connect(addr, key, ports) if addr else None
        elif what == "scan":
            res = pick_lan(key)
        elif what == "url":
            addr = ask("API URL (e.g. http://192.168.0.10:8080/v1 or https://api.example.com/v1)")
            res = connect(addr, ask_key(key)) if addr else None
        elif what == "cloud":
            url = pick_cloud(had)
            if not url:
                continue
            res, kind = connect(url, ask_key(key if had and same_url(had, url) else "", need=True)), servers.cloud_kind(url)
        if res:
            if not kind and res[2] is not None:
                kind = servers.kind_of(res[0], res[1])
            if kind:
                say(f"server type: {servers.LABELS.get(kind, kind)}", "ok")
            return (*res, kind)
        if ARGS.yes:
            return STRATA_URL, key, None, "strata"


def pick_cloud(had):
    """Cloud API provider -> its base URL, or None (Esc: back to the server list)."""
    names = list(CLOUD)
    now = next((i for i, n in enumerate(names) if had and same_url(had, CLOUD[n])), 0)
    n = select("Cloud API (an API key from the provider's site is needed)",
               [(name, CLOUD[name]) for name in names], now, back=True)
    return None if n is None else CLOUD[names[n]]


def choose_model(models, cur, kind="", base="", key=""):
    """Model: from the server list, otherwise typed by hand. None = not pinned: the agent takes the server's model.

    Strata and a one-model llama.cpp: not pinned, switching the model there needs no setup. LM Studio and
    Ollama list every downloaded model and load whichever a request names (the cache is lost): pinned, the
    loaded one is the default."""
    if ARGS.model:
        return ARGS.model
    if kind == "strata" or (kind == "llamacpp" and models and len(models) == 1):
        now = f" (now: {models[0]})" if models and len(models) == 1 else ""
        say(f"model: auto - the one loaded in {servers.LABELS[kind]}{now}; switch it there, no setup needed", "ok")
        return None
    was = cur.get("model") or "local"
    if not models:
        return ask("Model name (as on the server; LM Studio / llama.cpp accept 'local')", was)
    if len(models) == 1:
        say(f"model: {models[0]}", "ok")
        return models[0]
    loaded = servers.loaded_model(base, key, kind) if base else None
    pref = was if was in models else loaded if loaded in models else models[0]
    if ARGS.yes:
        return pref
    rows = [(m, ", ".join(x for x in ("loaded" if m == loaded else "", "current" if m == was else "") if x))
            for m in models] + [("Type another name ...", "")]
    n = select("Model", rows, models.index(pref))
    return models[n] if n < len(models) else ask("Model name", was)


def tune_server(kind, base, key, model, ctx):
    """What the server needs so the prompt cache holds as on Strata: a window as large as the agent uses, the
    model kept loaded. Ollama is fixed from here (user environment variables); for the others setup says how."""
    real = servers.context(base, key, kind, model) if kind else None
    tips = servers.advice(kind, real, ctx)
    if not tips:
        return
    section("Prompt cache")
    for tip in tips:
        say(tip, "warn")
    if kind == "ollama" and servers.is_local(split_addr(base)[1]) and \
            yes(f"Set OLLAMA_CONTEXT_LENGTH={ctx} and OLLAMA_KEEP_ALIVE=30m for your user?", True):
        try:
            servers.set_user_env({"OLLAMA_CONTEXT_LENGTH": ctx, "OLLAMA_KEEP_ALIVE": "30m"})
            say("set; restart Ollama (tray icon -> Quit Ollama, then start it again) to apply", "ok")
        except OSError as e:
            say(f"not set: {e}", "warn")


def choose_ctx(base, key, model, cur, kind="", online=True):
    """Context size: the server's value if it reports one, otherwise the previous value / 32768.

    For Strata and llama.cpp nothing is asked: the agent reads the real window at every start, the saved
    number is only the fallback while the server is not reachable."""
    if ARGS.ctx:
        return ARGS.ctx
    n = servers.context(base, key, kind, model) if online else None
    if kind in ("strata", "llamacpp"):
        say(f"context: auto - read from {servers.LABELS[kind]} at every start{f' (now {n} tokens)' if n else ''}", "ok")
        return n or cur.get("ctx") or 32768
    if n:
        say(f"context allocated by the server: {n} tokens", "ok")
    elif kind == "ollama":
        say("Ollama: the context is set on the server (OLLAMA_CONTEXT_LENGTH); choose the size it should use")
    best = n or cur.get("ctx") or 32768
    sizes = sorted(set(CTX_SIZES) | {best})
    rows = [(f"{s} tokens", "reported by the server" if s == n else "current" if s == cur.get("ctx") else "")
            for s in sizes] + [("Other ...", "")]
    i = select("Model context size", rows, sizes.index(best))
    if i < len(sizes):
        return sizes[i]
    while True:
        a = ask("Context size in tokens", str(best))
        if a.isdigit() and int(a) >= 2048:
            return int(a)
        say("enter a number of at least 2048, e.g. 32768", "warn")


def chat_test(base, key, model):
    """A short request to the model: checks that the model name is right and the server really generates."""
    say("waiting for the model (the first request may load the model, up to 2 minutes) ...")
    try:
        http(base.rstrip("/") + "/chat/completions", key,
             {"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16}, timeout=120)
        say("the model answers", "ok")
    except urllib.error.HTTPError as e:
        say(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}", "err")
        say("most often: wrong model name, or the model is not loaded on the server")
    except Exception as e:
        say(why(e), "err")


# ---------- files ----------

def load_cur(dest):
    """Current settings: config.json, and settings.bat of old installs (AGENT_BASE_URL, AGENT_MODEL)."""
    cur = {}
    if not (dest / "config.json").exists():     # an old-layout install: its settings come along
        dest = next((d for d in legacy_installs(dest) if (d / "config.json").exists()), dest)
    with suppress(Exception):
        cur = json.loads((dest / "config.json").read_text(encoding="utf-8"))
    with suppress(OSError):
        for k, v in re.findall(r'set "AGENT_(\w+)=([^"]*)"', (dest / "settings.bat").read_text(encoding="utf-8", errors="replace")):
            if k in ("BASE_URL", "MODEL", "CTX", "HOME"):
                cur.setdefault(k.lower(), int(v) if k == "CTX" and v.isdigit() else v)
    if cur.get("base_url"):
        say(f"current settings: {cur['base_url']}  model {cur.get('model', 'auto')}  context {cur.get('ctx', 32768)}")
    return cur


def choose_dir():
    """Install folder: --dir (or the default one) is the first answer, another one can be typed; --yes takes it."""
    first = Path(ARGS.dir).expanduser().resolve() if ARGS.dir else DEFAULT_DIR
    if ARGS.yes:
        return first
    section("Install folder")
    n = select("Install to", [(str(first), ("found: updates it; " if (first / PROGRAM / "install.py").exists() else
                                            "default; " if first == DEFAULT_DIR else "") +
                               f"chats and settings stay in {DATA_DIR}"),
                              ("Another folder ...", "")], 0)
    path = str(first) if n == 0 else ask("Folder", str(first))
    root = Path(path).expanduser().resolve()
    say(f"the program goes to {root / PROGRAM}; maxedcode app.bat and maxedcode CLI.bat to {root}")
    return root


def copy_files(dest):
    """Puts the agent files into dest. Data (sessions, memory, settings.json, config.json) is left alone."""
    if dest.resolve() == HERE:
        return
    say(f"copying files to {dest} ...")
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("local_agent.py", "system_prompt.md", "install.py", "install_gui.py"):
        if (HERE / f).exists():
            shutil.copy2(HERE / f, dest / f)
    pkg = dest / "maxedcode"
    if (pkg / "config.py").exists():
        shutil.rmtree(pkg, ignore_errors=True)   # no stale modules from an older version
    shutil.copytree(HERE / "maxedcode", pkg, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (SRC_DESKTOP / "app").is_dir():               # the app window (see app_window)
        try:
            shutil.copytree(SRC_DESKTOP, dest / "desktop", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "maxedcode.exe", "python.txt"))
        except shutil.Error as e:     # an open app window holds desktop\app\lib\*.dll: the rest of setup still runs
            say(f"some window files are in use ({len(e.args[0])}): close the maxedcode app and run setup again", "warn")
        with suppress(OSError):
            (dest / "desktop" / "app" / "maxedcode.exe").unlink()   # an older install's exe: app_window builds it anew
    say("program files copied", "ok")


WEBVIEW2_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"     # the Evergreen WebView2 Runtime in EdgeUpdate
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"   # its small bootstrapper from Microsoft


def webview2_ready():
    """The WebView2 Runtime the app window draws with: in Windows 11, often missing on a fresh Windows 10."""
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}")]
    for hive, path in keys:
        with suppress(OSError), winreg.OpenKey(hive, path) as k:
            if str(winreg.QueryValueEx(k, "pv")[0]) not in ("", "0.0.0.0"):
                return True
    return False


def install_webview2():
    """No WebView2 Runtime: winget, else Microsoft's bootstrapper (installs for this user without admin rights)."""
    say("the app window needs Microsoft Edge WebView2 Runtime: installing it ...")
    if shutil.which("winget"):
        run_logged(["winget", "install", "-e", "--id", "Microsoft.EdgeWebView2Runtime", "--source", "winget",
                    "--accept-package-agreements", "--accept-source-agreements"])
    if not webview2_ready():
        exe = Path(os.environ.get("TEMP") or Path.home()) / "MicrosoftEdgeWebview2Setup.exe"
        try:
            req = urllib.request.Request(WEBVIEW2_URL, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                exe.write_bytes(r.read())
            subprocess.run([str(exe), "/silent", "/install"], timeout=600)
        except (OSError, urllib.error.URLError, subprocess.SubprocessError) as e:
            say(f"WebView2 download / install failed: {e}")
        with suppress(OSError):
            exe.unlink()
    if webview2_ready():
        say("WebView2 Runtime installed", "ok")
    else:
        say("WebView2 Runtime is missing, the app window will not open: install it from "
            "https://developer.microsoft.com/microsoft-edge/webview2/ (the CLI works without it)", "err")


def app_window(dest, shortcuts):
    """The app window: maxedcode.exe, built and signed on this PC by desktop/app/build-exe.ps1 (Smart App Control
    blocks an unsigned exe; the script checks that this one runs). When that fails, desktop/app/maxedcode.ps1, which
    compiles the same C# in memory at start. Setup checks the window and, if asked, creates the "maxedcode app"
    shortcuts on the desktop and in the Start menu. The window starts the agent with the Python of this install."""
    app = dest / "desktop" / "app"
    if not (app / "maxedcode.cs").exists():
        return
    if not webview2_ready():
        install_webview2()
    (app / "python.txt").write_text(sys.executable, encoding="utf-8")
    exe = app / "maxedcode.exe"
    if (app / "build-exe.ps1").exists():
        b = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(app / "build-exe.ps1")],
                           capture_output=True, text=True)
        say(f"app exe: {(b.stdout or b.stderr).strip()[:300]}", "ok" if b.returncode == 0 else "warn")
    r = None
    if exe.exists():
        with suppress(OSError):         # Windows refused to start it: the script window below
            r = subprocess.run([str(exe), "--shortcuts" if shortcuts else "--check"], capture_output=True, text=True)
    if r is None or r.returncode != 0:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(app / "maxedcode.ps1"),
                            "-Shortcuts" if shortcuts else "-Check"], capture_output=True, text=True)
    if r.returncode == 0:
        say("maxedcode app window ready" + (": \"maxedcode app\" on the desktop and in Start" if shortcuts else ""), "ok")
    else:
        say(f"app window not ready: {(r.stderr or r.stdout).strip()[:300]}", "warn")


def run_logged(cmd):
    """Runs a command, its output line by line into say() (console, setup window and log) -> exit code."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env)
    except OSError as e:
        say(f"{cmd[0]}: {e}")
        return 1
    for line in p.stdout:
        if line.strip():
            say(line.rstrip())
    return p.wait()


def install_deps():
    """pip install of the dependencies into the Python that runs the installer. -> True when they import."""
    say("installing Python packages (needs internet, 1-2 minutes) ...")
    py = sys.executable
    if subprocess.run([py, "-m", "pip", "--version"], capture_output=True).returncode:
        run_logged([py, "-m", "ensurepip", "--upgrade"])
    pip = [py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--no-input",
           "--progress-bar", "off", "-q"]
    if run_logged(pip + DEPS):
        say("trying again as a per-user install (--user) ...")
        run_logged(pip + ["--user"] + DEPS)
    r = subprocess.run([py, "-c", CHECK_IMPORTS], capture_output=True, text=True)
    if r.returncode == 0:
        say("Python packages installed", "ok")
        return True
    say(f"Python packages are missing: {(r.stderr.strip().splitlines() or ['?'])[-1]}", "err")
    say("check the internet connection (a proxy? set HTTPS_PROXY) and run setup again; the details are in "
        f"{LOG}")
    return False


def write_config(dest, cur, base, key, model, ctx, kind=""):
    """config.json (other keys are kept; model None = taken from the server); an old settings.bat is renamed,
    otherwise it would override config.json. "server": the kind, so a run does not have to find it out."""
    cfg = {**cur, "base_url": base, "api_key": key, "model": model, "ctx": ctx, "server": kind}
    for k in ("model", "server"):
        if not cfg[k]:
            cfg.pop(k)
    legacy = dest / "settings.bat"
    if legacy.exists():
        if "home" not in cfg and (Path.home() / ".local-agent").is_dir():
            cfg["home"] = str(Path.home() / ".local-agent")   # history of old installs started via the old launcher
        with suppress(OSError):
            legacy.replace(dest / "settings.bat.old")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    say(f"settings saved: {dest / 'config.json'}", "ok")


def write_strata_settings():
    """First install with Strata: user settings.json with the model profiles (never overwrites an existing one)."""
    f = Path.home() / ".maxedcode" / "settings.json"
    if f.exists():
        return
    with suppress(OSError):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(STRATA_SETTINGS, indent=2), encoding="utf-8")
        say(f"model profiles for Strata: {f}", "ok")


LAUNCH = r'''@echo off
chcp 65001 >nul
rem maxedcode CLI: the agent in a terminal window. Started from a CMD it works in that folder; double-clicked
rem (or from the shortcut / Start) it works in Documents\maxedcode. From any folder: type maxed.
set "PY={py}"
if not exist "%PY%" set "PY=python"
set "WD=%CD%"
if /i "%CD%\"=="%~dp0" set "WD=%USERPROFILE%\Documents\maxedcode"
if /i "%CD%"=="%SystemRoot%\System32" set "WD=%USERPROFILE%\Documents\maxedcode"
if not exist "%WD%" mkdir "%WD%"
where wt >nul 2>nul
if %errorlevel%==0 (
  start "" wt -w new --title "maxedcode" -d "%WD%" "%PY%" "%~dp0program\local_agent.py"
) else (
  start "maxedcode" /d "%WD%" cmd /k ""%PY%" "%~dp0program\local_agent.py""
)
'''
CONFIGURE = r'''@echo off
chcp 65001 >nul
title maxedcode - model server
set "PY={py}"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0..\install.py" --configure
if errorlevel 1 pause
'''
APP_LAUNCH = r'''@echo off
rem maxedcode app: the agent in its own window (chats, projects, a terminal and a browser).
rem Same as the "maxedcode app" shortcut; from any folder: maxed app
call "%~dp0program\desktop\maxedcode-app.bat" %*
'''
# launcher of old installs (Cyrillic name); if present it is turned into a call of maxedcode CLI.bat
OLD_LAUNCHER = "ЗАПУСТИТЬ.bat"


MAXED = r'''@echo off
rem maxed: the agent in the window you typed it in, working in the folder you are in.
rem   maxed                   the terminal agent        maxed --print "prompt"   headless
rem   maxed app               the app window            maxed configure          change the model server
rem   maxed shortcuts         make the desktop / Start shortcuts again
rem   maxed uninstall         remove this install (--purge: also chats and settings)
rem setlocal: PY does not leak into the caller's CMD; the caller's code page comes back on exit.
rem goto, not ( ) blocks: a ")" in the prompt would end a block.
setlocal
if /i "%~1"=="app" goto app
for /f "tokens=2 delims=:." %%c in ('chcp') do set "CP=%%c"
chcp 65001 >nul
set "PY={py}"
if not exist "%PY%" set "PY=python"
if /i "%~1"=="configure" goto configure
if /i "%~1"=="shortcuts" goto shortcuts
if /i "%~1"=="uninstall" goto uninstall
"%PY%" "%~dp0..\local_agent.py" %*
set "RC=%errorlevel%"
goto done
:configure
"%PY%" "%~dp0..\install.py" --configure
set "RC=%errorlevel%"
goto done
:shortcuts
"%PY%" "%~dp0..\install.py" --launchers --no-path
set "RC=%errorlevel%"
:done
if defined CP chcp %CP% >nul
exit /b %RC%
:app
call "%~dp0..\desktop\maxedcode-app.bat"
exit /b 0
:uninstall
rem This file is deleted while it runs: one line, and (goto) leaves it without reading it again (exit /b alone
rem printed "The system cannot find the path specified" twice).
"%PY%" "%~dp0..\install.py" --uninstall %2 %3 & chcp %CP% >nul & (goto) 2>nul & exit /b
'''

# double-click remover; one line, since maxed.bat deletes this file while it runs
UNINSTALL_BAT = '@"{py}" "%~dp0..\\install.py" --uninstall %* & pause & (goto) 2>nul' + chr(10)   # CR: write_text

README = """maxedcode
=========

Start it with one of the two files in this folder:

  maxedcode app.bat   the app: its own window with chats, projects, a terminal and a browser
  maxedcode CLI.bat   the CLI: the same agent in a terminal window

Everything else is in program\\ - there is nothing to open there.
Your chats, memory and settings are in {home} (kept when you install again or update).

SHORTCUTS
  Setup puts "maxedcode app" and "maxedcode CLI" on the desktop and in the Start menu.
  Deleted one? Any of these brings them back:
    - type  maxed shortcuts  in CMD or PowerShell
    - or double-click  program\\bin\\shortcuts.bat
    - or just start the .bat files above directly

FROM ANY FOLDER (setup adds program\\bin to PATH; open a NEW CMD or PowerShell window)
  maxed                   the CLI, working in the folder you are in
  maxed app               the app window
  maxed --print "prompt"  one answer without the UI (for scripts)
  maxed configure         change the model server / model
  maxed shortcuts         make the shortcuts again
  Without PATH:  "{root}\\program\\bin\\maxed.bat" app

THE MODEL SERVER
  maxedcode talks to a model server on this PC or the network (Strata, LM Studio, Ollama, llama.cpp ...).
  Change it: maxed configure, program\\bin\\configure.bat, or in the app: Settings - maxedcode.

BROWSER EXTENSION (the agent in your own Chrome / Edge)
  In the app: Settings - maxedcode - Browser: turn it on, then "Open the step-by-step guide" shows how to
  load it in Chrome / Edge, with the exact folder. The folder is:
    {root}\\program\\maxedcode\\chrome\\extension
  After an update click the round arrow on the extension card in chrome://extensions once.

UPDATE: run the installer again (same folder).
UNINSTALL: type  maxed uninstall  (or double-click program\\bin\\uninstall.bat). It removes this folder, its
  shortcuts, its PATH entry and the browser bridge; your chats and settings in {home} stay.
  maxed uninstall --purge  removes them too. Installed with npm? maxedcode uninstall  also removes the npm package.
"""

PROGRAM = "program"          # the folder with everything but the two launchers and README.txt
# launchers of older setups: replaced by the two launchers in the root and program\bin\maxed.bat
OLD_FILES = ("start.bat", "maxed.bat", "configure.bat")
OLD_LINKS = ("Maxedcode.lnk",)
SHORTCUTS = r'''@echo off
chcp 65001 >nul
rem Makes the "maxedcode app" and "maxedcode CLI" shortcuts (desktop and Start menu) again.
set "PY={py}"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0..\install.py" --launchers --no-path
pause
'''


def write_launchers(dest):
    """dest = <root>\\program. The root gets only the two launchers and README.txt; maxed.bat, configure.bat and
    shortcuts.bat live in program\\bin. Launchers of older setups are removed; an old Cyrillic launcher just calls
    maxedcode CLI.bat."""
    root = dest.parent
    files = {root / "maxedcode CLI.bat": LAUNCH, dest / "bin" / "maxed.bat": MAXED,
             dest / "bin" / "configure.bat": CONFIGURE, dest / "bin" / "shortcuts.bat": SHORTCUTS,
             dest / "bin" / "uninstall.bat": UNINSTALL_BAT,
             root / "README.txt": README.replace("{root}", str(root)).replace("{home}", str(DATA_DIR))}
    if (dest / "desktop" / "maxedcode-app.bat").exists():
        files[root / "maxedcode app.bat"] = APP_LAUNCH
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text.replace("{py}", sys.executable), encoding="utf-8", newline="\r\n")
    for name in OLD_FILES:
        for folder in (root, dest):
            with suppress(OSError):
                (folder / name).unlink()
    old = root / OLD_LAUNCHER
    if old.exists():
        old.write_text('@echo off\ncall "%~dp0maxedcode CLI.bat"\n', encoding="utf-8", newline="\r\n")


def shortcut(dest):
    """"maxedcode CLI" shortcuts (desktop and Start) -> <root>\\maxedcode CLI.bat; the old "Maxedcode" one is
    removed. The "maxedcode app" shortcuts are made by the window itself (app_window)."""
    root = dest.parent
    ps = ("foreach($f in 'Desktop','Programs'){"
          "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath($f)+'\\maxedcode CLI.lnk');"
          "$s.TargetPath=$env:MX_T;$s.WorkingDirectory=$env:MX_D;$s.Description='maxedcode in a terminal';"
          "$s.IconLocation=$env:SystemRoot+'\\System32\\shell32.dll,13';$s.Save()}")
    env = {**os.environ, "MX_T": str(root / "maxedcode CLI.bat"), "MX_D": str(root)}
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, capture_output=True, text=True)
    desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    for name in OLD_LINKS:
        with suppress(OSError):
            (desktop / name).unlink()
    if r.returncode == 0:
        say("\"maxedcode CLI\" shortcut is on the desktop and in Start", "ok")
    else:
        say(f"shortcut not created: {r.stderr.strip()[:200]}", "warn")


def _norm(part):
    return os.path.expandvars(part.strip().strip('"')).rstrip("\\").lower()


def path_contains(cur, dest):
    """True when the user PATH already has the folder (no duplicate add on a rerun)."""
    want = str(dest).rstrip("\\").lower()
    return any(_norm(part) == want for part in (cur or "").split(";"))


def path_with_bin(cur, bin_dir, stale=()):
    """The user PATH with bin_dir (where maxed.bat is) and without the stale folders (maxed.bat of older setups);
    None when nothing changes."""
    gone = {str(s).rstrip("\\").lower() for s in stale} - {str(bin_dir).rstrip("\\").lower()}
    parts = [p for p in (cur or "").split(";") if p.strip()]
    keep = [p for p in parts if _norm(p) not in gone]
    if not path_contains(cur, bin_dir):
        keep.append(str(bin_dir))
    return None if keep == parts else ";".join(keep)


def retire_old_launchers(bin_dir, path=None):
    """Older setups left maxed.cmd / maxed.bat in shared PATH folders (~\\.local\\bin ...). One found earlier in
    PATH wins over bin_dir\\maxed.bat and starts a program that is gone. Only maxedcode's own launchers (they call
    local_agent.py) are renamed to *.old; any other 'maxed' is left alone. -> the renamed files."""
    own = str(bin_dir).rstrip("\\").lower()
    done = []
    for part in (path if path is not None else os.environ.get("PATH", "")).split(";"):
        folder = _norm(part)
        if not folder or folder == own:
            continue
        for name in ("maxed.cmd", "maxed.bat"):
            f = Path(folder) / name
            with suppress(OSError):
                if f.is_file() and "local_agent.py" in f.read_text(encoding="utf-8", errors="replace"):
                    f.replace(f.with_name(name + ".old"))
                    done.append(f)
    for f in done:
        say(f"old launcher {f} renamed to {f.name}.old (it started an older setup)", "ok")
    return done


def add_to_path(dest, stale=()):
    """Put dest\\bin in the user PATH, so 'maxed' works from any folder; folders of older setups are taken out.
    Already open windows need a restart. winreg, not setx (cuts PATH at 1024 chars) and not [Environment]
    (expands %VARS% and saves PATH as REG_SZ)."""
    import ctypes
    import winreg
    # older setups put maxed.bat in the root; a shared root (--dir C:\tools) stays in PATH
    root = [dest.parent] if dest.parent.name.lower() == "maxedcode" or \
        any((dest.parent / f).exists() for f in ("maxed.bat", "local_agent.py")) else []
    stale = [*stale, dest, *root, dest.parent / "bin", LEGACY_DIR, LEGACY_DIR / "bin"]
    retire_old_launchers(dest / "bin")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            try:
                cur, kind = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                cur, kind = "", winreg.REG_EXPAND_SZ
            new = path_with_bin(cur, dest / "bin", stale)
            if new is None:
                say("maxed is already in PATH", "ok")
                return
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                kind = winreg.REG_EXPAND_SZ
            winreg.SetValueEx(k, "Path", 0, kind, new)
    except OSError as e:
        say(f"PATH not updated: {e}", "warn")
        return
    with suppress(Exception):   # WM_SETTINGCHANGE "Environment": Explorer picks up the new PATH for new windows
        ctypes.windll.user32.SendMessageTimeoutW(ctypes.c_void_p(0xFFFF), 0x1A, ctypes.c_void_p(0),
                                                 ctypes.c_wchar_p("Environment"), 2, 5000,
                                                 ctypes.byref(ctypes.c_size_t()))
    say(f"maxed works from any folder now: open a new CMD window and type maxed ({dest / 'bin'})", "ok")


# ---------- older setups: the program lay in ~\.maxedcode next to the data ----------

LEGACY_ITEMS = ("local_agent.py", "install.py", "system_prompt.md", "config.json", "README.txt", "maxedcode",
                "desktop", "bin", "tests", "run_all_tests.py", "run_all_tests.bat", "start.bat", "maxed.bat",
                "configure.bat", "maxedcode app.bat", "maxedcode CLI.bat", "settings.bat.old", OLD_LAUNCHER)


def legacy_installs(dest):
    """Old-layout installs to move out of the way: ~\\.maxedcode, or the chosen root itself, with the program at
    the top (local_agent.py next to maxedcode\\config.py)."""
    found = []
    for old in dict.fromkeys([LEGACY_DIR, dest.parent]):
        if old.resolve() != dest.resolve() and (old / "local_agent.py").exists() and \
                (old / "maxedcode" / "config.py").exists() and not is_checkout(old):
            found.append(old)
    return found


def browsers_using(folder):
    """True when a Chrome / Edge / Brave profile has the unpacked extension loaded from this folder."""
    want = json.dumps(str(folder), ensure_ascii=False).strip('"').lower()   # as the profile's JSON has it (raw UTF-8)
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for data in ("Google/Chrome/User Data", "Microsoft/Edge/User Data", "BraveSoftware/Brave-Browser/User Data",
                 "Chromium/User Data", "Yandex/YandexBrowser/User Data", "Vivaldi/User Data"):
        for prefs in (local / data).glob("*/Secure Preferences"):
            with suppress(OSError):
                if want in prefs.read_text(encoding="utf-8", errors="replace").lower():
                    return True
    return False


def move_legacy(old, dest):
    """Moves the program files of an old-layout install into <old>\\old-program (nothing is deleted; the data -
    sessions, memory, settings.json - stays). A browser that loads the extension from the old folder keeps
    working: that folder becomes a link to the new one."""
    ext = old / "maxedcode" / "chrome" / "extension"
    keep_ext = browsers_using(ext)
    backup = old / "old-program"
    if backup.exists():
        backup = old / f"old-program-{time.strftime('%Y%m%d-%H%M%S')}"
    moved, stuck = 0, []
    for name in LEGACY_ITEMS:
        src = old / name
        if not src.exists():
            continue
        backup.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, backup / name)
            moved += 1
        except OSError:
            stuck.append(name)
    if moved:
        say(f"the old program files from {old} are in {backup} (delete it once everything works)", "ok")
    if stuck:
        say(f"not moved (in use - close maxedcode and run setup again): {', '.join(stuck)}", "warn")
    if keep_ext and not ext.exists() and (dest / "maxedcode" / "chrome" / "extension").is_dir():
        ext.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(ext), str(dest / "maxedcode" / "chrome" / "extension")],
                           capture_output=True, text=True)
        say("the browser extension keeps working: its old folder now points to the new one" if r.returncode == 0
            else f"reload the browser extension from {dest / 'maxedcode' / 'chrome' / 'extension'}",
            "ok" if r.returncode == 0 else "warn")
    return [old, old / "bin"]


def rehost(dest):
    """The browser bridge (native messaging host) is registered for a copy that is gone (moved by move_legacy,
    deleted): point it at this one. A bridge of another working install is left alone."""
    manifest = Path.home() / ".maxedcode-app" / "browser" / "com.maxedcode.browser.json"
    launcher = manifest.with_name("host.bat")
    if not manifest.exists():
        return
    with suppress(OSError):
        text = launcher.read_text(encoding="utf-8")
        host = re.search(r'"([^"]*host\.py)"', text)
        mine = host and Path(host.group(1)).resolve() == (dest / "maxedcode" / "chrome" / "host.py").resolve()
        if host and Path(host.group(1)).exists() and not (mine and "chcp 65001" not in text):
            return          # this install's own older launcher lacks the code page line: it is written again
    r = subprocess.run([sys.executable, "-c", "from maxedcode.chrome import register; register()"], cwd=str(dest),
                       capture_output=True, text=True)
    say("browser bridge now runs from this install" if r.returncode == 0
        else f"browser bridge not updated: {(r.stderr or '').strip()[-200:]}", "ok" if r.returncode == 0 else "warn")


COMPONENTS = [("maxedcode app", "its own window: chats, projects, a real terminal and a browser"),
              ("maxedcode CLI", "the same agent in a terminal: maxed in any folder, maxed --print for scripts"),
              ("browser extension", "the agent in your own Chrome / Edge (turn it on in the app's Settings)")]


def what_installs():
    """Said before anything is asked: what this setup puts on the computer."""
    section("This installs")
    for name, hint in COMPONENTS:
        print(f"  {c('+', '32;1')} {c(name, '1')}  {c(hint, '90')}")


STORE_PYTHON = ("this Python is from the Microsoft Store: Windows gives it a private copy of AppData, so the "
                "program, the shortcuts and PATH would land there and nothing would appear. Install Python from "
                "https://www.python.org/downloads/ (or run Maxedcode_setup.exe, it does that) and run setup again")


def store_python(exe=None):
    """Python from the Microsoft Store (a packaged app): its writes to AppData and HKCU go to a private copy."""
    exe = str(exe or sys.executable).lower()
    return "\\windowsapps\\" in exe or "pythonsoftwarefoundation.python" in exe


def is_checkout(dest):
    """A git working copy (agent/ of the repository): its launchers are tracked files, setup leaves them alone."""
    return (dest / ".git").exists() or (dest.parent / ".git").exists()


def refresh_launchers(dest):
    """--launchers (maxed shortcuts, sync button 1): the two launchers, README.txt, shortcuts and PATH of this
    install, without touching config.json or asking anything."""
    if is_checkout(dest) or dest.name != PROGRAM:
        sys.exit(f"{dest} is not an installed program folder: run --launchers from <install folder>\\program")
    write_launchers(dest)
    if not ARGS.no_shortcut:
        shortcut(dest)
    app_window(dest, not ARGS.no_shortcut)
    if not ARGS.no_path:
        add_to_path(dest)
    start_hints(dest, not ARGS.no_shortcut)


def start_hints(dest, links=True):
    """The last lines of setup: how to start it."""
    root = dest.parent

    def link(name):            # no backslash inside f-string expressions: setup runs on Python 3.10 too
        return f'"{name}" shortcut, ' if links else ""
    if (root / "maxedcode app.bat").exists():
        say(f"app:            {link('maxedcode app')}{root / 'maxedcode app.bat'} or maxed app")
    say(f"CLI:            {link('maxedcode CLI')}{root / 'maxedcode CLI.bat'} or maxed in any folder")
    say("change server:  maxed configure (or in the app: Settings - maxedcode)")
    say("shortcuts gone: maxed shortcuts")
    say(f"how it works:   {root / 'README.txt'}")


# ---------- uninstall: only what this install made, nothing of another install ----------

ROOT_FILES = ("maxedcode app.bat", "maxedcode CLI.bat", "README.txt")


def _inside(path, root):
    """path (may have %VARS% and quotes) lies in root."""
    with suppress(Exception):
        p = Path(os.path.expandvars(str(path).strip().strip('"'))).resolve()
        return p == root.resolve() or root.resolve() in p.parents
    return False


def uninstall_root():
    """The install folder to remove: --dir, else the one this install.py belongs to, else the default one."""
    if ARGS.dir:
        return Path(ARGS.dir).expanduser().resolve()
    if HERE.name.lower() == PROGRAM and not is_checkout(HERE):
        return HERE.parent
    return DEFAULT_DIR


def remove_shortcuts(root):
    """Desktop and Start shortcuts that start something in root -> their names. Another install's stay."""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $sh=New-Object -COM WScript.Shell;"
          "foreach($f in 'Desktop','Programs'){ $d=[Environment]::GetFolderPath($f);"
          "Get-ChildItem -LiteralPath $d -Filter 'maxedcode*.lnk' -ErrorAction SilentlyContinue | ForEach-Object {"
          "Write-Output ($_.FullName + '|' + $sh.CreateShortcut($_.FullName).TargetPath) } }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    gone = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        lnk, _, target = line.strip().partition("|")
        if target and _inside(target, root):
            with suppress(OSError):
                Path(lnk).unlink()
                gone.append(Path(lnk).stem)
    return gone


def remove_from_path(root):
    """User PATH entries inside root (program\\bin) -> how many were removed."""
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
        try:
            cur, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            return 0
        parts = [p for p in str(cur).split(";") if p.strip()]
        keep = [p for p in parts if not _inside(p, root)]
        if keep == parts:
            return 0
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(keep))
    with suppress(Exception):   # new windows see the shorter PATH
        ctypes.windll.user32.SendMessageTimeoutW(ctypes.c_void_p(0xFFFF), 0x1A, ctypes.c_void_p(0),
                                                 ctypes.c_wchar_p("Environment"), 2, 5000,
                                                 ctypes.byref(ctypes.c_size_t()))
    return len(parts) - len(keep)


def remove_bridge(root):
    """The browser bridge, when it points at this install's host.py -> True when it was removed."""
    launcher = Path.home() / ".maxedcode-app" / "browser" / "host.bat"
    with suppress(OSError):
        host = re.search(r'"([^"]*host\.py)"', launcher.read_text(encoding="utf-8"))
        if host and _inside(host.group(1), root):
            r = subprocess.run([sys.executable, "-c", "from maxedcode.chrome import unregister; unregister()"],
                               cwd=str(root / PROGRAM), capture_output=True, text=True)
            return r.returncode == 0
    return False


def remove_files(root):
    """program\\ and the root files setup wrote; the root itself only when nothing else is left in it
    (--dir C:\\tools may hold other things). -> paths that could not be removed (a running window holds them)."""
    os.chdir(Path.home())                 # a cwd inside root would hold the folder
    left = []
    shutil.rmtree(root / PROGRAM, onerror=lambda f, p, e: left.append(p))
    for name in ROOT_FILES:
        with suppress(FileNotFoundError):
            try:
                (root / name).unlink()
            except OSError:
                left.append(str(root / name))
    with suppress(OSError):
        root.rmdir()                      # only when empty
    return left


def run_uninstall():
    """--uninstall: the program, its shortcuts, PATH entry and browser bridge. Chats, memory and settings
    (~\\.maxedcode) stay unless --purge. -> exit code."""
    root = uninstall_root()
    if is_checkout(root / PROGRAM) or is_checkout(root) or not (root / PROGRAM / "maxedcode" / "config.py").exists():
        say(f"no maxedcode install in {root} (nothing removed)", "warn")
        return 1 if ARGS.dir else 0
    print(c("\n  maxedcode - uninstall\n  ---------------------", "36;1"))
    print(f"    removes:  {root} (the program, its launchers), its shortcuts, its PATH entry, its browser bridge")
    print(f"    {'removes' if ARGS.purge else 'keeps'}:    {DATA_DIR} (chats, memory, settings)"
          + ("" if ARGS.purge else "  - add --purge to remove them too"))
    if not ARGS.yes and sys.stdin.isatty() and input("\n  Uninstall? (y/N) ").strip().lower() not in ("y", "yes"):
        say("cancelled, nothing was removed")
        return 1
    bridge = remove_bridge(root)
    links = remove_shortcuts(root)
    try:
        paths = remove_from_path(root)
    except OSError as e:
        paths = 0
        say(f"PATH not changed: {e}", "warn")
    left = remove_files(root)
    with suppress(OSError):
        (DATA_DIR / "npm-install.json").unlink()      # the npm launcher's note of this install
    if ARGS.purge:
        for d in (DATA_DIR, Path.home() / ".maxedcode-app"):
            shutil.rmtree(d, ignore_errors=True)
    say(f"shortcuts removed: {', '.join(links) or 'none'}; PATH entries removed: {paths}"
        + ("; browser bridge removed" if bridge else ""), "ok")
    if left:
        say(f"{len(left)} file(s) are in use (close the maxedcode window and terminals), then delete {root}", "warn")
        return 1
    say(f"maxedcode is removed{'' if ARGS.purge else f'; your chats and settings are still in {DATA_DIR}'}", "ok")
    return 0


def parse():
    p = argparse.ArgumentParser(description="maxedcode installer")
    p.add_argument("--dir", help=f"install folder (default {DEFAULT_DIR})")
    p.add_argument("--base-url", help=f"server address (default Strata {STRATA_URL}): 192.168.0.10:1234, http://host:8080/v1 ...")
    p.add_argument("--api-key")
    p.add_argument("--model", help="model name (default: the one the server has loaded)")
    p.add_argument("--ctx", type=int)
    p.add_argument("--configure", action="store_true", help="only change server/model, no copying and no pip")
    p.add_argument("--yes", "-y", action="store_true", help="ask nothing, take the default answers")
    p.add_argument("--no-deps", action="store_true", help="do not install Python packages")
    p.add_argument("--no-shortcut", action="store_true", help="do not create the desktop shortcut")
    p.add_argument("--no-path", action="store_true", help="do not add the install folder to PATH (maxed)")
    p.add_argument("--launchers", action="store_true",
                   help="only rewrite the launchers, README.txt, shortcuts and PATH of this folder (no questions)")
    p.add_argument("--console", action="store_true", help="the console wizard instead of the setup window")
    p.add_argument("--first-run", action="store_true",
                   help="first start of a copy without config.json (a git clone): server, model, packages")
    p.add_argument("--uninstall", action="store_true",
                   help="remove the install (this one, or --dir): program, shortcuts, PATH entry, browser bridge")
    p.add_argument("--purge", action="store_true", help="with --uninstall: also remove chats, memory and settings")
    p.add_argument("--update", action="store_true",
                   help="update the install (--dir) to this version's files, nothing asked: config.json, chats, "
                        "settings, shortcuts and PATH stay (auto-update, npm update)")
    p.add_argument("--prepare", action="store_true",
                   help="only make sure this version's Python packages import (pip when they do not)")
    a = p.parse_args()
    if a.first_run:
        a.configure = True
    a.base_url = a.base_url or (os.environ.get("AGENT_BASE_URL") if a.yes else None)
    return a


def main():
    global ARGS
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ARGS = parse()
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 or newer is required, found {sys.version.split()[0]}")
    log(f"--- setup {time.strftime('%Y-%m-%d')}: {sys.executable} {sys.version.split()[0]} {sys.argv[1:]}")
    if ARGS.launchers:
        refresh_launchers(HERE)
        return
    if ARGS.uninstall:
        sys.exit(run_uninstall())
    if ARGS.prepare:
        sys.exit(0 if deps_ok() or install_deps() else 1)
    if ARGS.update:
        sys.exit(run_update())
    if store_python() and not ARGS.configure:
        say(STORE_PYTHON, "err")
        sys.exit(1)
    if not ARGS.yes and not ARGS.console and not ARGS.first_run:
        gui = None
        with suppress(ImportError):
            import install_gui as gui
        code = gui.run(sys.modules[__name__]) if gui and gui.available() else None
        if code is not None:          # None: no window could be opened, the console wizard runs instead
            sys.exit(code)
        if not (sys.stdin and sys.stdin.isatty()):    # and no console to ask in either: the defaults
            ARGS.yes = True
    UI["vt"] = not ARGS.yes and vt_on()
    title = "maxedcode - first start" if ARGS.first_run else \
        "maxedcode - model server" if ARGS.configure else "maxedcode setup"
    print(c(f"\n  {title}\n  {'-' * len(title)}", "36;1"))
    if not ARGS.configure:
        what_installs()
    if ARGS.first_run:
        say("No model server is chosen yet: pick one (found on this PC first). Change it later: python install.py --configure")
    elif not ARGS.yes:
        say("The servers found on this PC come first; the server does not have to run during setup")
    dest = HERE if ARGS.configure else choose_dir() / PROGRAM
    cur = load_cur(dest)
    base, key, models, kind = choose_server(cur)
    section("Model")
    model = choose_model(models, cur, kind, base, key)
    ctx = choose_ctx(base, key, model, cur, kind, online=models is not None)
    if models is not None:
        tune_server(kind, base, key, model or servers.loaded_model(base, key, kind), ctx)
    opts = []
    deps_missing = subprocess.run([sys.executable, "-c", CHECK_IMPORTS], capture_output=True).returncode != 0
    if not ARGS.no_deps and (not ARGS.configure or (ARGS.first_run and deps_missing)):
        opts.append(("deps", "Install / update Python packages", True))
    if not ARGS.configure and not ARGS.no_shortcut:
        opts.append(("link", "Create shortcuts: maxedcode app (window) and maxedcode CLI (terminal)", True))
    if not ARGS.configure and not ARGS.no_path:
        opts.append(("path", "Make maxed work in CMD from any folder (add program\\bin to PATH)", True))
    if models is not None:
        opts.append(("test", "Send a short test request to the model", not ARGS.yes))
    want = {}
    if opts:
        section("Options")
        marks = select("Also do", [label for _, label, _ in opts], marks=[on for *_, on in opts])
        want = {k: m for (k, *_), m in zip(opts, marks)}
    section("Summary")
    if not ARGS.configure:
        print(f"    folder:   {dest.parent}  (the program is in program, your chats in {DATA_DIR})")
        print("    installs: maxedcode app (window) + maxedcode CLI (terminal" +
              (", maxed from any folder)" if want.get("path") else ")") + " + browser extension")
    print(f"    server:   {base}" + (f"  ({servers.LABELS.get(kind, kind)})" if kind else "") +
          ("" if models is not None else "  - not running yet, checked at start"))
    print(f"    model:    {model or 'auto (from the server)'}")
    print(f"    context:  {ctx} tokens" + (f"  (auto from {servers.LABELS[kind]})"
                                           if kind in ("strata", "llamacpp") and not ARGS.ctx else ""))
    if select("Ready", ["Install" if not ARGS.configure else "Save", "Cancel"], 0, hot={"i": 0, "s": 0, "c": 1}) == 1:
        sys.exit("Cancelled, nothing was changed")
    section("Installing" if not ARGS.configure else "Saving")
    run_install(dest, cur, base, key, models, kind, model, ctx, want)
    section("Done")
    finish_hints(dest, models, kind, want)
    if os.environ.get("MX_SETUP_BAT") and not ARGS.yes and not ARGS.first_run:
        ask("Press Enter to close")


def deps_ok():
    return subprocess.run([sys.executable, "-c", CHECK_IMPORTS], capture_output=True).returncode == 0


def app_sources(dest):
    """The C# of the app window as text: the exe is built again only when it changed."""
    app = dest / "desktop" / "app"
    with suppress(OSError):
        return "".join((app / f).read_text(encoding="utf-8") for f in ("maxedcode.cs", "program.cs"))
    return None


def mark_dir(dest):
    """npm (bin/maxedcode.js) learns the folder this setup installed to."""
    if os.environ.get("MX_DIRFILE"):
        with suppress(OSError):
            Path(os.environ["MX_DIRFILE"]).write_text(str(dest.parent), encoding="utf-8")


def run_update():
    """--update: this version's program files into the install, with nothing asked and nothing chosen again;
    config.json (the server), the data, shortcuts and PATH stay as they are. Quick on purpose: the app window waits
    30 s for its server, so pip runs only when a package is missing and the exe is built only when its C# changed."""
    root = Path(ARGS.dir).expanduser().resolve() if ARGS.dir else DEFAULT_DIR
    dest = root / PROGRAM
    if not (dest / "install.py").exists() or is_checkout(dest):
        say(f"no maxedcode install in {root} to update", "err")
        return 1
    log(f"update: dest={dest}")
    before = app_sources(dest)
    copy_files(dest)
    mark_dir(dest)
    stale = []
    for old in legacy_installs(dest):
        stale += move_legacy(old, dest)
    rehost(dest)
    if not deps_ok():
        install_deps()
    write_launchers(dest)
    app = dest / "desktop" / "app"
    if before is not None and before == app_sources(dest) and (app / "maxedcode.exe").exists():
        with suppress(OSError):
            (app / "python.txt").write_text(sys.executable, encoding="utf-8")
    else:
        app_window(dest, False)
    if stale:
        add_to_path(dest, stale)
    say("maxedcode is updated; the server, chats and settings are as they were", "ok")
    return 1 if UI["errors"] else 0


def run_install(dest, cur, base, key, models, kind, model, ctx, want):
    """The install itself, the same for the console wizard and the setup window: files, packages, config.json,
    launchers, shortcuts, the app window, PATH, the test request. Everything it says goes through say()."""
    log(f"install: dest={dest} base={base} kind={kind} model={model} ctx={ctx} want={want}")
    stale = []
    if not ARGS.configure:
        copy_files(dest)
        mark_dir(dest)
        for old in legacy_installs(dest):
            stale += move_legacy(old, dest)
        rehost(dest)
    if want.get("deps"):
        install_deps()
    write_config(dest, cur, base, key, model, ctx, kind)
    if kind == "strata":
        write_strata_settings()
    if want.get("ollama_env"):
        try:
            servers.set_user_env({"OLLAMA_CONTEXT_LENGTH": ctx, "OLLAMA_KEEP_ALIVE": "30m"})
            say("Ollama settings set; restart Ollama (tray icon -> Quit Ollama, then start it again)", "ok")
        except OSError as e:
            say(f"Ollama settings not set: {e}", "warn")
    if not ARGS.first_run and dest.name == PROGRAM and not is_checkout(dest):   # a git clone keeps its start.bat
        write_launchers(dest)
    if want.get("link"):
        shortcut(dest)
    if not ARGS.configure:
        app_window(dest, bool(want.get("link")))
    if want.get("path"):
        add_to_path(dest, stale)
    if want.get("test"):
        chat_test(base, key, model or (models[0] if models and len(models) == 1 else "local"))
    for v in ("AGENT_BASE_URL", "AGENT_MODEL", "AGENT_API_KEY", "AGENT_CTX"):
        if os.environ.get(v):
            say(f"environment variable {v}={os.environ[v]} overrides config.json", "warn")


def finish_hints(dest, models, kind, want):
    """The last lines: how to start it, what is still to do."""
    if ARGS.first_run:
        say("saved; the agent starts now" + ("" if models is not None else " (start the model server first)"), "ok")
        return
    if models is None:
        say(("start Strata (strata launch)" if kind == "strata" else "start the model server") +
            ", then maxedcode", "warn")
    start_hints(dest, bool(want.get("link")))
    if want.get("path"):
        say("maxed works in a NEW CMD / PowerShell window (already open ones do not see the new PATH)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        with suppress(Exception):
            sys.stdout.write(f"{ESC}?25h" if UI["vt"] else "")
        sys.exit("\nCancelled")
