# Builds the self-contained installer: installer\Maxedcode_setup.zip
# (fresh agent files from ..\agent -> installer\app -> packed into dist\Maxedcode_setup.exe -> zip: the exe + README.txt)
# Tests, developer docs and local user data (sessions, keys, indexes) never go into the installer.
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Split-Path -Parent $here
$src  = Join-Path $root "agent"
$app  = Join-Path $here "app"

if (-not (Test-Path (Join-Path $src "install.py"))) { Write-Warning "No agent files in $src"; exit 1 }

# not shipped: tests, dev docs, anything personal (sessions, memory, keys, history, caches)
$EXCLUDE_DIRS  = @("tests", "__pycache__", ".git", ".maxedcode", ".pytest_cache", "sessions", ".agent-home",
                   "outputs", "checkpoints", "memory", "bg", "tags_cache")
$EXCLUDE_FILES = @("run_all_tests.py", "run_all_tests.bat", "TESTING.md", "*_REPORT.md", "start.bat", "maxed.bat", "configure.bat",
                   "settings.json", "config.json", "cloud_config.json", "permissions.json", "state.json", "mcp.json",
                   "input_history.jsonl", "*.dpapi", "*.db", "*.pyc", "*.log", "*.bat.old", "*.bak*")

# app is rebuilt from scratch every time, so nothing stale or personal survives in it
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
New-Item -ItemType Directory -Force -Path $app | Out-Null
robocopy $src $app /E /XD $EXCLUDE_DIRS /XF $EXCLUDE_FILES /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Warning "robocopy failed (code $LASTEXITCODE)"; exit 1 }

# the app window: maxedcode.ps1 + maxedcode.cs + program.cs + build-exe.ps1 + the WebView2 DLLs (signed by Microsoft);
# no exe in the package: setup builds maxedcode.exe on the user's PC and signs it there (an unsigned one is blocked)
robocopy (Join-Path $root "desktop") (Join-Path $app "desktop") /E /XD __pycache__ `
    /XF maxedcode.exe python.txt README.md maxedcode.png /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Warning "robocopy of desktop failed (code $LASTEXITCODE)"; exit 1 }

# step 1 of the setup: finds or installs Python (see bootstrap.ps1)
Copy-Item (Join-Path $here "bootstrap.ps1") (Join-Path $app "bootstrap.ps1")

$bad = Get-ChildItem -Path $app -Recurse -File -Include *.db, *.dpapi, config.json, *.jsonl
if ($bad) { Write-Warning ("Personal data in app, build stopped: " + ($bad.FullName -join ", ")); exit 1 }

# one setup file: Maxedcode_setup.exe with the program (a zip of app\) inside as a resource
$dist = Join-Path $here "dist"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$payload = Join-Path $dist "app.zip"
Compress-Archive -Path $app -DestinationPath $payload

# the same program inside an exe (setup.cs: unpack, bootstrap.ps1, install.py), signed with this PC's certificate
$exe = Join-Path $dist "Maxedcode_setup.exe"
$csc = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) "csc.exe"
$out = & $csc -nologo -target:exe -optimize "-out:$exe" "-win32icon:$(Join-Path $root 'desktop\maxedcode.ico')" `
    "-resource:$payload,app.zip" -r:System.IO.Compression.dll -r:System.IO.Compression.FileSystem.dll `
    (Join-Path $here "setup.cs") 2>&1
if ($LASTEXITCODE -ne 0) { Write-Warning "setup.exe did not compile: $($out -join ' ')"; exit 1 }
Remove-Item $payload
# a code-signing certificate "CN=maxedcode" of this PC (made once, like desktop\app\build-exe.ps1): a signed exe
# passes Smart App Control where an unsigned one is blocked; other PCs still see an unknown publisher
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq "CN=maxedcode" -and $_.NotAfter -gt (Get-Date).AddDays(30) } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=maxedcode" -CertStoreLocation Cert:\CurrentUser\My `
        -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(5)
}
$sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert -HashAlgorithm SHA256
if (-not $sig.SignerCertificate) { Write-Warning "setup.exe is not signed: $($sig.StatusMessage)" }
Copy-Item (Join-Path $here "README.txt") $dist

$zip = Join-Path $here "Maxedcode_setup.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $exe, (Join-Path $dist "README.txt") -DestinationPath $zip

Write-Output "Done: $zip  ($([math]::Round((Get-Item $zip).Length/1KB,1)) KB): Maxedcode_setup.exe + README.txt"
