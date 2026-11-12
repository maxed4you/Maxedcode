# maxedcode app as its own exe: compiles maxedcode.cs + program.cs into maxedcode.exe next to this script, signs it
# with this PC's own code-signing certificate ("CN=maxedcode" in CurrentUser\My, made once, private key not
# exportable; nothing is added to the trusted roots) and checks that Windows lets it run (maxedcode.exe --check).
# Smart App Control blocks an unsigned exe; a signed one ran on the test PC. It may still refuse a fresh build once
# ("An Application Control policy has blocked this file") and let the next one run: up to 3 builds are tried
# (an update replaced the exe with maxedcode.ps1 that way, 2026-11-12). Exit 0: the exe is ready to use;
# anything else: keep using maxedcode.ps1 (the installer does exactly that).
#
#   powershell -File build-exe.ps1
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$exe = Join-Path $Here 'maxedcode.exe'
$new = Join-Path $Here 'maxedcode.new.exe'

function Build {
    $csc = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
    if (-not (Test-Path $csc)) { throw "csc.exe not found ($csc)" }
    $icon = Join-Path (Split-Path $Here) 'maxedcode.ico'
    $out = & $csc -nologo -target:winexe -optimize "-out:$new" "-win32icon:$icon" `
        -r:System.Windows.Forms.dll -r:System.Drawing.dll -r:System.Web.Extensions.dll `
        "-r:$Here\lib\Microsoft.Web.WebView2.Core.dll" "-r:$Here\lib\Microsoft.Web.WebView2.WinForms.dll" `
        "$Here\maxedcode.cs" "$Here\program.cs" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "compile failed: $($out -join ' ')" }

    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq 'CN=maxedcode' -and $_.NotAfter -gt (Get-Date).AddDays(30) } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $cert) {
        $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=maxedcode' -CertStoreLocation Cert:\CurrentUser\My `
            -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(5)
    }
    $sig = Set-AuthenticodeSignature -FilePath $new -Certificate $cert -HashAlgorithm SHA256
    if (-not $sig.SignerCertificate) { throw "signing failed: $($sig.StatusMessage)" }
}

try {
    for ($try = 1; ; $try++) {
        Build
        try {
            $p = Start-Process -FilePath $new -ArgumentList '--check' -PassThru -Wait
            if ($p.ExitCode -ne 0) { throw "the exe did not run (exit $($p.ExitCode))" }
            break
        }
        catch {
            if ($try -ge 3 -or $_.Exception.Message -notmatch 'policy|blocked') { throw }
            Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
        }
    }
    try { Move-Item -LiteralPath $new -Destination $exe -Force }
    catch { throw "maxedcode.exe is in use (the window is open): close it and run this again" }
    "ready: $exe"
    exit 0
}
catch {
    Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue
    "not ready: $($_.Exception.Message)"
    exit 1
}
