param(
    [switch]$DebugStderr,
    [string]$Version
)

$ErrorActionPreference = "Stop"
[System.Diagnostics.Process]::GetCurrentProcess().PriorityClass = "BelowNormal"

$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$python   = Join-Path $repoRoot ".venv\Scripts\python.exe"
$entry    = Join-Path $repoRoot "run.py"
$webDir   = Join-Path $repoRoot "src\remote_mouse\web"

# Detect version if not explicitly passed
if (-not $Version) {
    $pyproject = Join-Path $repoRoot "pyproject.toml"
    if (Test-Path $pyproject) {
        $content = Get-Content $pyproject -Raw
        if ($content -match 'version\s*=\s*"([^"]+)"') {
            $rawVer = $matches[1]
            $parts = $rawVer.Split(".")
            while ($parts.Count -lt 4) { $parts += "0" }
            $Version = ($parts[0..3] -join ".")
        }
    }
}
if (-not $Version) {
    $Version = "1.0.0.0"
}

$outDir   = "C:\BuildCache\RemoteMouse\nuitka"
$distDir  = "C:\BuildCache\RemoteMouse\dist\RemoteMouse"
$env:NUITKA_CACHE_DIR = "C:\BuildCache\NuitkaCache"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found at $python"
}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $distDir, (Join-Path $outDir "run.dist")
New-Item -ItemType Directory -Force -Path $outDir, (Split-Path $distDir) | Out-Null

$nuitkaArgs = @(
    "-m", "nuitka",
    "--mode=standalone",
    "--zig",
    "--lto=no",
    "--windows-console-mode=disable",
    "--enable-plugin=tk-inter",
    "--include-package=remote_mouse",
    "--include-package=uvicorn",
    "--include-package=websockets",
    "--include-package=pynput",
    "--include-package=pystray",
    "--include-data-dir=$webDir=remote_mouse/web",
    "--output-dir=$outDir",
    "--output-filename=RemoteMouse.exe",
    "--product-name=Remote Mouse",
    "--file-description=Encrypted Remote Mouse and Keyboard Host",
    "--file-version=$Version",
    "--product-version=$Version",
    "--assume-yes-for-downloads"
)

if ($DebugStderr) {
    $nuitkaArgs += "--force-stderr-spec={TEMP}\RemoteMouse.err.txt"
}

$nuitkaArgs += $entry

& $python @nuitkaArgs

Move-Item -Path (Join-Path $outDir "run.dist") -Destination $distDir
Write-Host "Build complete: $distDir\RemoteMouse.exe" -ForegroundColor Green