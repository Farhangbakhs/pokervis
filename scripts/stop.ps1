# Stops everything scripts\run.ps1 started (chain service, game server, tunnel, card reader, any bots).
$pat = 'bot_player|uvicorn server\.app|vision\.service|cam_link|src[\\/]server\.ts|AutoPoker.*tsx|tools\\bin\\cloudflared'
$n = 0
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $pat } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
Write-Host "Stopped $n AutoPoker processes."
