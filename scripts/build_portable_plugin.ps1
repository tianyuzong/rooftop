param(
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $pluginRoot "dist"
}

$manifest = Get-Content (Join-Path $pluginRoot ".codex-plugin\plugin.json") -Raw | ConvertFrom-Json
$version = [string]$manifest.version
$safeVersion = $version -replace '[^0-9A-Za-z.+_-]', '_'
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)

New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
$archivePath = Join-Path $resolvedOutput "argus-stock-comparison-$safeVersion-source.zip"
$checksumPath = "$archivePath.sha256"

$sourceItems = @(
    ".codex-plugin",
    ".zcode-plugin",
    "commands",
    "skills",
    "scripts",
    "runtime\app",
    "runtime\docs",
    "runtime\tests",
    "runtime\requirements-data.txt",
    "runtime\requirements-research.txt",
    "runtime\requirements-search.txt",
    "runtime\requirements-stock-comparison.txt",
    "runtime\run.bat",
    "runtime\run.ps1",
    "README.md",
    "LICENSE"
)

foreach ($item in $sourceItems) {
    if (-not (Test-Path -LiteralPath (Join-Path $pluginRoot $item))) {
        throw "Required package item is missing: $item"
    }
}

foreach ($target in @($archivePath, $checksumPath)) {
    $resolvedTarget = [System.IO.Path]::GetFullPath($target)
    if (-not $resolvedTarget.StartsWith($resolvedOutput + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace a file outside the output directory: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget) {
        Remove-Item -LiteralPath $resolvedTarget -Force
    }
}

Push-Location $pluginRoot
try {
    $tarArguments = @(
        "-a",
        "-c",
        "-f",
        $archivePath,
        "--exclude=*/__pycache__/*",
        "--exclude=*.pyc"
    ) + $sourceItems
    & tar.exe @tarArguments
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $checksumPath -Value "$hash  $([System.IO.Path]::GetFileName($archivePath))" -Encoding ascii
$archive = Get-Item -LiteralPath $archivePath

[ordered]@{
    status = "ok"
    version = $version
    archive = $archive.FullName
    bytes = $archive.Length
    sha256 = $hash
    checksum_file = $checksumPath
    includes_mutable_data_lake = $false
    includes_model_weights = $false
    includes_python_environment = $false
} | ConvertTo-Json
