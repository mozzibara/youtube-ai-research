param([Parameter(Mandatory = $true)][string]$FilePath)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
$resolved = (Resolve-Path -LiteralPath $FilePath -ErrorAction Stop).ProviderPath
if (-not [System.IO.File]::Exists($resolved)) { throw "File not found" }
$items = New-Object System.Collections.Specialized.StringCollection
[void]$items.Add($resolved)
[System.Windows.Forms.Clipboard]::SetFileDropList($items)
$check = [System.Windows.Forms.Clipboard]::GetFileDropList()
if ($check.Count -ne 1 -or $check[0] -ne $resolved) { throw "CF_HDROP verification failed" }
Write-Output "FILE_DROP_READY"
