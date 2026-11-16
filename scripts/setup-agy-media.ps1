param([switch]$SkipInstall)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
Push-Location $root
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        throw "Existing project .venv not found. Create/activate it first."
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not $SkipInstall) {
        & $python -m pip install -r requirements-agy-media.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    }
    foreach ($name in @("ffmpeg", "ffprobe", "agy", "wt.exe")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $found) { Write-Warning "Missing command in PATH: $name" }
        else { Write-Host "FOUND $name" }
    }
    & $python -m unittest discover -s tests -p test_agy_media_probe.py -v
    if ($LASTEXITCODE -ne 0) { throw "AGY offline tests failed." }
} finally { Pop-Location }
