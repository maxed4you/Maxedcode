# maxedcode one-line install for Windows (PowerShell 5.1+):
#
#   irm https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.ps1 | iex
#
# It finds Python 3.10+ (or installs Python 3.12 with winget), downloads maxedcode from GitHub and runs its
# installer with no questions: the app window + the CLI (maxed), shortcuts, PATH, the model server found on this PC.
# Your chats and settings (%USERPROFILE%\.maxedcode) are kept; run it again to update.
#
# Options (environment variables, set them before the command):
#   $env:MAXEDCODE_ARGS  = "--base-url 192.168.0.10:1234 --model qwen3"   extra installer options (install.py --help)
#   $env:MAXEDCODE_ASK   = "1"        the interactive wizard instead of the silent defaults
#   $env:MAXEDCODE_REF   = "main"     branch or tag to install
#   $env:GITHUB_TOKEN    = "..."      only to install from a private fork

function Install-Maxedcode {
    $ErrorActionPreference = "Stop"
    $repo = "maxed4you/Maxedcode"
    $ref = if ($env:MAXEDCODE_REF) { $env:MAXEDCODE_REF } else { "main" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    Write-Host ""
    Write-Host "  maxedcode setup" -ForegroundColor Cyan
    Write-Host "  ---------------" -ForegroundColor Cyan

    # Python 3.10+: the py launcher, python on PATH, then winget
    # the real python.exe is asked for, so the rest needs no py launcher
    function Find-Py($exe, $pre) {
        try {
            $out = & $exe @pre -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
            $path = "$out".Trim()
            # not the Microsoft Store Python: its writes to AppData and HKCU go to a private copy
            if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path $path) -and $path -notmatch '\\WindowsApps\\') { return $path }
        } catch { }
        return $null
    }
    $py = Find-Py "py" @("-3")
    if (-not $py) { $py = Find-Py "python" @() }
    if (-not $py) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "Python 3.10+ is needed: install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again"
        }
        Write-Host "  Python 3.10+ not found: installing Python 3.12 with winget ..."
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Host
        foreach ($d in "Python313", "Python312") {
            $p = Join-Path $env:LOCALAPPDATA "Programs\Python\$d\python.exe"
            if (-not $py -and (Test-Path $p)) { $py = Find-Py $p @() }
        }
        if (-not $py) { $py = Find-Py "py" @("-3") }
        if (-not $py) { throw "Python could not be installed automatically: https://www.python.org/downloads/" }
    }
    Write-Host "  Python: $py"

    # The program. Like opencode, the built package of the latest release: the program alone (maxedcode.zip:
    # install.py, maxedcode\, desktop\ - no tests, no docs). No release yet / a branch asked for / private
    # repository: the source of $ref (only the program is installed from it, the rest stays in %TEMP%).
    $work = Join-Path $env:TEMP ("maxedcode-setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        $zip = Join-Path $work "maxedcode.zip"
        $ProgressPreference = "SilentlyContinue"
        if ($env:MAXEDCODE_SRC) {             # a local copy of the repository instead of the download (testing)
            Copy-Item -Recurse (Join-Path $env:MAXEDCODE_SRC "agent"), (Join-Path $env:MAXEDCODE_SRC "desktop") `
                (New-Item -ItemType Directory -Force (Join-Path $work "src")).FullName
        } elseif ($env:GITHUB_TOKEN) {
            Write-Host "  downloading maxedcode ($ref) ..."
            Invoke-WebRequest -UseBasicParsing -OutFile $zip -Uri "https://api.github.com/repos/$repo/zipball/$ref" `
                -Headers @{ Authorization = "Bearer $env:GITHUB_TOKEN"; "User-Agent" = "maxedcode-setup" }
        } else {
            $got = $false
            if (-not $env:MAXEDCODE_REF) {
                try {
                    Write-Host "  downloading maxedcode (latest release) ..."
                    Invoke-WebRequest -UseBasicParsing -OutFile $zip -Uri "https://github.com/$repo/releases/latest/download/maxedcode.zip"
                    $got = $true
                } catch { Write-Host "  no release package yet: taking the source of $ref" }
            }
            if (-not $got) {
                Write-Host "  downloading maxedcode ($ref) ..."
                Invoke-WebRequest -UseBasicParsing -OutFile $zip -Uri "https://github.com/$repo/archive/$ref.zip"
            }
        }
        $pkg = Join-Path $work "pkg"
        if (Test-Path $zip) { Expand-Archive -LiteralPath $zip -DestinationPath $pkg -Force }
        # the release package has install.py at its top; the source has it in <repo>-<ref>\agent
        $installer = @(Get-ChildItem $pkg, $work -Recurse -Depth 2 -Filter install.py -ErrorAction SilentlyContinue |
            Where-Object { Test-Path (Join-Path $_.DirectoryName "maxedcode\config.py") }) | Select-Object -First 1
        if (-not $installer) { throw "the download has no install.py" }

        $installArgs = @()
        if ($env:MAXEDCODE_ASK -ne "1") { $installArgs += "--yes" }
        if ($env:MAXEDCODE_ARGS) { $installArgs += ($env:MAXEDCODE_ARGS -split '\s+' | Where-Object { $_ }) }
        & $py $installer.FullName @installArgs
        if ($LASTEXITCODE -ne 0) { throw "the installer stopped with code $LASTEXITCODE" }
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

try {
    Install-Maxedcode
} catch {
    Write-Host "  [ERROR] $($_.Exception.Message)" -ForegroundColor Red
}
