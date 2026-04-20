$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "python"
$errLog = Join-Path $projectRoot "bot.err.log"
$outLog = Join-Path $projectRoot "bot.out.log"
$lockFile = Join-Path $projectRoot "bot.lock"

# 先杀掉所有当前目录下的 bot.py 实例，避免 Telegram 轮询冲突。
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match '(?i)telegram_codex_bridge\\bot\.py|(^|["\s])bot\.py($|["\s])'
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
