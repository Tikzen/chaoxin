# Run once as your normal Windows user. The console starts after that user logs in.
$ErrorActionPreference = 'Stop'
$StudyRoot = Split-Path $PSScriptRoot -Parent
$StudyLauncher = Join-Path $StudyRoot 'start-windows.cmd'
$Action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"`"$StudyLauncher`" --no-browser`"" -WorkingDirectory $StudyRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'ChaoxinConsole' -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Chaoxin course dashboard, starts after Windows login' -Force | Out-Null
Write-Host 'Registered ChaoxinConsole. It starts after you log in. Remove with:'
Write-Host 'Unregister-ScheduledTask -TaskName ChaoxinConsole -Confirm:$false'
