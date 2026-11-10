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

All three find Python 3.10+ (or install Python 3.12 for you), look for a model server on this PC, install the app and
the CLI, make the shortcuts and add `maxed` to PATH. Already installed? It updates itself (Settings → General).
Uninstall: `maxed uninstall` (npm: `maxedcode uninstall`).

## Files

| file | what it is |
|---|---|
| `Maxedcode_setup.zip` | `Maxedcode_setup.exe` (the setup wizard) and `README.txt` |
| `maxedcode.zip` | the program alone; the PowerShell one-liner and auto-update download it |
