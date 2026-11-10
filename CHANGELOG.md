# Changelog

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
