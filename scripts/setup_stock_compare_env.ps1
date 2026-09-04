param(
    [string]$PythonPath = "",
    [string]$VenvPath = "",
    [string]$PipIndexUrl = ""
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $pluginRoot "runtime"
$requirementsPath = Join-Path $runtimeRoot "requirements-stock-comparison.txt"
$searchRequirementsPath = Join-Path $runtimeRoot "requirements-search.txt"
$venvConfigPath = Join-Path $runtimeRoot ".venv-path"

if (-not $VenvPath) {
    if ($env:ARGUS_STOCK_COMPARE_VENV) {
        $VenvPath = $env:ARGUS_STOCK_COMPARE_VENV
    } elseif (Test-Path -LiteralPath $venvConfigPath) {
        $VenvPath = (Get-Content -Raw -LiteralPath $venvConfigPath).Trim()
    }
}
if (-not $VenvPath) {
    throw "未配置 Rooftop 股票对比虚拟环境路径"
}
if (-not [IO.Path]::IsPathRooted($VenvPath)) {
    $VenvPath = [IO.Path]::GetFullPath((Join-Path $pluginRoot $VenvPath))
}
if (-not $PipIndexUrl) {
    $PipIndexUrl = if ($env:ARGUS_PIP_INDEX_URL) {
        $env:ARGUS_PIP_INDEX_URL
    } else {
        "https://pypi.tuna.tsinghua.edu.cn/simple"
    }
}

if (-not (Test-Path -LiteralPath $requirementsPath)) {
    throw "缺少股票对比依赖清单：$requirementsPath"
}
if (-not (Test-Path -LiteralPath $searchRequirementsPath)) {
    throw "缺少语义检索依赖清单：$searchRequirementsPath"
}

$venvPython = Join-Path $VenvPath "Scripts\python.exe"
$condaPython = Join-Path $VenvPath "python.exe"
$installPython = if ((Test-Path -LiteralPath (Join-Path $VenvPath "conda-meta")) -and (Test-Path -LiteralPath $condaPython)) {
    $condaPython
} elseif (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    ""
}

if (-not $installPython) {
    if (-not $PythonPath) {
        $pythonCandidates = @(
            $env:ARGUS_BOOTSTRAP_PYTHON,
            (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
        $PythonPath = $pythonCandidates | Select-Object -First 1
    }
    if (-not $PythonPath) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand -and $pythonCommand.Source -notmatch "WindowsApps") {
            $PythonPath = $pythonCommand.Source
        }
    }
    if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
        throw "未找到可用于创建虚拟环境的 Python"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VenvPath) | Out-Null
    & $PythonPath -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败，退出码：$LASTEXITCODE"
    }
    $installPython = $venvPython
}

& $installPython -m pip install --disable-pip-version-check --index-url $PipIndexUrl --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "升级虚拟环境基础工具失败，退出码：$LASTEXITCODE"
}
& $installPython -m pip install --disable-pip-version-check --index-url $PipIndexUrl -r $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "安装股票对比依赖失败，退出码：$LASTEXITCODE"
}
& $installPython -m pip install --disable-pip-version-check --index-url $PipIndexUrl -r $searchRequirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "安装语义检索依赖失败，退出码：$LASTEXITCODE"
}

$probe = & $installPython -c "import json, sys; from importlib.metadata import version; print(json.dumps({'python': sys.version.split()[0], 'python_executable': sys.executable, 'akshare': version('akshare'), 'akquant': version('akquant'), 'baostock': version('baostock'), 'tdxrs': version('tdxrs'), 'sentence_transformers': version('sentence-transformers'), 'transformers': version('transformers')}, ensure_ascii=False))"
if ($LASTEXITCODE -ne 0) {
    throw "虚拟环境依赖验证失败，退出码：$LASTEXITCODE"
}

[ordered]@{
    status = "ready"
    virtual_environment = $VenvPath
    requirements = @($requirementsPath, $searchRequirementsPath)
    pip_index_url = $PipIndexUrl
    dependencies = ($probe | ConvertFrom-Json)
} | ConvertTo-Json -Depth 4 -Compress
