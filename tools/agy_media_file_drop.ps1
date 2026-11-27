param([Parameter(Mandatory = $true)][string]$FilePath)
$ErrorActionPreference = "Stop"
try {
    Add-Type -AssemblyName System.Windows.Forms
    $resolved = (Resolve-Path -LiteralPath $FilePath -ErrorAction Stop).ProviderPath
    if (-not [System.IO.File]::Exists($resolved)) { throw "Invalid input" }
    $items = New-Object System.Collections.Specialized.StringCollection
    [void]$items.Add($resolved)
} catch {
    Write-Output "FILE_DROP_INPUT_INVALID"
    exit 2
}
try {
    [System.Windows.Forms.Clipboard]::SetFileDropList($items)
    $check = [System.Windows.Forms.Clipboard]::GetFileDropList()
    if ($check.Count -ne 1 -or $check[0] -ne $resolved) {
        Write-Output "FILE_DROP_VERIFY_FAILED"
        exit 3
    }
} catch {
    Write-Output "FILE_DROP_SET_OR_READ_FAILED"
    exit 4
}
Write-Output "FILE_DROP_READY"
