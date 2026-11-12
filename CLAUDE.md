# CLAUDE.md — maxedcode

Guidance for Claude Code and other coding agents working in this repository.

## What it is
**maxedcode** is a coding agent for Windows in the style of Claude Code / Cline CLI: a full-screen TUI built on `textual`, an app window and a browser extension. It works with any OpenAI-compatible API: local servers (llama.cpp, Ollama, LM Studio, vLLM, Strata), a server on the network, or a cloud API (OpenAI, Claude, Gemini, OpenRouter ...). Any model works, from big cloud ones to small local ones with a small context (32768 tokens by default), so much of the code goes into saving context, keeping the prompt cache warm and repairing malformed tool calls.

Developed on Python 3.14 (3.10+ supported), Windows + PowerShell 5.1. Comments, docstrings, the system prompt and UI text are in English (Russian text in the code appears only in regexes that recognize Russian user phrases).

## Running
```
python install.py                          # setup wizard (arrow keys + Enter): folder, server (default Strata
                                           # 127.0.0.1:8080, model/context auto; another local / IP / URL / cloud),
                                           # model, context → config.json + pip + shortcut. The server may be offline —
                                           # the address is saved. --yes = silent install for Strata
python install.py --configure              # only change server/model (same as: maxed configure, program\bin\configure.bat)
python install.py --launchers              # from <install>\program: launchers/README.txt/shortcuts/PATH (maxed shortcuts)
python local_agent.py                      # without config.json (git clone) it first asks for the server: install.py --first-run
start.bat (repository)  |  maxed (after installation, from any folder)   # TUI (program\bin\maxed.bat in the current CMD window)
irm .../install.ps1 | iex                  # one-line install (repo root): Python + download + install.py --yes
npm i -g maxedcode; maxedcode              # npm (bin/maxedcode.js): the first run of the package opens the setup window
                                           # (bootstrap.ps1 + install.py --dir <offered folder>, as Maxedcode_setup.exe; an install
                                           # made before is offered for update; the folder chosen comes back via MX_DIRFILE);
                                           # after an npm update to a version above the installed one — install.py --update into
                                           # that folder (never a downgrade of a self-updated install); then maxed.bat with the
                                           # same arguments; folder — ~\.maxedcode\npm-install.json; maxedcode setup
maxed --update                             # look for a newer release now and install it (auto-update, see below)
```
**Auto-update** (`maxedcode/update.py`, version in `maxedcode/version.py`): the CLI and the app window look at the
latest GitHub release once a day in the background; a newer one (its `maxedcode.zip`) is unpacked into
`~\.maxedcode\update\<version>` and its packages are made sure of (`install.py --prepare`). At the next start
`local_agent.py`, before importing anything else, installs it with `install.py --update` (program files only:
config.json, data, shortcuts, PATH stay; pip only if a package is missing, the exe only if its C# changed — the window
waits 30 s for its server) and runs again on the new code. Not while another maxedcode of the install runs
(`update/running/<pid>`), never under `--print`, never for a git working copy. Off: `"auto_update": false` in
settings.json (Settings → General), `MAXEDCODE_NO_UPDATE=1`, `MAXEDCODE_OFFLINE=1`. A release: package.json and
version.py carry the same number (`release.yml` checks it against the tag; the author's release button bumps both).
Installation (default `%LOCALAPPDATA%\Programs\maxedcode`): the root contains only `maxedcode app.bat`, `maxedcode CLI.bat`,
`README.txt` and `program\` (the whole program + `config.json` + `bin\maxed.bat`/`configure.bat`/`shortcuts.bat`, with `bin` on PATH;
`maxed app`, `maxed configure`, `maxed shortcuts`). Data (sessions, memory, settings.json) still lives in
`~\.maxedcode` (config.HOME); the program is no longer copied there. The installer moves the old layout (program in `~\.maxedcode`)
to `~\.maxedcode\old-program` (`move_legacy`): it takes config.json, and if the browser loads the extension folder, it leaves a
junction to the new one there; the browser bridge is re-registered (`rehost`, only if the old host.py is gone). In the git copy
(`agent\`) the launchers are not touched. App window language → `~\.maxedcode-app\lang.txt` (`/api/lang`) → `chrome/host.py`
(`ready`/`lang`) → the extension panel takes `_locales/<lang>` instead of the browser language.
With the local server off, `servers.describe` answers in 0.5 s (`port_down`): on Windows a closed port only refuses after ~2 s,
and at startup there were 7 such requests (maxed hung for 14 s).
```
python local_agent.py --print "prompt" [--json]   # headless, no UI, everything auto-approved
python run_all_tests.py                    # regression tests (see CONTRIBUTING.md)
python tests/live_eval/runner.py <tag> [scenarios]   # live scenarios on a model server (AGENT_EVAL_URL)
```
Headless flags (maxedcode/headless.py): `--output-format text|json|stream-json`, `--max-turns N`
(default `AGENT_MAX_TURNS` = 50, or `max_turns` from settings.json), `--allowed-tools grep,read_file`,
`--continue` (`-c`, resumes the last session for this cwd), `--settings <file>`, `--model`, `--base-url`,
`--api-key`, `--ctx`, `--home`, `--assume` / `--yes` (do not ask, even when the rule = ask; `--yes` also skips
dangerous commands), `--session <id>` (continue or start the session with this id — the app window keeps one per chat),
`--btw` (a side question after the session history; saves nothing).
`stream-json` emits `assistant` events (with `usage` and `model_ms`), `delta` (a new piece of reasoning/text while streaming;
`reset` — an attempt was retried), `tool_result` (with the call `id` and `ms`) and `result`
(with `model_ms`, `model_calls`, `tool_ms`, `tool_calls`, counters/time per tool).

**Linux / macOS install** (all gated by `install.WINDOWS` / `MACOS`; the Windows path is unchanged):
`curl -fsSL .../install.sh | sh` (repo root) or the same npm package (`bin/maxedcode.js` detects the OS; `package.json`
`os`: win32, linux, darwin). Both start with `installer/bootstrap.sh` (twin of bootstrap.ps1, path via `MX_PYFILE`): a
Python 3.10+ whose `venv` + ensurepip really work, else uv (official standalone installer into `~/.maxedcode/uv`,
`UV_NO_MODIFY_PATH=1`) and its Python 3.12; manual instructions only if that fails too — no sudo anywhere. Default
folder `$XDG_DATA_HOME/maxedcode` (`~/.local/share/maxedcode`), asked by the wizard (Tk window, else the console wizard
with arrow keys via termios; `install.sh` asks when it has a terminal, `MAXEDCODE_YES=1` = defaults). Layout:
`<dir>/README.txt` + `<dir>/program` with its own venv `program/.venv` (PEP 668; `python -m venv` with the setup Python,
fallback `uv venv --seed --python 3.12`; `uv pip` is used when uv is there), `program/bin/maxed` (sh, 755, LF:
`maxed app` → `--app`, `configure`, `shortcuts`, `uninstall`, everything else → local_agent.py), the link
`~/.local/bin/maxed` (a wrapper when no symlink can be made; another program's `maxed` is left alone) and, when
`~/.local/bin` is not in PATH, a marked `export PATH=...` line in the shell's start file (`$SHELL`: ~/.bashrc,
~/.zshrc, ~/.bash_profile on macOS, fish) that uninstall takes out. Shortcuts: Linux `~/.local/share/applications/
maxedcode.desktop` (app) + `maxedcode-cli.desktop` (Terminal=true), icon in `icons/hicolor/256x256/apps`; macOS
`~/Applications/maxedcode.app` (Info.plist, `Contents/MacOS/maxedcode` = `exec program/bin/maxed app`, icns by
sips + iconutil when present). The app on POSIX = `local_agent.py --app` (own window via pywebview: setup installs
`pywebview[qt]` on Linux — QtPy + PyQt6 + PyQt6-WebEngine wheels — and `pywebview` on macOS, optional; else the
browser). `--update` rewrites `program/bin/maxed` and keeps the venv, the link and the menu entries / .app (they only
call `program/bin/maxed`). No exe, WebView2, `.lnk`, registry PATH, `.bat`, `move_legacy`/`rehost` there.

**App window:** its own, no dependencies — `python local_agent.py --app` (`maxedcode/webapp/`: stdlib server +
HTML/CSS/JS page). The app window is `desktop\app\maxedcode.exe`: the installer builds it in place (`build-exe.ps1`:
`maxedcode.cs` + `program.cs`, csc) and signs it with its own certificate `CN=maxedcode` (CurrentUser\My, not added to
trusted roots); an unsigned exe is blocked by Smart App Control, the signed one passed (2026-11-06).
If that fails — `desktop\app\maxedcode.ps1` (the same C# compiled into memory + WebView2). Launch via the shortcut or
`desktop\maxedcode-app.bat`. A folder dragged into the window gives its own path (WebView2 `postMessageWithAdditionalObjects`
→ `Dropped` in `maxedcode.cs`); dropped on the sidebar, it opens as a project. Right panel: a real terminal
(`webapp/pty.py` ConPTY + `static/vt.js`) and an embedded browser (its own WebView2 window over the tab).
Chats without a project each get their own folder `~\.maxedcode\chats\<id>` (created per turn; an empty one is deleted;
old chats from `Documents\maxedcode` are moved at server start, `move_shared_chats`). A new project —
`~\.maxedcode\projects\<name>`; the window's localStorage keeps its own chat folders and project names.
Window modes Manual / Accept edits / Auto / Plan (default Plan): Manual and Accept edits ask in the chat — the `approval`
event in stream-json, the answer as a file in `AGENT_APPROVAL_DIR` (`headless.approval_reason`). A window turn =
`--print --output-format stream-json --session <id>` in the chat folder; Plan/Auto mode → `AGENT_MODE`, effort → `AGENT_EFFORT`;
"Settings → maxedcode" — `maxedcode/appsettings.py`. The tool list is one list for TUI, headless and window
(`tools.catalog.session_catalog`): it is part of the system turn, and a different list used to reset the cache when
switching CLI ↔ window. The turn belongs to the server (`webapp.server.Run`), not the page: leaving the chat and reloading
do not stop it; the page comes back via `/api/watch` (turn lines from the start, then live), and `/api/stop` stops it.
Closing the window with a turn running asks (exe: `/api/quit?check=1`): stop it and close (`&stop=1`) / let it finish in
the background / cancel. Then `/api/quit?now=1`: the server exits as soon as no turn runs, with no grace — a window
reopened onto the old server kept a downloaded update from ever installing (it installs only at a new server's start).
`update.others()` drops a `running/<pid>` file whose pid now belongs to a newer process (Windows reuses pids; a killed
turn leaves its file). Window chats (`util.WINDOW_PREFIXES`: `app-`, `session-`) live next to the terminal sessions, but the
lists are separate: `/history`, `/search` and `--continue` do not see them, and the window sees only them
(`list_sessions(window=True)`). The stream-json format and headless flags are the contract with the window: change them
together with `maxedcode/webapp/static/app.js`.

## Repository and installer
`installer\bootstrap.ps1` (copied into the package next to install.py) is the first step of the installer on a bare Windows:
it looks for Python 3.10+ (registry, py, python, usual folders), skips the Python from the Microsoft Store (its entry in
AppData/HKCU goes into a private copy — the program, shortcuts and PATH "disappeared"), and if none is found installs
Python 3.12 for the user (winget, otherwise the installer from python.org); it passes the path back via a file.
install.py itself installs the WebView2 Runtime if it is missing (a fresh Win10).
`installer\build.bat` (`build.ps1`) builds `Maxedcode_setup.zip`: `Maxedcode_setup.exe` (`installer\setup.cs`, csc;
the program is the resource `app.zip`; signed with a self-signed certificate `CN=maxedcode`; the same steps: %TEMP% →
`bootstrap.ps1` → `install.py` with the arguments; `install.bat` with the packaged program was removed on 2026-11-06 — it was
being blocked) + `README.txt`; the build runs from `installer\app\` (the `app` folder is rebuilt from scratch every time;
tests, reports, `*.db`, keys, sessions and config.json do not get in there — the build fails if it finds personal data).
`Maxedcode_setup.exe` on another PC finds or installs Python 3.10+ and runs `app\install.py` (arguments are passed through:
`Maxedcode_setup.exe --yes --base-url 192.168.0.10:1234 --model qwen3`).
`install.py` accepts the server address in any form (`192.168.0.10`, `ip:port`, `http://host/v1`, `.../chat/completions`),
picks the port itself (1234 LM Studio, 11434 Ollama, 8080 llama.cpp, 8000, 5001, 5000) and `/v1`, can scan its own /24
subnet, takes the model list and the context from the server, writes `program\config.json`, two launchers, README.txt,
`program\bin\*.bat` and the user's PATH (`maxed` from any folder; `--no-path`, `--no-shortcut`). It does not touch the data
(`~\.maxedcode`: sessions, memory, settings.json); the old `settings.bat` is renamed to `settings.bat.old`.
Linux / macOS (see "Running"): `install.sh` + `installer/bootstrap.sh` are the setup exe there; the release also carries
`maxedcode-setup.sh` (a copy of install.sh). `maxedcode.zip` is packed by `installer/zip_program.py` (release.yml), not
Compress-Archive: forward slashes, 755 + LF for `*.sh`, plus `desktop/maxedcode.png` and `bootstrap.sh`;
`update.unpack` also turns backslash names into folders and chmods `*.sh` on POSIX. Tests: `tests.yml` runs the suites on
windows/ubuntu/macos and a real `install.sh` install + uninstall on ubuntu/macos (`posix-install`).

**The installer is English only.** Everything inside `installer\` and `agent\install.py` — user-facing text, questions,
errors, comments, docstrings, and the names of generated files (`start.bat`, `configure.bat`, `README.txt`) — is written in
English (and so are the root `README.md`, `install.ps1` and `install.sh`).

## Structure
```
agent/
├─ local_agent.py        entry point: --print/-p → headless.run, otherwise AgentApp().run()
├─ install.py            setup wizard (EN): copies local_agent.py, system_prompt.md, install.py, maxedcode/ and desktop/ (next to it or ../desktop in the repo) into <install>\program; launchers to the root, bin\*.bat, PATH, shortcuts "maxedcode app"/"maxedcode CLI", desktop\app\python.txt; moves the old layout
├─ install_gui.py        installer window (tkinter): the same steps as the console wizard install.py, the work is done by install.run_install;
│                        without Tk or with --console/--yes — console. Everything the setup says goes to %TEMP%\maxedcode-setup.log
├─ run_all_tests.py      sequential runner of the test suites (does not stop at the first failure)
├─ start.bat             launch in Windows Terminal (wt) or cmd
├─ system_prompt.md      the agent's system prompt (read by config.py)
└─ maxedcode/
   ├─ config.py          settings: settings.json (user → project → CLI → managed) → env → config.json → defaults;
   │                     SYSTEM build (without memory), compaction constants, PROTECTED paths; per-model temperature/top_k/max_turns/edit_format
   ├─ repo_map.py        AST map of a Python repo with a repo_map_tokens budget, added as a separate user message
   ├─ state.py           global dict S (mode, effort, tokens, cached), CMDS (slash commands), MODES/CYCLE,
   │                     EFFORTS, SPIN, VERBS, COMPACT_PROMPT, TITLES
   ├─ util.py            T()/s() — tool schemas, clip, repair_json, tok, split_think, build_content, dur,
   │                     clipboard, frontmatter, sessions (per cwd: list/save/load/delete_session, session_title,
   │                     FTS5 search over sessions_index.db), cache_stats (/metrics), cached_tokens,
   │                     server_ctx, wire, to_cli, shift_down
   ├─ compact.py         shared context-compaction rules for TUI and headless (prefix untouched, compaction anchor)
   ├─ cachekey.py        stable prefix-cache key (session/subagent) and the request extra_body
   ├─ cachedebug.py      AGENT_CACHE_DEBUG=1: where a request diverged from the previous one → cache_debug.log
   ├─ request_compat.py  drops an optional request field only if the server explicitly does not support it
   ├─ streaming.py       stream_completion: request streaming (headless and subagents), cuts off a looping reasoning
   ├─ llmretry.py        retries the model request on server failure (connection/5xx/429/stream without finish_reason); the SDK does not retry on its own
   ├─ events.py          typed event stream for TUI/headless (EventEmitter, EventMetrics)
   ├─ instruction_state.py  which path-scoped rules / folder docs have already been given to the model (survives /rewind)
   ├─ tool.py            immutable tool registry: schemas, validation, parallel-safety, kind
   ├─ cloud.py           /cloud: cloud help; only the `task` goes out (a description of a chunk/file, not project code);
   │                     any number of keys/providers (accounts, keys in DPAPI; on 429/quota — the next key);
   │                     the agent picks the model: cloud_code action=list / note (notes "what it is good for" after web_search)
   ├─ servers.py         which model servers are installed/running (Strata from source, LM Studio, Ollama, llama.cpp, Jan,
   │                     vLLM…), their real context and loaded model, cache fields for each (cache_fields); stdlib only.
   │                     LM Studio / Ollama load any model a request names: every request (main turn, subagents, ultra,
   │                     summaries, --btw) names the loaded one (pick_model; config.follow_loaded before each TUI turn),
   │                     with its loaded window; the configured one only when it is loaded or nothing is
   │                     CLOUD — clouds for the main model (OpenAI, Claude, Gemini, DeepSeek, Groq, Mistral, xAI,
   │                     OpenRouter, Together, Cerebras; all via their OpenAI-compatible endpoint, no separate protocol needed):
   │                     the installer ("Cloud API" line), /server, the app window (Settings → maxedcode: provider + API key).
   │                     find_base/candidates — an address in any form (IP, ip:port, URL) → the URL that responds;
   │                     auth_headers — the Anthropic key for /models goes in x-api-key
   ├─ launch.py          run a model on this PC the way each user starts it (window Settings -> Models, the model menu
   │                     under the input; CLI /local, /local stop). Every program has ways to start: builtin (a model
   │                     + a context + the program's own settings, OPTIONS: GPU, cache format, slots, own flags, env),
   │                     preset (Strata's `--list-presets`), script (found: <strata>/launcher/bat; own: any .bat/.sh/
   │                     .ps1 or command line + its address, for any program incl. "other": KoboldCpp, vLLM, Jan …),
   │                     run in a console of its own. The last way/model/context/settings per program and own ways:
   │                     ~/.maxedcode/launch.json. A started server becomes the agent's; a dead local server is followed
   │                     to the only one that runs (follow_target, window + CLI start). Never wakes a program to look
   │                     (no `lms` while LM Studio is off), never kills an unknown program on a busy port.
   │                     Window: POST /api/launch {do: start|stop|cancel|way-save|way-remove}, GET /api/launch?job=1
   ├─ memory.py          memory: MEMORY.md indexes, topic files, @import, path-scoped rules, notes,
   │                     README.md of folders (docs injected on first touch of a file)
   ├─ hooks.py           hooks from settings.json (7 events, stdin/stdout JSON, exit code 2 = block)
   ├─ skills.py          skills / custom slash commands / subagent definitions (.maxedcode/skills|commands|agents)
   ├─ bundled_skills/    built-in skills (<name>/SKILL.md); only name+description in context, body loaded by the skill tool
   ├─ permissions.py     allow/deny/ask rules, protected paths, kind(), dangerous(), dangerous_python()
   ├─ loopguard.py       anti-loop: duplicate calls, nudge and block when there is nothing new
   ├─ tool_validation.py normalization of tool-call arguments, required check, history cleanup, paste merging
   ├─ subagent.py        subagent (up to 12 steps), read-only by default; model/tools come from the agent definition
   ├─ ultra.py           effort `ultra` = high + staged run before the turn's first request: plan (read-only subagent) →
   │                     each part in its own subagent (write, up to AGENT_ULTRA_STEPS=24 steps) → check command →
   │                     independent reviewer → up to AGENT_ULTRA_ROUNDS=2 fix rounds; the report goes inside a
   │                     not-yet-sent user message (cache intact). Small tasks and plan mode — a normal turn on high
   ├─ headless.py        --print mode: its own loop (up to max_turns) and its own exec_tool
   ├─ update.py          auto-update: daily look at the latest GitHub release, download + --prepare, install at the next
   │                     start (install.py --update), running-instance registry; version.py — VERSION
   ├─ appsettings.py     app window "Settings → maxedcode": server, model (auto), temperature, effort, shared memory;
   │                     the last 3 servers before the current one (~\.maxedcode\recent_servers.json, no keys)
   ├─ appcli.py          app window "Settings → CLI commands": rules, MCP, hooks, memory, skills, commands, subagents, tools
   ├─ webapp/            app window (--app): server.py (stdlib HTTP, turns via headless) + static/ (index.html, app.css, app.js)
   ├─ chrome/            the user's own browser: extension/ (MV3, trimmed-down OpenSidekick), host.py (native messaging,
   │                     port for the agent), `python -m maxedcode.chrome on|off|status`; tool — tools/browser.py
   ├─ tools/
   │  ├─ __init__.py     BUILTIN (schemas of all tools), READONLY, PARALLEL_READS, EDIT_TOOLS, WRITE_PS/WRITE_PY,
   │  │                  ps_readonly(), parallel_safe()
   │  ├─ schemas.py      JSON schemas of the built-in tools (what the model sees)
   │  ├─ catalog.py      fixed catalog order, compact-mode descriptions, session_catalog()
   │  ├─ shell.py        persistent PowerShell per session, protocol with an exit-code marker
   │  ├─ psprep.py       preparing a powershell command: auto-fixes ($env.X, "$d:", &&), one base64 line,
   │  │                  pre-check (syntax, unknown cmdlets/parameters → do not run, hint)
   │  ├─ code_mode.py    restricted AST interpreter of Python over the tool registry (no exec)
   │  ├─ fs.py           read_file (+folder overview, LRU cache), edit_file/patch/apply_diff/write_file, grep, diff; after writing .py — ast.parse + ruff/pyflakes
   │  ├─ runners.py      powershell (120 s timeout), py_run (60 s), background tasks bg_* with auto-notification on completion
   │  ├─ web.py          web_search (ddgs), fetch_url
   │  ├─ pysrc.py        py_source — source of a library object
   │  ├─ mcp.py          MCP server client (names mcp__<server>__<tool>)
   │  ├─ todo.py         todo_write: MARK, validate, copy_tasks, TaskProgressGuard
   │  └─ undo.py         checkpoints before edit/write, /undo rollback (up to 50 per session) and rewind(ts)
   └─ ui/
      ├─ app.py          AgentApp: dialog loop, commands, compaction, confirmations, hooks, tool calls (the largest file)
      ├─ widgets.py      Card (tool card), TodoPanel, TaskProgress, Prompt (input field), Chat, text selection
      └─ screens.py      modal screens: Pick, BtwScreen, HistoryScreen (/history, Del — delete),
                         CommandPalette (Ctrl+P), EditorScreen, Cloud*Screen
                         (subagent tab / another session during a turn — not a modal, but #peek in app.py)
tests/                   test_*.py, the suite list is in run_all_tests.py
```
Tests: `agent/tests/`, run them all with `python agent/run_all_tests.py`.

## Rules for working with code
- Minimal change, existing code style (short functions; docstrings and comments in English only; `contextlib.suppress`).
- CRLF files (`\r\n`): `tool_edit` preserves the original line endings — do not break this.
- All I/O in UTF-8; the PowerShell wrapper in `runners.py` preserves the native exit code, so a failed pytest/git shows up as an error, not as successful output. Paths in agent tools are absolute.
- The prefix (system + tools) is not rebuilt, and memory is not part of SYSTEM — otherwise the server's prefix cache resets.
- Do not include `__pycache__` in archives or copies (install.py and robocopy already ignore it).
- Tests: `python run_all_tests.py` (or `run_all_tests.bat`); the runner compiles the sources, then runs the suites sequentially; one failing suite does not stop the others; on success it offers a live smoke run in AUTO.
- Prefix cache: do NOT add user/system messages in the middle of a turn (between assistant steps) — this shifts the "last user message" boundary in the chat template and the server re-reads the whole turn. Folder docs, path-scoped rules and background-task results go through `util.attach_late()` (into the fresh tool result); the repo map is updated only at the start of a turn, before the user message.
- Textual: `push_screen(..., wait_for_dismiss=True)` — only from a worker (`/commands` go through `command()` in a worker). In `action_*` (keys) and `on_*` (message handlers) — only the callback form `push_screen(screen, callback)`, otherwise `NoActiveWorker` crashes the app (this is how Ctrl+P crashed).
- Sessions: `~/.maxedcode/sessions/<hash_cwd>/<YYYYMMDD-HHMMSS>.json`; the id is a timestamp, do not show it to the user. The title is `util.session_title()` (the first real user message, without `<env>`, `<repo_map>` and other service blocks); an empty one shows as "New session". Do not delete an open session: it is re-saved immediately. Deletion — `util.delete_session(path)` (file + child subagents + search index), only from /history (Del twice); ✕ on a tab only closes it. Unsent messages (the turn queue and the tab queue) are stored in the `queued` field of the session file and are returned to the input when it is opened.
