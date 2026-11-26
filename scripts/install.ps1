$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstalledRoot = Join-Path $env:USERPROFILE ".gemini\config\plugins\wechat-assistant"

Write-Host "[1/2] Installing WeChat Assistant plugin into Antigravity..." -ForegroundColor Cyan
$installed = $false
try {
    & agy plugin install $RepoRoot
    if ($LASTEXITCODE -eq 0 -and (Test-Path $InstalledRoot)) {
        $installed = $true
    }
} catch {
    # Windows file-watcher/Defender can occasionally block temp-dir rename in `agy plugin install`
}

if (-not $installed) {
    Write-Host "  Falling back to direct copy into $InstalledRoot ..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstalledRoot) | Out-Null
    if (Test-Path $InstalledRoot) {
        Remove-Item -Recurse -Force $InstalledRoot
    }
    Copy-Item -Path $RepoRoot -Destination $InstalledRoot -Recurse -Force
    $gitDir = Join-Path $InstalledRoot ".git"
    if (Test-Path $gitDir) {
        Remove-Item -Recurse -Force $gitDir
    }
}

Write-Host "[2/2] Launching guided first-run setup wizard..." -ForegroundColor Cyan
node (Join-Path $InstalledRoot "scripts\run-py.mjs") (Join-Path $InstalledRoot "sidecars\bridge\setup.py") --all
