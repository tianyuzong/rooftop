[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TokenFile,

    [ValidateNotNullOrEmpty()]
    [string]$PublicHost = "127.0.0.1",

    [ValidateSet("http", "https")]
    [string]$PublicScheme = "https",

    [ValidateNotNullOrEmpty()]
    [string[]]$AllowedRemoteAddress = @(
        '219.142.153.224',
        '240e:306:2889:d101::/64'
    )
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '请在远端 Windows 上以管理员身份运行此脚本。'
}
$resolvedTokenFile = [IO.Path]::GetFullPath($TokenFile)
if (-not (Test-Path -LiteralPath $resolvedTokenFile -PathType Leaf)) {
    throw "远程 API 令牌文件不存在：$resolvedTokenFile"
}
if ([string]::IsNullOrWhiteSpace((Get-Content -Raw -LiteralPath $resolvedTokenFile))) {
    throw "远程 API 令牌文件为空：$resolvedTokenFile"
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
if (-not $listeners) {
    throw "端口 $Port 尚无监听进程。请先让远端量化后端监听 0.0.0.0:$Port，再重新运行。"
}

$nonLoopbackListeners = @($listeners | Where-Object {
    $_.LocalAddress -notin @('127.0.0.1', '::1')
})
if (-not $nonLoopbackListeners) {
    $addresses = ($listeners.LocalAddress | Sort-Object -Unique) -join ', '
    throw "后端只监听回环地址（$addresses）。请把监听地址改为 0.0.0.0 或服务器网卡地址。"
}

$ruleName = "Argus Remote Backend TCP $Port - Codex Frontend"
$existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Set-NetFirewallRule -DisplayName $ruleName -Enabled True -Action Allow `
        -Direction Inbound -Profile Any | Out-Null
    Get-NetFirewallPortFilter -AssociatedNetFirewallRule $existingRule |
        Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port | Out-Null
    Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $existingRule |
        Set-NetFirewallAddressFilter -RemoteAddress $AllowedRemoteAddress | Out-Null
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -RemoteAddress $AllowedRemoteAddress `
        -Profile Any | Out-Null
}

$processes = @($nonLoopbackListeners | ForEach-Object {
    Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
} | Select-Object -ExpandProperty ProcessName -Unique)
$profiles = @(Get-NetConnectionProfile | Select-Object InterfaceAlias, NetworkCategory,
    IPv4Connectivity, IPv6Connectivity)

[ordered]@{
    status = 'ready'
    port = $Port
    listener_addresses = @($nonLoopbackListeners.LocalAddress | Sort-Object -Unique)
    listener_processes = $processes
    firewall_rule = $ruleName
    allowed_remote_addresses = $AllowedRemoteAddress
    token_file = $resolvedTokenFile
    api_auth_header = 'X-Argus-Token'
    public_scheme = $PublicScheme
    tls_termination = if ($PublicScheme -eq 'https') { 'external_reverse_proxy_required' } else { 'not_configured_not_recommended' }
    network_profiles = $profiles
    next_check = "使用 -TokenFile 启动 Argus 后，从浏览器访问 ${PublicScheme}://${PublicHost}:$Port/；首次 API 请求会要求输入令牌"
} | ConvertTo-Json -Depth 4
