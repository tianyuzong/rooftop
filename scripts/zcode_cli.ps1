param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments,
    [string]$ZCodeCliPath = ""
)

$ErrorActionPreference = "Stop"
$zcodeCli = if ($ZCodeCliPath) {
    $ZCodeCliPath
} elseif ($env:ZCODE_CLI_PATH) {
    $env:ZCODE_CLI_PATH
} elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "Programs\ZCode\resources\glm\zcode.cjs"
} else {
    ""
}
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue

if (-not $zcodeCli -or -not (Test-Path -LiteralPath $zcodeCli)) {
    throw "ZCode CLI not found. Set ZCODE_CLI_PATH or pass -ZCodeCliPath."
}

if (-not $nodeCommand) {
    throw "Node.js was not found on PATH."
}

& $nodeCommand.Source $zcodeCli @Arguments
exit $LASTEXITCODE
