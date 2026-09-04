param(
    [string]$DataLakePath = "",
    [string]$OutputDirectory = "",
    [ValidateRange(1, 365)]
    [int]$Keep = 14
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $pluginRoot "runtime"
$venvConfigPath = Join-Path $runtimeRoot ".venv-path"
$venvRoot = if ($env:ARGUS_STOCK_COMPARE_VENV) {
    $env:ARGUS_STOCK_COMPARE_VENV
} elseif (Test-Path -LiteralPath $venvConfigPath) {
    (Get-Content -Raw -LiteralPath $venvConfigPath).Trim()
} else {
    ""
}
$pythonPath = @(
    (Join-Path $venvRoot "python.exe"),
    (Join-Path $venvRoot "Scripts\python.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $pythonPath) {
    throw "Rooftop 专用 Python 环境不存在"
}
$dataLakeRoot = if ($DataLakePath) {
    [IO.Path]::GetFullPath($DataLakePath)
} elseif ($env:ARGUS_DATA_LAKE) {
    [IO.Path]::GetFullPath($env:ARGUS_DATA_LAKE)
} else {
    [IO.Path]::GetFullPath((Join-Path $runtimeRoot "data_lake"))
}
$database = Join-Path $dataLakeRoot "db\market_intelligence.db"
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
    throw "Rooftop 数据库不存在：$database"
}
$backupRoot = if ($OutputDirectory) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $dataLakeRoot "backups"))
}
& $pythonPath (Join-Path $PSScriptRoot "backup_argus_data.py") `
    --database $database --output-directory $backupRoot --keep $Keep
exit $LASTEXITCODE
