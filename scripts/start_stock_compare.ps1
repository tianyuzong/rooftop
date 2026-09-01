param(
    [string]$Stocks = "",
    [string]$Profile = "balanced",
    [ValidateSet("auto", "signals", "compare", "harness")]
    [string]$View = "auto",
    [int]$Port = 0,
    [string]$BindAddress = "127.0.0.1",
    [string]$PublicHost = "",
    [ValidateSet("http", "https")]
    [string]$PublicScheme = "http",
    [string]$DataLakePath = "",
    [string]$TokenFile = "",
    [string]$AllowedHosts = "",
    [string]$AllowedOrigins = "",
    [ValidateRange(30, 10000)]
    [int]$ApiRateLimitPerMinute = 180
)

$ErrorActionPreference = "Stop"
$profileMap = @{
    "aggressive" = "aggressive"
    "激进" = "aggressive"
    "激进派" = "aggressive"
    "balanced" = "balanced"
    "neutral" = "balanced"
    "中立" = "balanced"
    "中间" = "balanced"
    "中间派" = "balanced"
    "均衡" = "balanced"
    "conservative" = "conservative"
    "保守" = "conservative"
    "保守派" = "conservative"
}

$normalizedProfile = $profileMap[$Profile.Trim().ToLowerInvariant()]
if (-not $normalizedProfile) {
    throw "投资态度必须是激进派、中间派或保守派"
}

$stockItems = @($Stocks -split '[,，、;；\s]+' | Where-Object { $_ })
$resolvedView = if ($View -eq "auto") {
    if ($stockItems.Count) { "compare" } else { "signals" }
} else {
    $View
}
if ($resolvedView -eq "compare" -and ($stockItems.Count -lt 2 -or $stockItems.Count -gt 8)) {
    throw "请输入 2-8 只股票名称或代码"
}

$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $pluginRoot "runtime"
$serveScript = Join-Path $PSScriptRoot "serve.py"
if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot "app\server.py"))) {
    throw "插件运行文件不完整：$runtimeRoot"
}

$resolvedTokenFile = ""
if ($TokenFile) {
    $resolvedTokenFile = [IO.Path]::GetFullPath($TokenFile)
    if (-not (Test-Path -LiteralPath $resolvedTokenFile -PathType Leaf)) {
        throw "令牌文件不存在：$resolvedTokenFile"
    }
    if ([string]::IsNullOrWhiteSpace((Get-Content -Raw -LiteralPath $resolvedTokenFile))) {
        throw "令牌文件为空：$resolvedTokenFile"
    }
}

function Get-ArgusAuthHeaders {
    if (-not $resolvedTokenFile) {
        return @{}
    }
    return @{ "X-Argus-Token" = (Get-Content -Raw -LiteralPath $resolvedTokenFile).Trim() }
}

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

$venvConfigPath = Join-Path $runtimeRoot ".venv-path"
$venvRoot = if ($env:ARGUS_STOCK_COMPARE_VENV) {
    $env:ARGUS_STOCK_COMPARE_VENV
} elseif (Test-Path -LiteralPath $venvConfigPath) {
    (Get-Content -Raw -LiteralPath $venvConfigPath).Trim()
} else {
    ""
}
if ($venvRoot -and -not [IO.Path]::IsPathRooted($venvRoot)) {
    $venvRoot = [IO.Path]::GetFullPath((Join-Path $pluginRoot $venvRoot))
}
$pythonPath = if ($venvRoot) {
    @(
        (Join-Path $venvRoot "python.exe"),
        (Join-Path $venvRoot "Scripts\python.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
} else { "" }
if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw "Argus 股票对比专用虚拟环境尚未准备，请先运行 scripts\setup_stock_compare_env.ps1"
}
$parsedBindAddress = $null
if (-not [Net.IPAddress]::TryParse($BindAddress, [ref]$parsedBindAddress)) {
    throw "绑定地址必须是有效的 IPv4 或 IPv6 地址"
}
$bindIsLoopback = [Net.IPAddress]::IsLoopback($parsedBindAddress)
if (-not $bindIsLoopback -and -not $resolvedTokenFile) {
    throw "非回环监听必须通过 -TokenFile 提供远程 API 令牌"
}
$bindIsWildcard = $BindAddress -in @("0.0.0.0", "::")
if (-not $PublicHost) {
    $PublicHost = if ($bindIsWildcard) { "127.0.0.1" } else { $BindAddress }
}
$probeHost = if ($BindAddress -eq "::") { "::1" } elseif ($BindAddress -eq "0.0.0.0") { "127.0.0.1" } else { $BindAddress }
$marketProvider = "tdx"
$env:ARGUS_MARKET_PROVIDER = $marketProvider
$env:ARGUS_BIND_ADDRESS = $BindAddress
$env:ARGUS_PUBLIC_HOST = $PublicHost
$env:ARGUS_API_RATE_LIMIT_PER_MINUTE = [string]$ApiRateLimitPerMinute
if ($AllowedHosts) { $env:ARGUS_ALLOWED_HOSTS = $AllowedHosts }
if ($AllowedOrigins) { $env:ARGUS_ALLOWED_ORIGINS = $AllowedOrigins }
if (Test-Path -LiteralPath (Join-Path $venvRoot "conda-meta")) {
    $env:CONDA_PREFIX = $venvRoot
} else {
    Remove-Item Env:CONDA_PREFIX -ErrorAction SilentlyContinue
}
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

function Format-HttpHost([string]$HostName) {
    if ($HostName.StartsWith("[") -and $HostName.EndsWith("]")) {
        return $HostName
    }
    $parsedHost = $null
    if ([Net.IPAddress]::TryParse($HostName, [ref]$parsedHost) -and
        $parsedHost.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        return "[$HostName]"
    }
    return $HostName
}

function Get-RuntimeRevision([string]$DirectoryPath) {
    $revisionScript = Join-Path $PSScriptRoot "runtime_revision.py"
    if (-not (Test-Path -LiteralPath $revisionScript)) {
        throw "运行时代码指纹脚本不存在：$revisionScript"
    }
    $revision = & $pythonPath $revisionScript $DirectoryPath
    if ($LASTEXITCODE -ne 0 -or -not $revision) {
        throw "无法计算运行时代码指纹：$DirectoryPath"
    }
    return ([string]$revision).Trim()
}

$runtimeRevision = Get-RuntimeRevision $runtimeRoot
$pluginVersion = [string]((Get-Content -Raw -LiteralPath `
    (Join-Path $pluginRoot ".codex-plugin\plugin.json") | ConvertFrom-Json).version)

function Test-ComparisonServer(
    [int]$CandidatePort,
    [string]$ExpectedEnvironment,
    [string]$ExpectedPluginVersion,
    [string]$ExpectedRuntimeRevision,
    [string]$ExpectedDataLake,
    [string]$NetworkHost = "127.0.0.1"
) {
    try {
        $uriHost = Format-HttpHost $NetworkHost
        $scriptResponse = Invoke-WebRequest -Uri "http://${uriHost}:$CandidatePort/stock-compare.js" -TimeoutSec 5 -UseBasicParsing
        $health = Invoke-RestMethod -Uri "http://${uriHost}:$CandidatePort/api/health" `
            -Headers (Get-ArgusAuthHeaders) -TimeoutSec 5 -UseBasicParsing
        $actualEnvironment = [IO.Path]::GetFullPath([string]$health.runtime.virtual_environment)
        $expectedEnvironmentPath = [IO.Path]::GetFullPath($ExpectedEnvironment)
        $actualDataLakePath = [IO.Path]::GetFullPath([string]$health.runtime.data_lake)
        $expectedDataLakePath = [IO.Path]::GetFullPath($ExpectedDataLake)
        return $scriptResponse.StatusCode -eq 200 `
            -and $scriptResponse.Content.Contains("runComparison") `
            -and $health.runtime.is_virtual_environment `
            -and $health.market_data.provider -eq $marketProvider `
            -and $health.concurrency.shared_service `
            -and $health.concurrency.refresh_coalescing `
            -and $actualEnvironment.Equals($expectedEnvironmentPath, [StringComparison]::OrdinalIgnoreCase) `
            -and $actualDataLakePath.Equals($expectedDataLakePath, [StringComparison]::OrdinalIgnoreCase) `
            -and ([string]$health.runtime.plugin_version).Equals($ExpectedPluginVersion, [StringComparison]::Ordinal) `
            -and ([string]$health.runtime.runtime_revision).Equals($ExpectedRuntimeRevision, [StringComparison]::Ordinal)
    } catch {
        return $false
    }
}

function Test-PortInUse([int]$CandidatePort) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connected = $client.ConnectAsync("127.0.0.1", $CandidatePort)
        return $connected.Wait(150) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

$launcherMutex = [Threading.Mutex]::new($false, "Local\ArgusStockComparisonLauncher")
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $launcherMutex.WaitOne([TimeSpan]::FromSeconds(45))
    } catch [Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "等待 Argus 股票对比共享服务启动锁超时"
    }

    $selectedPort = $Port
    $reused = $false
    if ($selectedPort -eq 0) {
    $listeningPorts = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | ForEach-Object Port)
    foreach ($candidate in 8765..8795) {
        if (($listeningPorts -contains $candidate) -and (Test-ComparisonServer $candidate $venvRoot $pluginVersion $runtimeRevision $dataLakeRoot $probeHost)) {
            $selectedPort = $candidate
            $reused = $true
            break
        }
    }
    if ($selectedPort -eq 0) {
        foreach ($candidate in 8765..8795) {
            if (-not ($listeningPorts -contains $candidate)) {
                $selectedPort = $candidate
                break
            }
        }
    }
    } elseif ((Test-PortInUse $selectedPort) -and (Test-ComparisonServer $selectedPort $venvRoot $pluginVersion $runtimeRevision $dataLakeRoot $probeHost)) {
        $reused = $true
    } elseif (Test-PortInUse $selectedPort) {
        throw "端口 $selectedPort 已被其他程序占用"
    }

    if ($selectedPort -eq 0) {
        throw "8765-8795 端口范围内没有可用端口"
    }

    $processId = $null
    if (-not $reused) {
    $logDir = Join-Path $dataLakeRoot "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $env:ARGUS_LIVE_REFRESH_ENABLED = "1"
    $env:ARGUS_REPORT_REFRESH_ENABLED = "1"
    $env:ARGUS_CONTINUOUS_LEARNING_ENABLED = "1"
    $previousTokenFile = $env:ARGUS_REMOTE_TOKEN_FILE
    try {
        if ($resolvedTokenFile) {
            $env:ARGUS_REMOTE_TOKEN_FILE = $resolvedTokenFile
        } else {
            Remove-Item Env:ARGUS_REMOTE_TOKEN_FILE -ErrorAction SilentlyContinue
        }
        $process = Start-Process -FilePath $pythonPath `
            -ArgumentList @($serveScript, "--root", $runtimeRoot, "--host", $BindAddress, "--port", $selectedPort) `
            -WorkingDirectory $runtimeRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir "plugin-server.out.log") `
            -RedirectStandardError (Join-Path $logDir "plugin-server.err.log")
    } finally {
        if ($null -ne $previousTokenFile) {
            $env:ARGUS_REMOTE_TOKEN_FILE = $previousTokenFile
        } else {
            Remove-Item Env:ARGUS_REMOTE_TOKEN_FILE -ErrorAction SilentlyContinue
        }
    }
    $processId = $process.Id

    $ready = $false
    foreach ($attempt in 1..60) {
        if (Test-ComparisonServer $selectedPort $venvRoot $pluginVersion $runtimeRevision $dataLakeRoot $probeHost) {
            $ready = $true
            break
        }
        if ($process.HasExited) {
            $errorLog = Join-Path $logDir "plugin-server.err.log"
            $details = if (Test-Path -LiteralPath $errorLog) { Get-Content -Raw $errorLog } else { "未知错误" }
            throw "本地服务启动失败：$details"
        }
        Start-Sleep -Milliseconds 250
    }
        if (-not $ready) {
            throw "本地服务启动超时，请查看 $logDir"
        }
    }
} finally {
    if ($mutexAcquired) {
        $launcherMutex.ReleaseMutex()
    }
    $launcherMutex.Dispose()
}

$stockQuery = [Uri]::EscapeDataString(($stockItems -join ','))
$urlHost = Format-HttpHost $PublicHost
$url = if ($resolvedView -eq "compare") {
    "${PublicScheme}://${urlHost}:$selectedPort/?view=compare&stocks=$stockQuery&profile=$normalizedProfile"
} else {
    "${PublicScheme}://${urlHost}:$selectedPort/?view=$resolvedView"
}
[ordered]@{
    status = "ready"
    url = $url
    view = $resolvedView
    stocks = $stockItems
    profile = $normalizedProfile
    port = $selectedPort
    reused_server = $reused
    process_id = $processId
    python_executable = $pythonPath
    virtual_environment = $venvRoot
    data_lake = $dataLakeRoot
    network = [ordered]@{
        bind_address = $BindAddress
        public_host = $PublicHost
        remote_access = -not [Net.IPAddress]::IsLoopback($parsedBindAddress)
        dual_stack = $BindAddress -eq "::"
    }
    market_data = [ordered]@{
        provider = $marketProvider
        tdx_home = $env:ARGUS_TDX_HOME
        storage = "local_sqlite"
    }
    concurrency = [ordered]@{
        shared_service = $true
        isolated_links = $true
        startup_mutex = $true
    }
    api_key_json = $false
    auth_header = if ($resolvedTokenFile) { "X-Argus-Token" } else { $null }
    security = [ordered]@{
        api_token_required = -not $bindIsLoopback
        public_scheme = $PublicScheme
        allowed_hosts = $AllowedHosts
        allowed_origins = $AllowedOrigins
        api_rate_limit_per_minute = $ApiRateLimitPerMinute
        tls_termination = if ($PublicScheme -eq "https") { "external_reverse_proxy" } else { "not_configured" }
    }
    order_execution = $false
} | ConvertTo-Json -Compress
