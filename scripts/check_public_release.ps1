param(
    [string]$RepositoryRoot = ""
)

$ErrorActionPreference = "Stop"
$root = if ($RepositoryRoot) {
    [IO.Path]::GetFullPath($RepositoryRoot)
} else {
    [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}

Push-Location $root
try {
    $tracked = @(git ls-files)
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed"
    }

    $forbiddenPaths = @(
        '^runtime/data_lake/',
        '^data_lake/',
        '^dist/',
        '^runtime/\.venv-path$',
        '(^|/)\.env($|\.)',
        '\.(db|sqlite|sqlite3|safetensors|pt|pth|onnx|token|secret)$'
    )
    $pathViolations = @($tracked | Where-Object {
        $path = $_
        $path -ne ".env.example" -and
            ($forbiddenPaths | Where-Object { $path -match $_ })
    })
    if ($pathViolations) {
        throw "Forbidden runtime or secret paths are tracked: $($pathViolations -join ', ')"
    }

    $secretPatterns = @(
        '-----BEGIN [A-Z ]+PRIVATE KEY-----',
        '\bsk-[A-Za-z0-9_-]{16,}\b',
        '\bghp_[A-Za-z0-9]{20,}\b',
        '\bgithub_pat_[A-Za-z0-9_]{20,}\b',
        '\bxox[baprs]-[A-Za-z0-9-]{20,}\b',
        '\bAKIA[0-9A-Z]{16}\b',
        ('zong' + 'tianyu[\\/]' + 'AppData')
    )
    $contentViolations = @()
    foreach ($file in $tracked) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $file
        if ($item.Length -gt 5MB) { continue }
        $content = Get-Content -Raw -LiteralPath $file -ErrorAction SilentlyContinue
        foreach ($pattern in $secretPatterns) {
            if ($content -match $pattern) {
                $contentViolations += "$file ($pattern)"
            }
        }
    }
    if ($contentViolations) {
        throw "Potential secrets or machine-specific paths found: $($contentViolations -join ', ')"
    }

    [ordered]@{
        status = "ok"
        tracked_files = $tracked.Count
        forbidden_paths = 0
        secret_matches = 0
    } | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
