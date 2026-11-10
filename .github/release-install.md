## Install

**PowerShell** (recommended):

```powershell
irm https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.ps1 | iex
```

**npm** (Node.js 18+):

```powershell
npm install -g maxedcode
maxedcode
```

**Setup zip:** download `Maxedcode_setup.zip` below, extract it and run `Maxedcode_setup.exe`. It has no paid
code-signing certificate, so Windows may say "Windows protected your PC": **More info → Run anyway**. If Smart App
Control blocks it, use the PowerShell or npm install.

**Linux / macOS** (Python 3.10+; Debian/Ubuntu also need `python3-venv`):

```sh
curl -fsSL https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.sh | sh
```

or `npm install -g maxedcode` and `maxedcode` (the same package detects the system), or download
`maxedcode-setup.sh` below and run `sh maxedcode-setup.sh`.

On Windows all three find Python 3.10+ (or install Python 3.12 for you), look for a model server on this PC, install the app and
the CLI, make the shortcuts and add `maxed` to PATH. Already installed? It updates itself (Settings → General).
On Linux / macOS the program goes to `~/.local/share/maxedcode` with its own Python environment, `maxed` is linked in
`~/.local/bin`, and the app is in the app menu (Linux) or `~/Applications/maxedcode.app` (macOS).
Uninstall: `maxed uninstall` (npm: `maxedcode uninstall`).

## Files

| file | what it is |
|---|---|
| `Maxedcode_setup.zip` | `Maxedcode_setup.exe` (the setup wizard) and `README.txt` |
| `maxedcode.zip` | the program alone; the one-line installs and auto-update download it |
| `maxedcode-setup.sh` | the Linux / macOS setup script (the same as `install.sh`) |
