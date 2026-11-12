# Changelog

## 1.1.6

### The app window
- An open list (mode, model, project, Settings choices) closes when you click outside the window or on its title
  bar, and when the window is resized; before, it stayed open.
- The right panel's browser no longer covers Start models and the first-start dialog.
- Escape closes one thing at a time: Start models closes and Settings under it stays.
- Settings no longer says a downloaded release "installs at the next start" while automatic updates are off.

### Setup and update
- When Smart App Control refuses a freshly built app window once, setup builds it again (up to 3 times) instead
  of falling back to the slower script window.

## 1.1.5

### The app window: closing it closes it
- Closing the window while the agent is still working asks: stop it and close, let it finish in the background,
  or keep the window open. Before, the agent went on unseen and the app seemed never to close.
- Once nothing runs, the window's server exits at once (it used to wait 90 seconds). A window opened again within
  that time came back to the old server, so a downloaded update ("installs at the next start") never installed.

### Setup and update
- A maxedcode that was killed no longer holds back updates when Windows gives its process id to another program.

## 1.1.4

### Starting a model: "ready" only when the model is in memory
- LM Studio: a start no longer shows "loading" for minutes over a model that is already loaded (`lms load` could
  stay open after "Model loaded successfully"); a model you loaded yourself never counts as the new load.
- Strata, llama.cpp and start scripts: a previous server that did not close is no longer taken for the new one
  (the start said "ready" at once, with the old model). A start that cannot run fails in seconds, not 15 minutes.
- Ollama: a big model that takes longer than the request counts once Ollama has it.
- A Strata whose engine is not in memory shows "not in memory: loads with the first request", not "loaded".
- Strata counts as installed only when there is something to run (leftovers of an unfinished first setup in
  `~\.strata` no longer do).

### Connection
- After a start on the Start models tab the agent works with that server at once; Connection says
  "The model comes from <address> · started on the Start models tab".
- An address in any form no longer breaks a look at the servers (`http://pc[::1]` and the like).

### The app window
- A step that was refused (Plan mode) is no longer shown as done: no "Changed files" or "+N −M" for a write that
  never happened. A step waiting for your Yes says "Writing a file", not "Wrote a file".
- Every hint names the real Settings section (Connection, Browser); the Permissions text says what each mode does.
- The window's server answers every request, also a broken one, instead of dropping the connection.

### Setup and update
- Install folders with spaces, Cyrillic letters, `( ) & ' !` (a user name like "Иван Петров (Work)"): the CLI
  launcher without Windows Terminal, the double-click uninstall, the app shortcuts on uninstall and auto-update work.

### Safety and tools
- `grep` passes the pattern safely to ripgrep (a pattern like `--pre=...` could run a program).
- More dangerous commands are caught: `ri`/`rd`, deleting a quoted `"C:\Program Files"`, `git push -fu`,
  `git reset HEAD~1 --hard`, `rmtree` imports, writes to `.env::$DATA`.
- File edits keep UTF-16 big-endian files, lone CR and rare line separators as they were; `apply_diff` handles
  `-- comment` lines and pure insertions right; `glob` no longer walks a whole drive.
- Odd model replies (empty choices, a non-JSON answer, arguments as an object, a call without a name, broken
  Unicode) no longer crash a turn.

## 1.1.2

### App window
- The "Find servers" window (Settings → Models → Find servers) now closes like the other windows: a click outside
  it or Esc. The "Later" button is gone; the window has a close button (×) in its top-right corner instead.

## 1.1.1

### LM Studio / Ollama: the model you loaded, never a second one
- maxedcode now uses the model that is **loaded** in LM Studio / Ollama, with its real context. Before, it always
  asked for the model from the settings, and if another one was loaded, LM Studio quietly loaded a second model
  (often with a 4096-token context). Now it says once which model it uses and why, and checks again before each turn,
  so switching the model in LM Studio is followed. Nothing loaded: it keeps your choice and tells you to load it.
- A model loaded with a context too small to work in (under 16384 tokens: maxedcode's instructions and tools alone
  take about 9-10k) is now said plainly, instead of a bare "Error code: 400".

### Linux (tried on a fresh Ubuntu 24.04 desktop)
- The one-line install for Linux is `wget -qO- .../install.sh | sh`: Ubuntu and Debian have `wget`, not `curl`.
- The setup window uses the desktop's own fonts: no more cut-off text and boxes instead of check marks.
- The app's own window: on Wayland it opens without extra packages; on an X11 desktop without `libxcb-cursor0`
  the app opens in the browser and says the one command that gives it its own window
  (`sudo apt install libxcb-cursor0`), also as a desktop notification when started from the app menu.
- Strata is no longer marked "recommended" on Linux / macOS when it is not installed.

## 1.1.0

**maxedcode now runs on Linux and macOS too.** One package for all three systems: `npm install -g maxedcode`
finds out which one it is on. Or one line in a terminal:
`wget -qO- https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.sh | sh` on Linux
(Ubuntu has no `curl` out of the box), `curl -fsSL https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.sh | sh` on macOS.

### Linux / macOS
- **Setup needs nothing installed first and never asks for sudo.** It uses your Python 3.10+ if it can make a
  venv, and otherwise fetches [uv](https://github.com/astral-sh/uv) and its own Python 3.12, all in your home folder.
- You choose the install folder (default `~/.local/share/maxedcode`). The program gets its own Python environment;
  `maxed` works in any new terminal (setup adds `~/.local/bin` to PATH if needed, and removes it on uninstall).
- **The app:** in its own window (pywebview), or in your browser when no window can be opened. It is in the app
  menu on Linux and in `~/Applications/maxedcode.app` on macOS; `maxed app` from a terminal.
- **The agent runs commands in bash** (the model sees a `bash` tool instead of `powershell`), with the same
  safety checks for dangerous commands and protected folders (`~/.ssh`, `/etc`, `/usr`, `/System` ...).
  Rules, hooks and agent definitions written for Windows (`powershell(...)`) apply to bash.
- The terminal in the app, the browser extension bridge (Chrome, Edge, Brave, Chromium, Vivaldi, Yandex), API keys
  in the system keyring (or a file only you can read), LM Studio / Ollama / Jan / llama.cpp found where they install.
- Auto-update and `maxed uninstall` work the same as on Windows.

### Windows
- Nothing changes in how it works: the same tools, prompt and setup.

## 1.0.2

### Setup
- Setup no longer starts your model: the test request is off by default (it made LM Studio load the model with
  its own small 4096-token context). Everything installs, and the model server can be checked later in Settings.
- `maxed uninstall` / `maxedcode uninstall` without a console to answer in now cancels cleanly instead of failing.
- Releases now appear only once npm really serves the new version, so `npm install -g maxedcode` right after a
  release no longer fails with 404.

## 1.0.1

**maxedcode now updates itself.** Once a day the app and the CLI look for a new release here, download it in the
background and install it the next time you start maxedcode. Your model server, chats, settings, shortcuts and PATH
stay as they are. Turn it off in Settings → General → *Update automatically*; `maxed --update` updates right away.

> **Coming from 1.0.0?** 1.0.0 cannot update itself yet, so update once by hand: run the install again (PowerShell
> one-liner or `Maxedcode_setup.exe`), or `npm install -g maxedcode` and then `maxedcode`. From 1.0.1 on, updates
> arrive on their own.

### Setup
- **npm opens the setup window** (the same one as `Maxedcode_setup.exe`) instead of installing silently to a folder
  you never chose. You pick the install folder; an install you already have is offered for update.
- The setup window shows exactly where everything goes (program, `maxedcode app.bat`, `maxedcode CLI.bat`, your data)
  and, at the end, where it was installed and how to start it. Nothing starts by itself after setup.
- `npm` updates use a quick program-only update and never take back a newer version the program installed itself.

### App window
- **English by default** on the first start (the browser extension too); Russian is in Settings → General → Language.
- **Choosing a model server is clearer:** one main button for the recommended server, the exact command or folder
  each server starts from, and *Use* now only saves the address — starting a server is a separate, optional step.
- **Recent servers:** the last 3 servers you used are kept, in the first-start picker and under Settings → Models →
  Server URL, so you can switch back in one click (a cloud API asks for its key again; keys are not kept there).
- Settings → General shows the version, *Check now* and the auto-update switch.

## 1.0.0

The first public release.
