"""Local Agent — full-screen terminal agent in the style of Cline CLI.

Install:    python install.py   (asks where to install and which API: address, key, model)  — or manually: pip install openai ddgs textual "mcp>=2" pillow rich
Run:        maxedcode CLI.bat in the install folder (start.bat in the repo), maxed in any folder, or python local_agent.py
Headless:   python local_agent.py --print "prompt" [--json]   (no UI, answer to stdout; --json — result+usage)
Window:     python local_agent.py --app [--port 3077]   (local server; the window: maxedcode app.bat / maxed app)
Keys:       Enter — send, Shift+Enter / Ctrl+J / Alt+Enter / "\\"+Enter — new line, Up/Down — previous prompts,
            Tab / Shift+Tab — cycle auto/manual/plan modes, Alt+V — paste image from clipboard, Esc — interrupt, PgUp/PgDn — scroll, Ctrl+C — quit,
            mouse selection (double-click — widget, triple-click — chat) or /copy — everything selected → clipboard automatically

All code lives in the maxedcode/ package: config, state, util, tools (fs, web, runners, pysrc, mcp, undo), ui (widgets, screens, app), headless.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def needs_setup():
    """No model server chosen anywhere yet (a fresh git clone: no config.json, no base_url, no AGENT_BASE_URL)."""
    if os.environ.get("AGENT_BASE_URL") or "--base-url" in sys.argv or "--settings" in sys.argv:
        return False
    if (HERE / "config.json").exists():
        return False
    for f in (Path.home() / ".maxedcode" / "settings.json", Path.cwd() / ".maxedcode" / "settings.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("base_url"):
                return False
        except (OSError, ValueError, AttributeError):
            pass
    return True


def auto_server():
    """Non-interactive first start (--print, the window's turns): the default address if it answers, else the
    first server running on this PC. Only for this run; nothing is written (the window asks the user)."""
    from maxedcode import servers
    base = servers.default_base("strata")
    try:
        up = servers.model_ids(base, timeout=2) is not None
    except Exception:
        up = True                       # it answers, it only wants a key
    if not up:
        found = servers.running()
        if not found:
            return
        base = found[0]["base"]
        print(f"[setup] no server configured, using {found[0]['label']} at {base} "
              "(python install.py --configure to choose)", file=sys.stderr)
    os.environ["AGENT_BASE_URL"] = base


if needs_setup():
    if "--app" not in sys.argv and "--print" not in sys.argv and "-p" not in sys.argv and sys.stdin.isatty():
        rc = subprocess.run([sys.executable, str(HERE / "install.py"), "--first-run"]).returncode
        if rc or not (HERE / "config.json").exists():
            sys.exit(rc or 1)
    elif "--app" not in sys.argv:
        auto_server()

if "--app" in sys.argv:
    # The desktop window: a local web page over headless turns (maxedcode/webapp, window desktop/app/maxedcode.exe).
    from maxedcode.webapp.server import main
    main(sys.argv)
elif "--print" in sys.argv or "-p" in sys.argv:
    import asyncio

    VAL = ("--output-format", "--max-turns", "--allowed-tools", "--settings", "--model", "--base-url",
           "--api-key", "--ctx", "--home", "--session")
    FLAGS = ("--print", "-p", "--json", "--continue", "-c", "--yes", "--assume", "--btw")
    words, i = [], 1
    while i < len(sys.argv):
        x = sys.argv[i]
        if x in FLAGS:
            i += 1
        elif x in VAL:
            i += 2
        else:
            words.append(x)
            i += 1
    prompt = " ".join(words) or sys.stdin.read()
    if not prompt.strip():
        print('Usage: python local_agent.py --print "prompt" [--json] [--output-format text|json|stream-json] '
              '[--max-turns N] [--allowed-tools grep,read_file] [--continue] [--assume] [--yes]', file=sys.stderr)
        sys.exit(1)
    from maxedcode.headless import run
    asyncio.run(run(prompt, "--json" in sys.argv))
else:
    # Configure ANSI truecolor before Textual/Rich imports choose terminal capabilities.
    os.environ.setdefault("COLORTERM", "truecolor")
    os.environ.setdefault("FORCE_COLOR", "3")
    try:
        from maxedcode.ui.app import AgentApp
    except ImportError as e:
        sys.exit(f"Missing Python package ({e.name}). Install them: python install.py --configure --first-run "
                 f"(or: pip install openai ddgs textual \"mcp>=2\" pillow rich)")
    AgentApp().run(mouse=os.environ.get("AGENT_MOUSE", "1") != "0")
