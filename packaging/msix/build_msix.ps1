$ErrorActionPreference = "Stop"

$msixDir    = $PSScriptRoot
$manifest   = Join-Path $msixDir "AppxManifest.xml"
$assetsDir  = Join-Path $msixDir "assets"

$distExeDir = "C:\BuildCache\RemoteMouse\dist\RemoteMouse"
$stagingDir = "C:\BuildCache\RemoteMouse\msix_staging"
$outRoot    = "C:\BuildCache\RemoteMouse\dist"

if (-not (Test-Path "$distExeDir\RemoteMouse.exe")) {
    throw "Nuitka build not found at $distExeDir\RemoteMouse.exe. Run packaging\nuitka\build_exe.ps1 first."
}
if (-not (Test-Path "$distExeDir\remote_mouse\web\index.html")) {
    throw "Bundled web UI missing at $distExeDir\remote_mouse\web\index.html."
}
if (-not (Test-Path "$assetsDir\StoreLogo.png")) {
    throw "MSIX icon assets missing. Run .\.venv\Scripts\python.exe .\packaging\msix\generate_assets.py first."
}

$makeappx = (Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1).FullName
if (-not $makeappx) {
    throw "makeappx.exe not found. Install the Windows SDK first: winget install -e --id Microsoft.WindowsSDK.10.0.26100"
}

[xml]$xml = Get-Content $manifest
$version = $xml.Package.Identity.Version
$arch    = $xml.Package.Identity.ProcessorArchitecture
$msixOut = Join-Path $outRoot "RemoteMouse_${version}_${arch}.msix"

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $stagingDir
Copy-Item -Recurse -Force $distExeDir $stagingDir

# Ensure no local runtime files ever enter the Store package
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    (Join-Path $stagingDir ".remote_mouse"), `
    (Join-Path $stagingDir "*.err.txt"), `
    (Join-Path $stagingDir "*.log")

Copy-Item $manifest (Join-Path $stagingDir "AppxManifest.xml")
Copy-Item -Recurse -Force $assetsDir (Join-Path $stagingDir "Assets")

& $makeappx pack /d $stagingDir /p $msixOut /o
if ($LASTEXITCODE -ne 0) {
    throw "makeappx pack failed with exit code $LASTEXITCODE"
}

Write-Host "MSIX package ready: $msixOut" -ForegroundColor Green