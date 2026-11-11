# uninstall_autostart.ps1
# Removes the Windows Scheduled Task named "MunichScraper".

$ErrorActionPreference = "SilentlyContinue"
$taskName = "MunichScraper"

Write-Host "Unregistering Scheduled Task '$taskName'..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Scheduled task '$taskName' removed."

