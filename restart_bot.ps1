$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "python"
$errLog = Join-Path $projectRoot "bot.err.log"
$outLog = Join-Path $projectRoot "bot.out.log"
$lockFile = Join-Path $projectRoot "bot.lock"

# 先按锁文件 PID 杀一次。
if (Test-Path $lockFile) {
    try {
        $lockedPid = [int](Get-Content $lockFile -Raw).Trim()
        if ($lockedPid -gt 0) {
            Stop-Process -Id $lockedPid -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }

# 再杀掉所有 bot.py 实例，避免 Telegram 轮询冲突。
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match '(?i)(^|["\s])bot\.py($|["\s])'
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2
Remove-Item $lockFile -Force -ErrorAction SilentlyContinue

Start-Process -FilePath $python `
    -ArgumentList "bot.py" `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden

Start-Sleep -Seconds 4
Get-Content $errLog -Tail 40
