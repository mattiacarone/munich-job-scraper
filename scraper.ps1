# munichScraper control (updated 7 Nov 2026; 17:30 fix: every call is wrapped in @() because PowerShell unrolls a one-item array, so a lone supervisor process counted as 0). Nothing starts on its own: you choose.
#
#   Desktop icons: "Start Scraper", "Stop Scraper", "Scraper Status" run this file with -Pause.
#
#   powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 start    # continuous, in the background (new cycle 10 min after the last ends)
#   powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 stop     # stops supervisor + daemon, then checks that nothing is left
#   powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 status   # running or stopped? last activity?
#   powershell -ExecutionPolicy Bypass -File C:\Users\Mattia\munichScraper\scraper.ps1 once     # one cycle, in this window, then exits
param([ValidateSet('once', 'start', 'stop', 'status')][string]$Mode = 'status', [switch]$Pause)

$dir = 'C:\Users\Mattia\munichScraper'
Set-Location $dir

function Get-ScraperProcs {
    @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'supervisor\.py|main\.py' -and $_.CommandLine -notmatch 'web_app|--web' })
}

function Show-State([string]$headline, [string]$color, [string[]]$lines) {
    Write-Host ''
    Write-Host ('  ' + $headline) -ForegroundColor $color
    foreach ($l in $lines) { if ($l) { Write-Host ('  ' + $l) } }
    Write-Host ''
}

function Get-LastActivity {
    $hb = Join-Path $dir 'daemon.heartbeat'
    if (Test-Path $hb) { 'Last scraper activity: ' + (Get-Item $hb).LastWriteTime.ToString('dd MMM yyyy HH:mm') } else { '' }
}

function Wait-ForExit {
    if ($Pause) {
        Write-Host '  Press any key to close this window.'
        try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 10 }
    }
}

$code = 0
switch ($Mode) {
    'once' {
        if (@(Get-ScraperProcs).Count) { Show-State 'The continuous scraper is running. Stop it first (Stop Scraper icon).' Yellow @(); $code = 1; break }
        Write-Host 'munichScraper: single run started (takes about 20-40 minutes)...'
        & python main.py --run-once
        Write-Host 'Single run finished. New matches are in jobs.db; the apply pipeline picks them up on its next run.'
    }
    'start' {
        if (@(Get-ScraperProcs).Count) { Show-State 'SCRAPER IS ALREADY RUNNING' Green @('Nothing to do.', (Get-LastActivity)); break }
        Start-Process python -ArgumentList 'supervisor.py' -WorkingDirectory $dir -WindowStyle Hidden
        # check that it really came up (supervisor starts the daemon within a few seconds)
        $procs = @(); for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Seconds 1; $procs = @(Get-ScraperProcs); if ($procs.Count -ge 2) { break } }
        if ($procs.Count) {
            Show-State 'SCRAPER IS RUNNING' Green @('It works in the background, no window. A new cycle starts 10 minutes after the last one ends.', 'To stop it: double-click "Stop Scraper" on the Desktop.')
        } else {
            $tail = if (Test-Path (Join-Path $dir 'watchdog.log')) { (Get-Content (Join-Path $dir 'watchdog.log') -Tail 3) -join ' | ' } else { '' }
            Show-State 'THE SCRAPER DID NOT START' Red @('Last lines of watchdog.log:', $tail); $code = 1
        }
    }
    'stop' {
        $procs = @(Get-ScraperProcs)
        if (-not $procs.Count) { Show-State 'SCRAPER IS STOPPED' Yellow @('It was already stopped. Nothing is running.', (Get-LastActivity)); break }
        # supervisor first, together with its child processes, so it cannot restart the daemon
        foreach ($p in ($procs | Sort-Object { if ($_.CommandLine -match 'supervisor') { 0 } else { 1 } })) {
            if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { $null = & taskkill /PID $p.ProcessId /T /F 2>&1 }
        }
        # check that nothing is left; force-stop anything that survived
        $left = @(); for ($i = 0; $i -lt 15; $i++) { Start-Sleep -Seconds 1; $left = @(Get-ScraperProcs); if (-not $left.Count) { break } }
        if ($left.Count) { $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2; $left = @(Get-ScraperProcs) }
        Remove-Item (Join-Path $dir 'daemon.pid') -ErrorAction SilentlyContinue
        if ($left.Count) {
            Show-State 'WARNING: THE SCRAPER IS STILL RUNNING' Red @(($left | ForEach-Object { 'pid ' + $_.ProcessId }) -join ', '); $code = 1
        } else {
            Show-State 'SCRAPER IS STOPPED' Yellow @("Stopped $($procs.Count) scraper process(es) and checked: nothing is left running.", 'To start it again: double-click "Start Scraper" on the Desktop.')
        }
    }
    'status' {
        $procs = @(Get-ScraperProcs)
        $task = Get-ScheduledTask -TaskName 'MunichScraper' -ErrorAction SilentlyContinue
        $auto = if ($task) { 'WARNING: an autostart task "MunichScraper" exists (remove it with uninstall_autostart.ps1).' } else { 'Autostart: off (it never starts by itself).' }
        if ($procs.Count) {
            $since = ($procs | ForEach-Object { $_.CreationDate } | Sort-Object | Select-Object -First 1).ToString('dd MMM yyyy HH:mm')
            Show-State 'SCRAPER IS RUNNING' Green @("Running since $since.", (Get-LastActivity), $auto)
        } else {
            Show-State 'SCRAPER IS STOPPED' Yellow @('No scraper process is running.', (Get-LastActivity), $auto)
        }
    }
}
Wait-ForExit
exit $code
