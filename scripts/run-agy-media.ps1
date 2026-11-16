param(
    [Parameter(Mandatory = $true)][string]$Run,
    [ValidateSet("audio", "video")][string]$MediaType = "audio",
    [ValidateSet("wav", "mp3")][string]$AudioFormat = "wav",
    [ValidateSet("clipboard", "file-read")][string]$Transport = "clipboard",
    [switch]$Submit,
    [switch]$DryRun,
    [int]$StartupSeconds = 90,
    [int]$TimeoutSeconds = 600
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Missing venv python" }
$cmd = @((Join-Path $root "tools\agy_media_probe.py"),
    "--run", $Run, "--media-type", $MediaType, "--audio-format", $AudioFormat,
    "--transport", $Transport,
    "--startup-seconds", "$StartupSeconds",
    "--timeout-seconds", "$TimeoutSeconds")
if ($DryRun) { $cmd += "--dry-run" }
elseif ($Submit) { $cmd += "--submit" }
else { $cmd += "--attachment-probe" }
& $python @cmd
if ($LASTEXITCODE -ne 0) { throw "AGY probe failed with exit code $LASTEXITCODE" }
