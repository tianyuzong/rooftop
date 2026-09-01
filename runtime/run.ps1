param(
    [int]$Port = 8765,
    [string]$BindAddress = "127.0.0.1",
    [string]$PublicHost = ""
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
Write-Host "Starting Argus local analysis platform..." -ForegroundColor Cyan
$pluginRoot = Split-Path -Parent $PSScriptRoot
$venvPath = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot ".venv-path")).Trim()
if (-not [IO.Path]::IsPathRooted($venvPath)) {
    $venvPath = [IO.Path]::GetFullPath((Join-Path $pluginRoot $venvPath))
}
$Python = @(
    (Join-Path $venvPath "python.exe"),
    (Join-Path $venvPath "Scripts\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Python) { throw "Argus Python environment is not ready: $venvPath" }
if (-not $PublicHost) { $PublicHost = if ($BindAddress -in @("0.0.0.0", "::")) { "127.0.0.1" } else { $BindAddress } }
if (Test-Path -LiteralPath (Join-Path $venvPath "conda-meta")) { $env:CONDA_PREFIX = $venvPath }
$env:ARGUS_BIND_ADDRESS = $BindAddress
$env:ARGUS_PUBLIC_HOST = $PublicHost
$displayHost = if ($PublicHost.Contains(":") -and -not $PublicHost.StartsWith("[")) { "[$PublicHost]" } else { $PublicHost }
Write-Host "Open http://${displayHost}:$Port in your browser." -ForegroundColor Green
& $Python (Join-Path $pluginRoot "scripts\serve.py") --root $PSScriptRoot --host $BindAddress --port $Port
