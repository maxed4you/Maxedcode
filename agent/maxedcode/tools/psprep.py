"""Preparing the model's PowerShell command: auto-fix, pre-check and wrapping into one line.

Why:
  * the persistent shell reads stdin line by line (`-Command -`): a multi-line command (foreach/if over
    several lines, here-string) hung there until the 120 s timeout, and `$?` reflected only the last
    line. Now the whole command goes as one base64 line and runs as a scriptblock;
  * a weak model often writes non-existent commands (`Select-First`, `Get-Childir`), bash flags
    (`ls -la`, `rm -rf`), `$env.X` instead of `$env:X`, `"$d: $n"`, `a && b` (an error in PS 5.1).
    Such a command used to run halfway: the first part worked, the second failed, and the model
    guessed what happened. Now the command is parsed before running, command names and cmdlet
    parameters are checked; on an error NOTHING runs, and the model gets the exact reason and a hint;
  * unambiguous errors (`$env.X`, `"$d:"`, top-level `&&`/`||`) are fixed automatically, with a note.
"""
import base64
import difflib
import re
import subprocess

PRE_MARK = "[pre-check] "
# Appended to each top-level pipeline (see RUN); unmark() drops it from echoed error source lines.
OUT_TAIL = " | Out-Default"

# bash/cmd commands that don't exist in PowerShell (or the alias exists but the syntax differs).
HINTS = {
    "grep": "Select-String -Pattern 'x' -Path file (or use the grep tool)",
    "egrep": "Select-String -Pattern 'x'",
    "head": "Select-Object -First N  (or Get-Content file -TotalCount N)",
    "tail": "Get-Content file -Tail N",
    "touch": "New-Item -ItemType File -Path file -Force",
    "which": "Get-Command name",
    "whereis": "Get-Command name",
    "export": "$env:NAME = 'value'",
    "set-env": "$env:NAME = 'value'",
    "unset": "Remove-Item Env:NAME",
    "wc": "(Get-Content file | Measure-Object -Line).Lines",
    "sed": "(Get-Content file) -replace 'a','b'",
    "awk": "ForEach-Object { $_.Split()[0] }",
    "xargs": "ForEach-Object { ... }",
    "du": "(Get-ChildItem dir -Recurse -File | Measure-Object Length -Sum).Sum",
    "df": "Get-PSDrive -PSProvider FileSystem  (or Get-Volume)",
    "free": "Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize",
    "uname": "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version",
    "nproc": "$env:NUMBER_OF_PROCESSORS",
    "lscpu": "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors",
    "lsblk": "Get-Disk; Get-Volume",
    "ifconfig": "Get-NetIPAddress -AddressFamily IPv4  (or ipconfig)",
    "ip": "Get-NetIPAddress -AddressFamily IPv4",
    "sudo": "no sudo on Windows: run the command itself (an elevated action needs an admin terminal)",
    "chmod": "not needed on Windows (use icacls only for ACLs)",
    "chown": "not needed on Windows (use icacls only for ACLs)",
    "ln": "New-Item -ItemType SymbolicLink -Path link -Target target",
    "apt": "winget install <id>", "apt-get": "winget install <id>", "brew": "winget install <id>",
    "open": "Start-Process file", "xdg-open": "Start-Process file",
    "nano": "edit files with the edit_file tool", "vim": "edit files with the edit_file tool", "vi": "edit files with the edit_file tool",
    "Select-First": "Select-Object -First N", "Select-Last": "Select-Object -Last N",
    "Select-Top": "Select-Object -First N", "Sort": "Sort-Object", "Remove": "Remove-Item",
    "RemoveItem": "Remove-Item", "Get-Childitems": "Get-ChildItem", "Get-Child": "Get-ChildItem",
    "Get-Contents": "Get-Content", "Test-Paths": "Test-Path", "Write-Line": "Write-Output",
    "Out-Null-Error": "2>$null", "Get-Processes": "Get-Process", "Get-Services": "Get-Service",
    "For-Each": "ForEach-Object { ... }", "ForEach-Item": "ForEach-Object { ... }", "Foreach-Obj": "ForEach-Object { ... }",
    "Where": "Where-Object { ... }", "Split-Parent": "Split-Path -Parent path", "Split-ParentPath": "Split-Path -Parent path",
    "Get-Parent": "Split-Path -Parent path", "Join-Paths": "Join-Path a b", "Read-File": "Get-Content file",
    "Write-File": "Set-Content -Path file -Value text -Encoding UTF8", "Get-FileContent": "Get-Content file",
    "Count-Lines": "(Get-Content file | Measure-Object -Line).Lines", "Get-Size": "(Get-Item file).Length",
}
# Start of an invented name -> the real command (Select-FirstNothing, Select-Top10 ...)
PREFIX_HINTS = (("Select-First", "Select-Object -First N"), ("Select-Last", "Select-Object -Last N"),
                ("Select-Top", "Select-Object -First N"), ("Split-Parent", "Split-Path -Parent path"),
                ("For-Each", "ForEach-Object { ... }"), ("Get-ChildItems", "Get-ChildItem"))

# Which bash flags map to what (for the "no such parameter" error).
FLAG_HINTS = {
    ("Get-ChildItem", "la"): "Get-ChildItem -Force", ("Get-ChildItem", "l"): "Get-ChildItem",
    ("Get-ChildItem", "al"): "Get-ChildItem -Force", ("Get-ChildItem", "lh"): "Get-ChildItem",
    ("Get-ChildItem", "R"): "Get-ChildItem -Recurse", ("Get-ChildItem", "a"): "Get-ChildItem -Force",
    ("Remove-Item", "rf"): "Remove-Item -Recurse -Force", ("Remove-Item", "fr"): "Remove-Item -Recurse -Force",
    ("Remove-Item", "r"): "Remove-Item -Recurse", ("Copy-Item", "r"): "Copy-Item -Recurse",
    ("Copy-Item", "rf"): "Copy-Item -Recurse -Force", ("New-Item", "p"): "New-Item -ItemType Directory -Force -Path dir",
    ("Get-Content", "n"): "Get-Content file -TotalCount N  (or -Tail N)", ("Get-Content", "f"): "Get-Content file -Wait",
    ("Select-String", "r"): "Get-ChildItem dir -Recurse -File | Select-String -Pattern 'x'",
    ("Select-String", "i"): "Select-String is case-insensitive by default",
    ("Select-String", "n"): "Select-String already prints line numbers",
    ("Select-String", "rn"): "Get-ChildItem dir -Recurse -File | Select-String -Pattern 'x'",
    ("Rename-Item", "Extension"): "Rename-Item -LiteralPath $_.FullName -NewName ($_.BaseName + '.new')",
    ("Get-Volume", "DrivePath"): "Get-Volume -DriveLetter C",
}

# Dynamic provider parameters (FileSystem, Certificate): they may be missing from Parameters.
DYNAMIC = ("File", "Directory", "Hidden", "ReadOnly", "System", "Attributes", "FollowSymlink", "Stream",
           "Encoding", "Raw", "Delimiter", "Wait", "NoNewline", "AsByteStream", "ItemType", "Value", "Type",
           "CodeSigningCert", "DnsName", "Eku", "ExpiringInDays", "SSLServerAuthentication",
           "DocumentEncryptionCert", "Recurse", "Depth", "Force", "Name", "Options")

# The command installs/imports programs itself — checking unknown names then gives false positives.
DEFINES = re.compile(r"(?i)import-module|\bipmo\b|set-alias|new-alias|\bsal\b|\$env:path\b|install-module|"
                     r"winget\s+install|choco\s+install|scoop\s+install|npm\s+(?:i|install)\b|pip\s+install|"
                     r"refreshenv|add-type|^\s*\.\s|;\s*\.\s")

PREFLIGHT = r'''
$__maxedcode_pre = @()
$__maxedcode_tk = $null; $__maxedcode_pe = $null
$__maxedcode_ast = [System.Management.Automation.Language.Parser]::ParseInput($__maxedcode_src, [ref]$__maxedcode_tk, [ref]$__maxedcode_pe)
if ($__maxedcode_pe.Count) {
  $__maxedcode_lines = $__maxedcode_src -split "`r?`n"
  foreach ($__maxedcode_e in $__maxedcode_pe) {
    $__maxedcode_x = $__maxedcode_e.Extent
    $__maxedcode_pre += "syntax error at line $($__maxedcode_x.StartLineNumber) char $($__maxedcode_x.StartColumnNumber): $($__maxedcode_e.Message)"
    $__maxedcode_ln = $__maxedcode_lines[$__maxedcode_x.StartLineNumber - 1]
    if ($__maxedcode_ln) { $__maxedcode_pre += "    $($__maxedcode_ln.Trim())" }
  }
} else {
  $__maxedcode_fn = @($__maxedcode_ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object { ($_.Name -split ':')[-1] })
  $__maxedcode_seen = @{}
  $__maxedcode_cmds = @($__maxedcode_ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true))
  # A "is the program there" probe: if (Get-Command ollama ...) { ollama ... } or try { ollama } catch {} is not an error
  foreach ($__maxedcode_c in $__maxedcode_cmds) {
    if (@('Get-Command', 'gcm', 'where', 'where.exe') -contains $__maxedcode_c.GetCommandName()) {
      foreach ($__maxedcode_el in @($__maxedcode_c.CommandElements | Select-Object -Skip 1)) {
        if ($__maxedcode_el -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $__maxedcode_seen[$__maxedcode_el.Value] = 1 }
      }
    }
  }
  foreach ($__maxedcode_c in $__maxedcode_cmds) {
    $__maxedcode_n = $__maxedcode_c.GetCommandName()
    if (-not $__maxedcode_n -or $__maxedcode_n -match '[\\/]' -or $__maxedcode_n.StartsWith('.') -or $__maxedcode_fn -contains $__maxedcode_n) { continue }
    $__maxedcode_ci = $null
    try { $__maxedcode_ci = Get-Command -Name $__maxedcode_n -ErrorAction Stop | Select-Object -First 1 } catch {}
    if (-not $__maxedcode_ci) {
      $__maxedcode_up = $__maxedcode_c.Parent
      while ($__maxedcode_up -and $__maxedcode_up -isnot [System.Management.Automation.Language.TryStatementAst]) { $__maxedcode_up = $__maxedcode_up.Parent }
      # `prog --version 2>$null` is a probe too: the model suppresses the "no such program" error itself
      if (@($__maxedcode_c.Redirections | Where-Object { "$($_.Extent.Text)" -match '^[2*]>\s*\$null$' }).Count) { $__maxedcode_up = $true }
      if (-not __CHECK_UNKNOWN__ -or $__maxedcode_up -or $__maxedcode_seen[$__maxedcode_n]) { continue }
      $__maxedcode_seen[$__maxedcode_n] = 1
      $__maxedcode_pre += "unknown command: $__maxedcode_n"
      continue
    }
    # An alias's ResolvedCommand can be empty until its module is loaded (ls -> Get-ChildItem)
    if ($__maxedcode_ci.CommandType -eq 'Alias') { $__maxedcode_ci = Get-Command -Name $__maxedcode_ci.Definition -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if (-not $__maxedcode_ci) { continue }
    if (-not ($__maxedcode_ci.CommandType -eq 'Cmdlet' -or ($__maxedcode_ci.CommandType -eq 'Function' -and $__maxedcode_ci.CmdletBinding))) { continue }
    $__maxedcode_pp = $null
    try { $__maxedcode_pp = $__maxedcode_ci.Parameters } catch {}
    if (-not $__maxedcode_pp -or -not $__maxedcode_pp.Count) { continue }
    $__maxedcode_names = @($__maxedcode_pp.Values | ForEach-Object { $_.Name; $_.Aliases })
    if ($__maxedcode_ci.ImplementingType -and [System.Management.Automation.IDynamicParameters].IsAssignableFrom($__maxedcode_ci.ImplementingType)) { $__maxedcode_names += @(__DYNAMIC__) }
    foreach ($__maxedcode_p in $__maxedcode_c.CommandElements) {
      if ($__maxedcode_p -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
      $__maxedcode_pn = $__maxedcode_p.ParameterName
      if (-not $__maxedcode_pn -or $__maxedcode_pn -match '[\*\?\[]') { continue }
      if (-not @($__maxedcode_names | Where-Object { $_ -like "$__maxedcode_pn*" }).Count) {
        $__maxedcode_pre += "$($__maxedcode_ci.Name) has no parameter -$__maxedcode_pn"
      }
    }
  }
}
'''

# Exactly what the old one-line wrapper did, but around a scriptblock.
RUN = r'''
$global:LASTEXITCODE = 0
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# Text piped into a program (`"4 4" | python x.py`): PowerShell 5.1 encodes it with $OutputEncoding (ASCII:
# Cyrillic became "?") and starts it with Console.InputEncoding's BOM, so the program read "﻿4 4".
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
try { [Console]::InputEncoding = $OutputEncoding } catch {}
try { $__maxedcode_bs = $Host.UI.RawUI.BufferSize; $__maxedcode_bs.Width = 1024; $Host.UI.RawUI.BufferSize = $__maxedcode_bs } catch {}
if ($global:Error.Count -gt 200) { $global:Error.Clear() }
$__maxedcode_err0 = $global:Error.Count
$__maxedcode_src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__SRC__'))
__PREFLIGHT__
if ($__maxedcode_pre.Count) {
  foreach ($__maxedcode_l in $__maxedcode_pre) { [Console]::Out.WriteLine("__MARK__$__maxedcode_l") }
  $global:__maxedcode_run_exit = 1
} else {
  $global:__maxedcode_last_ok = $true
  $__maxedcode_ok = $true
  # Each top-level pipeline is formatted on its own: in one output stream the first object fixes the
  # table columns and rows of other types (a second Select-Object) come out blank or vanish.
  $__maxedcode_run = $__maxedcode_src
  $__maxedcode_eb = $__maxedcode_ast.EndBlock
  if ($__maxedcode_eb -and $__maxedcode_eb.Statements.Count -gt 1) {
    $__maxedcode_tops = @($__maxedcode_eb.Statements | Where-Object { $_ -is [System.Management.Automation.Language.PipelineAst] })
    if ($__maxedcode_tops.Count -gt 1) {
      [array]::Reverse($__maxedcode_tops)
      foreach ($__maxedcode_t in $__maxedcode_tops) { $__maxedcode_run = $__maxedcode_run.Insert($__maxedcode_t.Extent.EndOffset, '__OUT_TAIL__') }
    }
  }
  # `Get-Command a, b -ErrorAction SilentlyContinue` leaves $? false although the model said "missing is
  # fine": a last statement whose every command silences its errors is not a failure.
  $__maxedcode_quiet = $false
  if ($__maxedcode_eb -and $__maxedcode_eb.Statements.Count) {
    $__maxedcode_lc = @($__maxedcode_eb.Statements[$__maxedcode_eb.Statements.Count - 1].FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true))
    $__maxedcode_quiet = $__maxedcode_lc.Count -gt 0
    foreach ($__maxedcode_c in $__maxedcode_lc) {
      if ("$($__maxedcode_c.GetCommandName())" -match '^((Select|Where|Sort|Measure|Group|ForEach)-Object|(Format|Out|ConvertTo)-\w+|select|where|sort|measure|group|foreach|ft|fl|%|\?)$') { continue }
      if ($__maxedcode_c.Extent.Text -notmatch '(?i)\s-(ErrorAction|ea)(\s+|:)(SilentlyContinue|Ignore|0)\b|[2*]>\s*\$null') { $__maxedcode_quiet = $false; break }
    }
  }
  try {
    . ([scriptblock]::Create($__maxedcode_run + "`n`$global:__maxedcode_last_ok = `$?"))
    $__maxedcode_ok = $global:__maxedcode_last_ok -or $__maxedcode_quiet
  } catch {
    [Console]::Out.WriteLine(($_ | Out-String).TrimEnd())
    $__maxedcode_ok = $false
  }
  $__maxedcode_native_exit = $LASTEXITCODE
  __NATIVE__
  __ORIGIN__
  $global:__maxedcode_run_exit = 0
  if ($null -ne $__maxedcode_native_exit -and $__maxedcode_native_exit -ne 0) { $global:__maxedcode_run_exit = [int]$__maxedcode_native_exit }
  elseif (-not $__maxedcode_ok) { $global:__maxedcode_run_exit = 1 }
}
'''


def _segments(cmd):
    """Split a command into chunks (kind, text): code / dq (in "...") / sq (in '...')."""
    out, i, n, start, kind = [], 0, len(cmd), 0, "code"
    while i < n:
        ch = cmd[i]
        if kind == "code":
            if ch in "'\"‘’“”":
                out.append(("code", cmd[start:i]))
                start, kind = i, ("sq" if ch in "'‘’" else "dq")
            elif ch == "#" and (i == 0 or cmd[i - 1] in " \t\n;"):
                j = cmd.find("\n", i)
                j = n if j < 0 else j
                out.append(("code", cmd[start:i]))
                out.append(("comment", cmd[i:j]))
                start = i = j
                continue
        elif kind == "sq":
            if ch in "'‘’":
                if i + 1 < n and cmd[i + 1] in "'‘’":
                    i += 2
                    continue
                out.append(("sq", cmd[start:i + 1]))
                start, kind = i + 1, "code"
        else:
            if ch == "`":
                i += 2
                continue
            if ch in "\"“”":
                if i + 1 < n and cmd[i + 1] in "\"“”":
                    i += 2
                    continue
                out.append(("dq", cmd[start:i + 1]))
                start, kind = i + 1, "code"
        i += 1
    out.append((kind, cmd[start:]))
    return [x for x in out if x[1]]


ENV_DOT = re.compile(r"\$env\.([A-Za-z_]\w*)", re.I)
# "$d: $n", "$root:\x" — a parse error in PowerShell ("':' was not followed by a valid variable name").
VAR_COLON = re.compile(r"(?<![\w`{$])\$([A-Za-z_]\w*):(?![\w:])")
# A calculated property without its key: @{N='MB';={...}} (real M20 run, twice in a row). `;={` is never valid.
CALC_NO_KEY = re.compile(r";\s*=\s*\{")
SCOPES = {"env", "global", "script", "local", "private", "using", "function", "variable", "alias", "workflow"}


def _fix_var_colon(m):
    return m.group(0) if m.group(1).lower() in SCOPES else "${%s}:" % m.group(1)


def _chain(line):
    """`a && b` / `a || c` at the top level of the line -> `a; if ($?) { b }`. None if there is nothing to fix."""
    parts, ops, depth, buf, i = [], [], 0, "", 0
    segs = _segments(line)
    for kind, text in segs:
        if kind != "code":
            buf += text
            continue
        i = 0
        while i < len(text):
            ch = text[i]
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
            two = text[i:i + 2]
            if depth == 0 and two in ("&&", "||"):
                parts.append(buf)
                ops.append(two)
                buf, i = "", i + 2
                continue
            buf += ch
            i += 1
    parts.append(buf)
    if not ops or len(set(ops)) > 1 or any(not p.strip() for p in parts):
        return None
    cond = "$?" if ops[0] == "&&" else "-not $?"
    s = parts[-1].strip()
    for p in reversed(parts[:-1]):
        s = f"{p.strip()}; if ({cond}) {{ {s} }}"
    return s


def autofix(cmd):
    """Fix unambiguously wrong constructs. Returns (command, [what was fixed])."""
    cmd = str(cmd or "")
    notes = []
    if "@'" in cmd or '@"' in cmd:
        return cmd, notes          # here-string: anything can be inside, leave it alone
    fixed = []
    for kind, text in _segments(cmd):
        if kind in ("code", "dq"):
            new = ENV_DOT.sub(lambda m: "$env:" + m.group(1), text)
            if new != text:
                notes.append("$env.NAME -> $env:NAME")
            text, new = new, VAR_COLON.sub(_fix_var_colon, new)
            if new != text:
                notes.append('"$name:" -> "${name}:"')
            text = new
        if kind == "code":
            new = CALC_NO_KEY.sub("; E={", text)
            if new != text:
                notes.append("@{N='x';={...}} -> @{N='x'; E={...}}")
            text = new
        fixed.append(text)
    cmd = "".join(fixed)
    if "&&" in cmd or "||" in cmd:
        lines = cmd.split("\n")
        for k, line in enumerate(lines):
            if "&&" in line or "||" in line:
                new = _chain(line)
                if new is not None:
                    lines[k] = new
                    notes.append("&&/|| -> ; if ($?) { ... }  (Windows PowerShell 5.1 has no && / ||)")
        cmd = "\n".join(lines)
    return cmd, sorted(set(notes), key=notes.index)


def unmark(body):
    """Remove the wrapper's OUT_TAIL from "+ <source line>" echoes in PowerShell errors."""
    if "| Out-D" not in body:
        return body
    out = []
    for ln in body.split("\n"):
        if ln.startswith("+ "):
            ln = ln.replace(OUT_TAIL, "")
            # PowerShell cuts a long echo as "... | Out-D ..."
            head = ln.rstrip("\r")
            if head.endswith(" ..."):
                k = next((k for k in range(len(OUT_TAIL), 2, -1) if head[:-4].endswith(OUT_TAIL[:k])), 0)
                ln = head[:-4 - k] + " ..." + ln[len(head):]
        out.append(ln)
    return "\n".join(out)


def wrap(cmd, one_shot=False, check_only=False):
    """PowerShell script that checks and runs cmd. For the live shell — a single line.

    check_only: pre-check only (for background commands that are started separately later)."""
    src = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
    check = "$false" if DEFINES.search(cmd) else "$true"
    dyn = ",".join(f"'{x}'" for x in DYNAMIC)
    from .runners import PS_NATIVE_STDERR, PS_ORIGIN
    pre = PREFLIGHT.replace("__CHECK_UNKNOWN__", check).replace("__DYNAMIC__", dyn)
    body = (RUN.replace("__SRC__", src).replace("__PREFLIGHT__", pre).replace("__MARK__", PRE_MARK)
            .replace("__OUT_TAIL__", OUT_TAIL).replace("__NATIVE__", PS_NATIVE_STDERR).replace("__ORIGIN__", PS_ORIGIN))
    if check_only:
        body = body.replace("$__maxedcode_pre.Count) {", "$__maxedcode_pre.Count -or $true) {", 1)
    if one_shot:
        return body + "\nexit $global:__maxedcode_run_exit\n"
    packed = base64.b64encode(body.encode("utf-8")).decode("ascii")
    # Default value 1: if the wrapper itself fails, the command must not look successful.
    return ("$global:__maxedcode_run_exit = 1; "
            f". ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{packed}'))))")


_COMMANDS = None


def _known_commands():
    """Cmdlet/function/alias names for the "did you mean" hint (loaded once)."""
    global _COMMANDS
    if _COMMANDS is None:
        _COMMANDS = []
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "Get-Command -CommandType Cmdlet,Function,Alias | ForEach-Object Name"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                               stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _COMMANDS = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        except Exception:
            pass
    return _COMMANDS


def _suggest(name):
    hint = HINTS.get(name) or next((v for k, v in HINTS.items() if k.lower() == name.lower()), None) \
        or next((v for k, v in PREFIX_HINTS if name.lower().startswith(k.lower())), None)
    if hint:
        return hint
    close = difflib.get_close_matches(name, _known_commands(), n=3, cutoff=0.75)
    if not close:
        lower = {c.lower(): c for c in _known_commands()}
        close = [lower[c] for c in difflib.get_close_matches(name.lower(), list(lower), n=3, cutoff=0.75)]
    return " or ".join(close) if close else None


def explain(body):
    """If the pre-check rejected the command — a clear message for the model, otherwise None."""
    lines = [x[len(PRE_MARK):] for x in (body or "").split("\n") if x.startswith(PRE_MARK)]
    if not lines:
        return None
    msg = ["Error: command NOT run — pre-check failed, nothing was executed. Fix the command and run it once:"]
    for line in lines:
        if line.startswith("    "):          # the code line with the syntax error
            if f"  {line}" not in msg:
                msg.append(f"  {line}")
            continue
        hint = None
        m = re.match(r"unknown command: (.+)$", line)
        if m:
            hint = _suggest(m.group(1).strip())
            line = f"'{m.group(1)}' is not a PowerShell command, alias or program on this PC"
        m = re.match(r"(\S+) has no parameter -(\S+)$", line)
        if m:
            hint = FLAG_HINTS.get((m.group(1), m.group(2))) or FLAG_HINTS.get((m.group(1), m.group(2).lower()))
            if not hint and re.fullmatch(r"[a-z]{1,3}", m.group(2)):
                hint = "this looks like a bash/Unix flag; this is Windows PowerShell 5.1"
            if not hint:
                hint = f"see the real parameters: (Get-Command {m.group(1)}).Parameters.Keys"
        if "&&" in line or "||" in line:
            hint = "Windows PowerShell 5.1 has no && / ||: use `a; if ($?) { b }`"
        elif "Missing '=' operator after key in hash literal" in line:
            hint = "quote hashtable keys that contain '-' or spaces: @{'gemini-cli'='...'}"
        elif "Missing expression after ','" in line or "Missing closing" in line:
            hint = "check brackets/braces: every @{ ... } and ( ... ) must be closed"
        msg.append(f"- {line}" + (f"  -> use: {hint}" if hint else ""))
    return "\n".join(msg)
