param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("PRE_OPEN", "POST_CLOSE", "BACKFILL")]
    [string]$Phase,
    [int]$StockLimit = 20,
    [double]$MaxDrawdown = 0.15,
    [string]$DataLakePath = ""
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $pluginRoot "runtime"
$userDataLakePath = [Environment]::GetEnvironmentVariable("ARGUS_DATA_LAKE", "User")
$configuredDataLakePath = if ($DataLakePath) {
    $DataLakePath
} elseif ($env:ARGUS_DATA_LAKE) {
    $env:ARGUS_DATA_LAKE
} elseif ($userDataLakePath) {
    $userDataLakePath
} else {
    Join-Path $runtimeRoot "data_lake"
}
$dataLakeRoot = [IO.Path]::GetFullPath($configuredDataLakePath)
New-Item -ItemType Directory -Force -Path $dataLakeRoot | Out-Null
$env:ARGUS_DATA_LAKE = $dataLakeRoot
$venvRoot = (Get-Content -Raw -LiteralPath (Join-Path $runtimeRoot ".venv-path")).Trim()
$pythonPath = Join-Path $venvRoot "python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $venvRoot "Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Argus dedicated Python environment does not exist"
}

$env:ARGUS_MARKET_PROVIDER = "tdx"
$env:PYTHONUTF8 = "1"
if (-not $env:ARGUS_TDX_HOME) {
    $tdxProcess = Get-Process -Name "TdxW" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tdxProcess -and $tdxProcess.Path) {
        $env:ARGUS_TDX_HOME = Split-Path -Parent $tdxProcess.Path
    } else {
        $tdxCandidate = @("C:\new_tdx64", "D:\new_tdx64", "E:\new_tdx64") |
            Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
            Select-Object -First 1
        if ($tdxCandidate) { $env:ARGUS_TDX_HOME = $tdxCandidate }
    }
}
$logDir = Join-Path $dataLakeRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "continuous_learning_tasks.log"
$stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$stamp] starting $Phase" | Add-Content -LiteralPath $logPath -Encoding UTF8

Push-Location $runtimeRoot
try {
    & $pythonPath -m app.continuous_learning --phase $Phase `
        --stock-limit $StockLimit --max-drawdown $MaxDrawdown `
        --trigger-kind windows_task 2>&1 | Add-Content -LiteralPath $logPath -Encoding UTF8
    if ($LASTEXITCODE -ne 0) {
        throw "Continuous learning task failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
