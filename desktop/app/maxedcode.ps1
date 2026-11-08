# maxedcode app: the app window. Compiles maxedcode.cs in memory and opens the window. Setup also builds the signed
# maxedcode.exe (build-exe.ps1, the same C#); this script is what runs where Windows does not let that exe start.
#
#   conhost.exe --headless powershell.exe -STA -File maxedcode.ps1 [-Port 3077]   what the shortcuts run
#   powershell -File maxedcode.ps1 -Shortcuts                                       create the shortcuts
#   powershell -File maxedcode.ps1 -Check                                           only compile (a test)
param([int]$Port = 3077, [switch]$Shortcuts, [switch]$Check)
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Web.Extensions
    $dlls = @('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll') | ForEach-Object { Join-Path $Here "lib\$_" }
    Add-Type -Path $dlls
    Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $Here 'maxedcode.cs'))) `
        -ReferencedAssemblies (@('System.Windows.Forms', 'System.Drawing', 'System.Web.Extensions') + $dlls)
    if ($Check) { 'compiled'; exit 0 }
    if ($Shortcuts) {
        [MaxedcodeApp]::Shortcuts($Here) | ForEach-Object { "Shortcut: $_" }
        exit 0
    }
    [MaxedcodeApp]::Start($Here, $Port)
}
catch {
    if ($Shortcuts -or $Check) { throw }
    [Windows.Forms.MessageBox]::Show("maxedcode app did not start:`n`n$_", 'maxedcode', 'OK', 'Error') | Out-Null
    exit 1
}
