# Start the chain service + game server in their own windows and print the URLs.
#   scripts\dev.ps1            real devnet settlement
#   scripts\dev.ps1 -Mock      in-memory ledger (no Solana)
#   scripts\dev.ps1 -Bot       also start a bot opponent (first free seat)
param([switch]$Mock, [switch]$Bot)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path "$root\.env")) { & "$root\.venv\Scripts\python.exe" tools\init_env.py }

if (-not $Mock) {
  Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root\chain\service'; npm start" -WindowStyle Normal
  Start-Sleep 6
  $env:USE_MOCK_CHAIN = "0"
} else { $env:USE_MOCK_CHAIN = "1" }

$envVars = "`$env:USE_MOCK_CHAIN='$($env:USE_MOCK_CHAIN)'"
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root'; $envVars; .\.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port 8000" -WindowStyle Normal
Start-Sleep 5

if ($Bot) {
  Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root'; .\.venv\Scripts\python.exe -u tools\bot_player.py --name Botty" -WindowStyle Normal
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" -or $_.IPAddress -like "172.*" } | Select-Object -First 1).IPAddress
$pin = (Select-String -Path "$root\.env" -Pattern "^DEALER_PIN=(.*)$").Matches.Groups[1].Value
Write-Host ""
Write-Host "Dealer console (laptop):  http://localhost:8000/dealer      PIN: $pin"
Write-Host "Big screen:               http://localhost:8000/table"
Write-Host "Phones on the same Wi-Fi: http://${ip}:8000/play/<seat>?code=...   (scan the QRs on the dealer page)"
Write-Host "Dealer-cam phone (needs HTTPS): run  cloudflared tunnel --url http://localhost:8000   then open <tunnel-url>/cam"
Write-Host "Vision:  .\.venv\Scripts\python.exe -m vision.calibrate --source <cam> --mode click"
Write-Host "         .\.venv\Scripts\python.exe -m vision.service   --source <cam>"
