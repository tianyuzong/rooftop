[CmdletBinding()]
param(
    [string]$PluginRoot = "",
    [string]$ZCodeHome = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$resolvedPluginRoot = if ($PluginRoot) {
    [IO.Path]::GetFullPath($PluginRoot)
} else {
    [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
}
$manifestPath = Join-Path $resolvedPluginRoot ".zcode-plugin\plugin.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "ZCode plugin manifest not found: $manifestPath"
}
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if (-not ([string]$manifest.name -match '^[a-z0-9][a-z0-9._-]{0,127}$')) {
    throw "Invalid ZCode plugin name: $($manifest.name)"
}

function Resolve-PluginComponent([string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "ZCode component paths must be relative: $RelativePath"
    }
    $resolvedPath = [IO.Path]::GetFullPath((Join-Path $resolvedPluginRoot $RelativePath))
    $rootPrefix = $resolvedPluginRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "ZCode component path escapes the plugin root: $RelativePath"
    }
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
        throw "ZCode component directory not found: $resolvedPath"
    }
    return $resolvedPath
}

function Test-SamePath([string]$Left, [string]$Right) {
    $leftPath = [IO.Path]::GetFullPath($Left).TrimEnd('\', '/')
    $rightPath = [IO.Path]::GetFullPath($Right).TrimEnd('\', '/')
    return $leftPath.Equals($rightPath, [StringComparison]::OrdinalIgnoreCase)
}

$sourceSkillsRoot = Resolve-PluginComponent ([string]$manifest.skills)
$sourceCommandsRoot = Resolve-PluginComponent ([string]$manifest.commands)
$resolvedZCodeHome = if ($ZCodeHome) {
    [IO.Path]::GetFullPath($ZCodeHome)
} elseif ($env:ZCODE_HOME) {
    [IO.Path]::GetFullPath($env:ZCODE_HOME)
} else {
    Join-Path ([Environment]::GetFolderPath("UserProfile")) ".zcode"
}
$targetSkillsRoot = Join-Path $resolvedZCodeHome "skills"
$targetCommandsRoot = Join-Path $resolvedZCodeHome "commands"
New-Item -ItemType Directory -Path $targetSkillsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $targetCommandsRoot -Force | Out-Null

$registeredSkills = @()
foreach ($sourceSkill in @(Get-ChildItem -LiteralPath $sourceSkillsRoot -Directory |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf })) {
    $destination = Join-Path $targetSkillsRoot $sourceSkill.Name
    $mode = "created"
    if (Test-Path -LiteralPath $destination) {
        $existing = Get-Item -LiteralPath $destination -Force
        $existingTarget = @($existing.Target | Where-Object { $_ }) | Select-Object -First 1
        if ($existing.LinkType -eq "Junction" -and $existingTarget -and
            (Test-SamePath $existingTarget $sourceSkill.FullName)) {
            $mode = "reused"
        } elseif (-not $Force) {
            throw "ZCode skill destination already exists and is not this plugin's junction: $destination. Re-run with -Force to replace it."
        } else {
            $targetPrefix = [IO.Path]::GetFullPath($targetSkillsRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
            $resolvedDestination = [IO.Path]::GetFullPath($destination)
            if (-not $resolvedDestination.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to replace a path outside the ZCode skills directory: $resolvedDestination"
            }
            Remove-Item -LiteralPath $destination -Recurse -Force
            New-Item -ItemType Junction -Path $destination -Target $sourceSkill.FullName | Out-Null
            $mode = "replaced"
        }
    } else {
        New-Item -ItemType Junction -Path $destination -Target $sourceSkill.FullName | Out-Null
    }
    $registeredSkills += [ordered]@{ name=$sourceSkill.Name; path=$destination; target=$sourceSkill.FullName; mode=$mode }
}

$registeredCommands = @()
foreach ($sourceCommand in @(Get-ChildItem -LiteralPath $sourceCommandsRoot -Filter "*.md" -File)) {
    $destination = Join-Path $targetCommandsRoot $sourceCommand.Name
    $mode = "created"
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        $sourceHash = (Get-FileHash -LiteralPath $sourceCommand.FullName -Algorithm SHA256).Hash
        $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        if ($sourceHash -eq $destinationHash) {
            $mode = "reused"
        } elseif (-not $Force) {
            throw "ZCode command destination already exists with different content: $destination. Re-run with -Force to replace it."
        } else {
            Copy-Item -LiteralPath $sourceCommand.FullName -Destination $destination -Force
            $mode = "updated"
        }
    } else {
        Copy-Item -LiteralPath $sourceCommand.FullName -Destination $destination
    }
    $registeredCommands += [ordered]@{ name=[IO.Path]::GetFileNameWithoutExtension($sourceCommand.Name); path=$destination; mode=$mode }
}

[ordered]@{
    status = "ready"
    plugin = [string]$manifest.name
    version = [string]$manifest.version
    plugin_root = $resolvedPluginRoot
    zcode_home = $resolvedZCodeHome
    skills = $registeredSkills
    commands = $registeredCommands
    invocation = "/rooftop-stock"
    compatibility_invocation = "/argus-stock"
} | ConvertTo-Json -Depth 5
