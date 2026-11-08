# maxedcode setup, step 1 (Maxedcode_setup.exe runs it; build.ps1 puts it into the setup next to install.py).
# Finds a Python 3.10+ that is not the Microsoft Store one, or installs Python 3.12 (winget, else the python.org
# installer, for this user, no admin rights), and writes the path of its python.exe to $env:MX_PYFILE.
# The Microsoft Store Python is skipped on purpose: Windows gives it a private copy of AppData and of the user
# registry, so the program, the shortcuts and PATH would land there and nothing would appear on the computer.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$log = Join-Path $env:TEMP "maxedcode-setup.log"

function Log($m) {
    try { Add-Content -LiteralPath $log -Value ("{0} [boot] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -Encoding UTF8 } catch { }
}
function Say($m) { Write-Host "  $m"; Log $m }

function Test-Py($exe, [string[]]$pre) {
    # -> the full path of python.exe when it is 3.10+ and not the Microsoft Store one, else $null
    try {
        $out = @(& $exe @pre -c "import sys; print(sys.executable); print(int(sys.version_info >= (3, 10)))" 2>$null)
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { return $null }
    $path = "$($out[0])".Trim()
    if ("$($out[-1])".Trim() -ne "1" -or -not $path -or -not (Test-Path -LiteralPath $path)) {
        Log "skipped (older than 3.10): $exe $pre"
        return $null
    }
    if ($path -match '\\WindowsApps\\') { Log "skipped the Microsoft Store Python: $path"; return $null }
    return $path
}

function Find-Python {
    $list = New-Object System.Collections.ArrayList
    # the registry (python.org installers, winget), newest first
    foreach ($root in "HKCU:\Software\Python\PythonCore", "HKLM:\Software\Python\PythonCore",
                      "HKLM:\Software\WOW6432Node\Python\PythonCore") {
        $keys = Get-ChildItem $root -ErrorAction SilentlyContinue | Sort-Object -Descending {
            $v = $_.PSChildName -replace '[^0-9.].*$', ''
            try { [version]$v } catch { [version]"0.0" }
        }
        foreach ($k in $keys) {
            $ip = Get-ItemProperty -LiteralPath (Join-Path $k.PSPath "InstallPath") -ErrorAction SilentlyContinue
            if ($ip -and $ip.ExecutablePath) { [void]$list.Add(@($ip.ExecutablePath)) }
            elseif ($ip -and $ip.'(default)') { [void]$list.Add(@((Join-Path $ip.'(default)' "python.exe"))) }
        }
    }
    [void]$list.Add(@("py", "-3"))
    [void]$list.Add(@("python"))
    [void]$list.Add(@("python3"))
    foreach ($pat in "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe",
                     "$env:SystemDrive\Python3*\python.exe", "$env:LOCALAPPDATA\Python\pythoncore-3*\python.exe") {
        Get-ChildItem $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending |
            ForEach-Object { [void]$list.Add(@($_.FullName)) }
    }
    foreach ($c in $list) {
        $exe = $c[0]
        $pre = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $p = Test-Py $exe $pre
        if ($p) { return $p }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Python 3.10+ not found: installing Python 3.12 with winget (a minute or two) ..."
        winget install -e --id Python.Python.3.12 --scope user --source winget --accept-package-agreements --accept-source-agreements | Out-Host
        Log "winget (user scope) exit code $LASTEXITCODE"
        $p = Find-Python
        if ($p) { return $p }
        winget install -e --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements | Out-Host
        Log "winget exit code $LASTEXITCODE"
        $p = Find-Python
        if ($p) { return $p }
    } else {
        Log "no winget"
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-$arch.exe"
    $exe = Join-Path $env:TEMP "python-3.12.10-$arch.exe"
    Say "downloading Python 3.12 from python.org ..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $exe
    } catch {
        Say "download failed: $($_.Exception.Message)"
        return $null
    }
    Say "installing Python 3.12 for this user (no admin rights needed) ..."
    $proc = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1",
        "Include_test=0", "Include_launcher=1", "InstallLauncherAllUsers=0"
    Log "python.org installer exit code $($proc.ExitCode)"
    Remove-Item -LiteralPath $exe -ErrorAction SilentlyContinue
    return Find-Python
}

[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = "utf-8"
Log "--- bootstrap: $([Environment]::OSVersion.VersionString), PowerShell $($PSVersionTable.PSVersion), $env:PROCESSOR_ARCHITECTURE"
$py = Find-Python
if (-not $py) { $py = Install-Python }
if (-not $py) {
    Say "[ERROR] Could not find or install Python 3.10+."
    Say "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Say "then run Maxedcode_setup.exe again. The log: $log"
    exit 1
}
Say "Python: $py"
Log "python: $py"
[IO.File]::WriteAllText($env:MX_PYFILE, $py, (New-Object Text.UTF8Encoding($false)))
exit 0
