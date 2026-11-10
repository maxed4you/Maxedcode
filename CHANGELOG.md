# Changelog

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
