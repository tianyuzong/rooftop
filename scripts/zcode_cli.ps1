[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments,
    [string]$ZCodeCliPath = "",
    [string]$NodePath = ""
)

$ErrorActionPreference = "Stop"

function Select-ExistingFile([string[]]$Candidates) {
    foreach ($candidate in @($Candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return ""
}

$zcodeProcessPaths = @(Get-Process -Name "ZCode" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path } |
    Select-Object -ExpandProperty Path -Unique)
$zcodeCliCandidates = @($ZCodeCliPath, $env:ZCODE_CLI_PATH)
foreach ($processPath in $zcodeProcessPaths) {
    $zcodeCliCandidates += Join-Path (Split-Path -Parent $processPath) "resources\glm\zcode.cjs"
}
if ($env:LOCALAPPDATA) {
    $zcodeCliCandidates += Join-Path $env:LOCALAPPDATA "Programs\ZCode\resources\glm\zcode.cjs"
}
$profileName = Split-Path -Leaf ([Environment]::GetFolderPath("UserProfile"))
foreach ($drive in @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
    $zcodeCliCandidates += Join-Path $drive.Root "Users\$profileName\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs"
}
$zcodeCli = Select-ExistingFile $zcodeCliCandidates
if (-not $zcodeCli) {
    throw "ZCode CLI not found. Set ZCODE_CLI_PATH or pass -ZCodeCliPath."
}

$nodeCandidates = @($NodePath, $env:ZCODE_NODE_PATH)
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if ($nodeCommand) {
    $nodeCandidates += $nodeCommand.Source
}
$userProfile = [Environment]::GetFolderPath("UserProfile")
$nodeCandidates += Join-Path $userProfile ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
foreach ($drive in @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
    $nodeCandidates += Join-Path $drive.Root "Users\$profileName\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
}
if ($env:ProgramFiles) {
    $nodeCandidates += Join-Path $env:ProgramFiles "nodejs\node.exe"
}
$resolvedNodePath = Select-ExistingFile $nodeCandidates
if (-not $resolvedNodePath) {
    throw "Node.js was not found. Install Node.js, set ZCODE_NODE_PATH, or pass -NodePath."
}

& $resolvedNodePath $zcodeCli @Arguments
exit $LASTEXITCODE
