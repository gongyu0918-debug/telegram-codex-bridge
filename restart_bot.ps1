$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "python"
$errLog = Join-Path $projectRoot "bot.err.log"
$outLog = Join-Path $projectRoot "bot.out.log"
$lockFile = Join-Path $projectRoot "bot.lock"
$bridgeMarker = "--bridge-root"
$bridgeMarkerValue = $projectRoot

# Stop the PID from bot.lock first when available.
if (Test-Path $lockFile) {
    $rawLockedPid = (Get-Content $lockFile -Raw -ErrorAction SilentlyContinue | Out-String).Trim()
    $lockedPid = 0
    $parsed = [int]::TryParse($rawLockedPid, [ref]$lockedPid)
    if ($parsed) {
        if ($lockedPid -gt 0) {
            Stop-Process -Id $lockedPid -Force -ErrorAction SilentlyContinue
        }
    }
}

# Then stop any remaining bot.py processes to avoid Telegram polling conflicts.
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -like "*bot.py*" -and
        $_.CommandLine -like "*$bridgeMarker*" -and
        $_.CommandLine -like "*$bridgeMarkerValue*"
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2
Remove-Item $lockFile -Force -ErrorAction SilentlyContinue

Start-Process `
    -FilePath $python `
    -ArgumentList @("bot.py", $bridgeMarker, $bridgeMarkerValue) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden

Start-Sleep -Seconds 4
Get-Content $errLog -Tail 40
