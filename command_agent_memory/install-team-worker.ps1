# Устанавливает задание текущего пользователя в Планировщик Windows. Функций нет.
# Вход: абсолютные пути, идентификатор команды и интервал. Выход: имя задания.
param(
    [Parameter(Mandatory)][string]$Config,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$TeamId,
    [Parameter(Mandatory)][int]$Minutes
)
$ErrorActionPreference = 'Stop'
$taskName = "CommandAgentMemory-$TeamId"
$arguments = '-B -X utf8 -m command_agent_memory.team_worker --config "' + $Config + '"'
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory (Split-Path -Parent $Config)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Write-Output $taskName
