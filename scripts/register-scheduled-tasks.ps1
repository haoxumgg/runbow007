param(
    [string]$ConfigPath = "config.yaml",
    [switch]$EnableSending
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $ProjectRoot ".venv\Scripts\runbow007.exe"
$ResolvedConfig = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot $ConfigPath)).Path

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "未找到 $Executable，请先运行 scripts\install.ps1"
}

$SendArgument = if ($EnableSending) { " --send" } else { "" }
$HourlyArguments = "--config `"$ResolvedConfig`" run --rules R1,R3,R4$SendArgument"
$ArrivalArguments = "--config `"$ResolvedConfig`" run --rules R2$SendArgument"

$HourlyAction = New-ScheduledTaskAction -Execute $Executable -Argument $HourlyArguments -WorkingDirectory $ProjectRoot
$HourlyTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$ArrivalAction = New-ScheduledTaskAction -Execute $Executable -Argument $ArrivalArguments -WorkingDirectory $ProjectRoot
$ArrivalTrigger = New-ScheduledTaskTrigger -Daily -At "13:30"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Runbow007-Hourly" -Action $HourlyAction -Trigger $HourlyTrigger -Settings $Settings -Description "007 每小时检查 WMS、合同和延迟提醒" -Force | Out-Null
Register-ScheduledTask -TaskName "Runbow007-Arrival" -Action $ArrivalAction -Trigger $ArrivalTrigger -Settings $Settings -Description "007 每天13:30发送预计到达提醒" -Force | Out-Null

Write-Host "定时任务注册完成。EnableSending=$EnableSending"
