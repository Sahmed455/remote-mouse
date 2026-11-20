$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$python   = Join-Path $repoRoot ".venv\Scripts\python.exe"
$entry    = Join-Path $repoRoot "remote_mouse.py"
$outDir   = Join-Path $repoRoot "build\nuitka"
$distDir  = Join-Path $repoRoot "dist\RemoteMouse"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found at $python"
}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $outDir, $distDir
New-Item -ItemType Directory -Force -Path $outDir, (Join-Path $repoRoot "dist") | Out-Null

& $python -m nuitka `
  --mode=standalone `
  --zig `
  --windows-console-mode=disable `
  --enable-plugin=tk-inter `
  --include-package=uvicorn `
  --include-package=websockets `
  --include-package=pynput `
  --include-package=pystray `
  --output-dir="$outDir" `
  --output-filename=RemoteMouse.exe `
  --product-name="Remote Mouse" `
  --file-description="Encrypted Remote Mouse and Keyboard Host" `
  --file-version=1.0.0.0 `
  --product-version=1.0.0.0 `
  --assume-yes-for-downloads `
  "$entry"

Move-Item -Path (Join-Path $outDir "remote_mouse.dist") -Destination $distDir
Write-Host "Build complete: $distDir\RemoteMouse.exe" -ForegroundColor Green