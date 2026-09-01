param(
    [string]$PluginRoot = ""
)

$ErrorActionPreference = "Stop"
$root = if ($PluginRoot) {
    [IO.Path]::GetFullPath($PluginRoot)
} else {
    [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}
$manifestPath = Join-Path $root "MANIFEST.sha256"
$excludedSegments = @(
    "\runtime\data_lake\",
    "\dist\",
    "\__pycache__\",
    "\.git\"
)
$files = Get-ChildItem -LiteralPath $root -File -Recurse | Where-Object {
    $full = $_.FullName
    $_.FullName -ne $manifestPath -and
    $_.Extension -ne ".pyc" -and
    -not ($excludedSegments | Where-Object { $full.Contains($_) })
} | Sort-Object { $_.FullName.Substring($root.Length).Replace("\", "/") }
$lines = foreach ($file in $files) {
    $relative = $file.FullName.Substring($root.Length + 1).Replace("\", "/")
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relative"
}
Set-Content -LiteralPath $manifestPath -Value $lines -Encoding ascii
[ordered]@{
    status = "ok"
    manifest = $manifestPath
    files = $lines.Count
    excludes_mutable_data_lake = $true
    excludes_python_cache = $true
    excludes_dist = $true
} | ConvertTo-Json -Compress
