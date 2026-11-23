<#
.SYNOPSIS
    Unified Version & Release Manager for Remote MouseKB.
    Bumps versions across pyproject.toml, MSIX AppxManifest.xml, PE headers,
    generates CHANGELOG.md entries from git commits, and tags the release.

.EXAMPLE
    .\scripts\release.ps1 -Type patch -CommitAndTag
    .\scripts\release.ps1 -Type minor
    .\scripts\release.ps1 -Type major
    .\scripts\release.ps1 -Version "1.0.1.0" -CommitAndTag
#>

param(
    [ValidateSet("patch", "fix", "bugfix", "minor", "enhancement", "feature", "major", "breaking")]
    [string]$Type,

    [string]$Version,

    [switch]$CommitAndTag,

    [switch]$Push
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$pyprojectFile = Join-Path $repoRoot "pyproject.toml"
$manifestFile = Join-Path $repoRoot "packaging\msix\AppxManifest.xml"
$changelogFile = Join-Path $repoRoot "CHANGELOG.md"

if (-not (Test-Path $pyprojectFile)) {
    throw "pyproject.toml not found at $pyprojectFile"
}
if (-not (Test-Path $manifestFile)) {
    throw "AppxManifest.xml not found at $manifestFile"
}

# 1. Read current version from pyproject.toml
$pyContent = Get-Content $pyprojectFile -Raw
if ($pyContent -notmatch 'version\s*=\s*"([^"]+)"') {
    throw "Could not determine current version from pyproject.toml"
}
$currentSemver = $matches[1]

# Decompose into major.minor.patch
$parts = $currentSemver.Split(".")
$curMajor = [int]($parts[0])
$curMinor = if ($parts.Count -gt 1) { [int]($parts[1]) } else { 0 }
$curPatch = if ($parts.Count -gt 2) { [int]($parts[2]) } else { 0 }

# 2. Determine target version
if ($Version) {
    # If explicit version passed
    $vClean = $Version.Trim().TrimStart("v")
    $vParts = $vClean.Split(".")
    $newMajor = [int]($vParts[0])
    $newMinor = if ($vParts.Count -gt 1) { [int]($vParts[1]) } else { 0 }
    $newPatch = if ($vParts.Count -gt 2) { [int]($vParts[2]) } else { 0 }
    $newBuild = if ($vParts.Count -gt 3) { [int]($vParts[3]) } else { 0 }
} elseif ($Type) {
    $newBuild = 0
    switch -Regex ($Type) {
        "^(patch|fix|bugfix)$" {
            $newMajor = $curMajor
            $newMinor = $curMinor
            $newPatch = $curPatch + 1
        }
        "^(minor|enhancement|feature)$" {
            $newMajor = $curMajor
            $newMinor = $curMinor + 1
            $newPatch = 0
        }
        "^(major|breaking)$" {
            $newMajor = $curMajor + 1
            $newMinor = 0
            $newPatch = 0
        }
    }
} else {
    throw "Please specify either -Type (patch/minor/major) or an explicit -Version."
}

$newSemver = "$newMajor.$newMinor.$newPatch"
$newQuadVer = "$newMajor.$newMinor.$newPatch.$newBuild"
$tag = "v$newQuadVer"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Remote MouseKB Version Bump Manager" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Current SemVer:      $currentSemver"
Write-Host "New Python SemVer:   $newSemver" -ForegroundColor Green
Write-Host "New Windows QuadVer: $newQuadVer (MSIX & PE binary)" -ForegroundColor Green
Write-Host "Release Tag:         $tag" -ForegroundColor Green
Write-Host "----------------------------------------"

# 3. Update pyproject.toml
$updatedPyContent = [regex]::Replace(
    $pyContent,
    'version\s*=\s*"[^"]+"',
    "version = `"$newSemver`""
)
Set-Content -Path $pyprojectFile -Value $updatedPyContent -NoNewline
Write-Host "[OK] Updated pyproject.toml -> $newSemver" -ForegroundColor Green

# 4. Update AppxManifest.xml
$manifestContent = Get-Content $manifestFile -Raw
$updatedManifest = [regex]::Replace(
    $manifestContent,
    'Version="[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"',
    "Version=`"$newQuadVer`""
)
Set-Content -Path $manifestFile -Value $updatedManifest -NoNewline
Write-Host "[OK] Updated AppxManifest.xml -> $newQuadVer" -ForegroundColor Green

# 5. Extract commits and generate CHANGELOG entry
# Find the latest git tag, or use git root if none
$latestTag = (git -C $repoRoot tag --sort=-creatordate | Select-Object -First 1)

if ($latestTag) {
    $commitLog = (git -C $repoRoot log "$latestTag..HEAD" --oneline)
} else {
    $commitLog = (git -C $repoRoot log --oneline)
}

$features = @()
$fixes = @()
$security = @()
$docsAndChores = @()
$others = @()

foreach ($line in $commitLog) {
    if ($line -match '^[a-f0-9]+\s+(feat|feature)(\(.*\))?:\s*(.+)$') {
        $features += "- $($matches[3]) ($($matches[0].Split(' ')[0]))"
    } elseif ($line -match '^[a-f0-9]+\s+fix\(security\):\s*(.+)$') {
        $security += "- $($matches[1]) ($($matches[0].Split(' ')[0]))"
    } elseif ($line -match '^[a-f0-9]+\s+(fix|bugfix)(\(.*\))?:\s*(.+)$') {
        $fixes += "- $($matches[3]) ($($matches[0].Split(' ')[0]))"
    } elseif ($line -match '^[a-f0-9]+\s+(docs|chore|refactor|build)(\(.*\))?:\s*(.+)$') {
        $docsAndChores += "- $($matches[3]) ($($matches[0].Split(' ')[0]))"
    } elseif ($line -match '^[a-f0-9]+\s+(.+)$') {
        $others += "- $($matches[1]) ($($matches[0].Split(' ')[0]))"
    }
}

$dateStr = (Get-Date).ToString("yyyy-MM-dd")
$newSection = @"
## [$tag] - $dateStr

"@

if ($features.Count -gt 0) {
    $newSection += "### 🚀 Features & Enhancements`n" + ($features -join "`n") + "`n`n"
}
if ($security.Count -gt 0) {
    $newSection += "### 🔒 Security Updates`n" + ($security -join "`n") + "`n`n"
}
if ($fixes.Count -gt 0) {
    $newSection += "### 🐛 Bug Fixes`n" + ($fixes -join "`n") + "`n`n"
}
if ($docsAndChores.Count -gt 0) {
    $newSection += "### 📦 Build & Documentation`n" + ($docsAndChores -join "`n") + "`n`n"
}
if ($others.Count -gt 0 -and ($features.Count + $fixes.Count + $security.Count + $docsAndChores.Count -eq 0)) {
    $newSection += "### Changes`n" + ($others -join "`n") + "`n`n"
}

if (Test-Path $changelogFile) {
    $existingChangelog = Get-Content $changelogFile -Raw
    # Insert after title header if present
    if ($existingChangelog -match '^(# Changelog\r?\n\r?\n)') {
        $finalChangelog = $matches[1] + $newSection + $existingChangelog.Substring($matches[1].Length)
    } else {
        $finalChangelog = "# Changelog`n`n" + $newSection + $existingChangelog
    }
} else {
    $finalChangelog = "# Changelog`n`nAll notable changes to this project will be documented in this file.`n`n" + $newSection
}

Set-Content -Path $changelogFile -Value $finalChangelog -NoNewline
Write-Host "[OK] Updated CHANGELOG.md" -ForegroundColor Green

# 6. Commit and Tag (if requested)
if ($CommitAndTag) {
    git -C $repoRoot add pyproject.toml packaging/msix/AppxManifest.xml CHANGELOG.md packaging/nuitka/build_exe.ps1 .github/workflows/release.yml README.md scripts/release.ps1
    git -C $repoRoot commit -m "chore(release): bump version to $tag"
    git -C $repoRoot tag -a $tag -m "Release $tag"
    Write-Host "[OK] Created Git commit and annotated tag: $tag" -ForegroundColor Green

    if ($Push) {
        Write-Host "Pushing commits and tags to remote origin..." -ForegroundColor Yellow
        git -C $repoRoot push origin HEAD --tags
        Write-Host "[OK] Pushed to remote repository!" -ForegroundColor Green
    } else {
        Write-Host "To push to GitHub, run:" -ForegroundColor Cyan
        Write-Host "  git push origin HEAD --tags" -ForegroundColor White
    }
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Version bump to $tag completed successfully!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
