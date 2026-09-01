param(
    [int]$StockLimit = 20,
    [double]$MaxDrawdown = 0.15,
    [string]$DataLakePath = ""
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $pluginRoot "runtime"
$configuredDataLakePath = if ($DataLakePath) {
    $DataLakePath
} else {
    Join-Path $runtimeRoot "data_lake"
}
$dataLakeRoot = [IO.Path]::GetFullPath($configuredDataLakePath)
New-Item -ItemType Directory -Force -Path $dataLakeRoot | Out-Null
[Environment]::SetEnvironmentVariable("ARGUS_DATA_LAKE", $dataLakeRoot, "User")
$env:ARGUS_DATA_LAKE = $dataLakeRoot
$runner = Join-Path $PSScriptRoot "run_continuous_learning.ps1"
$launcher = Join-Path $PSScriptRoot "start_stock_compare.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "缺少持续学习运行脚本：$runner"
}
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "缺少股票服务启动脚本：$launcher"
}
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$definitions = @(
    @{ Name = "Argus-Ashare-PreOpen-Learning"; Phase = "PRE_OPEN"; At = "08:45" },
    @{ Name = "Argus-Ashare-PostClose-Learning"; Phase = "POST_CLOSE"; At = "18:00" }
)

$installed = @()
foreach ($definition in $definitions) {
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`" " +
        "-Phase $($definition.Phase) -StockLimit $StockLimit -MaxDrawdown $MaxDrawdown " +
        "-DataLakePath `"$dataLakeRoot`""
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At $definition.At
    Register-ScheduledTask -TaskName $definition.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description "Argus A股持续学习：$($definition.Phase)" `
        -Force | Out-Null
    $installed += Get-ScheduledTask -TaskName $definition.Name | Select-Object TaskName, State
}

$serviceTaskName = "Argus-Ashare-Web-Service"
$serviceArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$launcher`" -View harness -DataLakePath `"$dataLakeRoot`""
$serviceAction = New-ScheduledTaskAction -Execute $powershell -Argument $serviceArguments
$serviceTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $identity),
    (New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At "08:40")
)
Register-ScheduledTask -TaskName $serviceTaskName -Action $serviceAction -Trigger $serviceTriggers `
    -Principal $principal -Settings $settings -Description "Argus 股票研究本地服务自动启动" `
    -Force | Out-Null
$installed += Get-ScheduledTask -TaskName $serviceTaskName | Select-Object TaskName, State

$installed | ConvertTo-Json -Compress
