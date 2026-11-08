# AutoPoker - clean start. NO bots. Run it from anywhere:
#     powershell -ExecutionPolicy Bypass -File scripts\run.ps1
# or double-click run.cmd in the project root.
#
# What it does:
#   1. stops anything left over from an earlier run (chain service, game server, tunnel, card reader, bots)
#   2. starts the Solana chain service (devnet), the game server, a fresh HTTPS tunnel and the card reader
#   3. waits until the public link really answers (retries the tunnel if Cloudflare is slow)
#   4. makes the FRESH camera QR (data\cam_qr.png) and prints every link you need (also saved in data\links.txt)
#
# Options:  -KeepTable   keep the same seat QR codes / roster as last run (default: empty table, new seat codes)
#           -NoVision    do not start the card reader
param([switch]$KeepTable, [switch]$NoVision)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force $data | Out-Null

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ---------- checks ----------
if (-not (Test-Path $py)) { Die "Python venv missing at $py. Create it: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt" }
if (-not (Test-Path (Join-Path $root ".env"))) { & $py tools\init_env.py }
if (-not (Test-Path (Join-Path $root "chain\service\node_modules"))) { Say "Installing chain service packages (once)..."; Push-Location chain\service; npm install; Pop-Location }
if (-not (Test-Path (Join-Path $root "chain\service\.keys\server.json"))) { Die "No server wallet. Run once: cd chain\service ; npm run setup  (then fund the printed address with devnet SOL)" }
if (-not (Test-Path (Join-Path $root "chain\service\.state\state.json"))) { Die "No CHIP mint yet. Run once: cd chain\service ; npm run setup" }

function EnvValue($name) {
  $m = Select-String -Path (Join-Path $root ".env") -Pattern "^$name=(.*)$" | Select-Object -First 1
  if ($m) { return $m.Matches.Groups[1].Value.Trim() } else { return "" }
}
$pin = EnvValue "DEALER_PIN"

function Stop-Stack {
  $pat = 'bot_player|uvicorn server\.app|vision\.service|cam_link|src[\\/]server\.ts|AutoPoker.*tsx|tools\\bin\\cloudflared'
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $pat } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Wait-Url($url, $seconds, $match) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) {
    try { $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 8; if ($r.StatusCode -eq 200 -and (-not $match -or $r.Content -match $match)) { return $true } } catch { }
    Start-Sleep -Seconds 2
  }
  return $false
}

function Wait-Tunnel($url, $seconds) {
  # Prove a brand-new tunnel works WITHOUT asking this network's DNS. A lookup made before the name has spread gets
  # "does not exist", and the network's resolvers (and the phones using them) then repeat that answer for 60 s.
  # So: get the address from 1.1.1.1 and connect to it directly; normal DNS is only used once the name is live.
  $h = ([uri]$url).Host
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) {
    $ip = $null
    try { $ip = (Resolve-DnsName $h -Server 1.1.1.1 -Type A -DnsOnly -QuickTimeout -ErrorAction Stop | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress } catch { }
    if ($ip) {
      $body = & curl.exe -s --max-time 8 --resolve "${h}:443:$ip" "$url/ui/"
      if ($LASTEXITCODE -eq 0 -and ($body -join "`n") -match "AutoPoker") { return $true }
    }
    Start-Sleep -Seconds 1
  }
  return $false
}

# ---------- 1. clean slate ----------
Say "Stopping any earlier run..."
Stop-Stack
Start-Sleep -Seconds 3
if (-not $KeepTable) { Remove-Item (Join-Path $data "table_state.json") -ErrorAction SilentlyContinue }
Remove-Item (Join-Path $data "public_url.txt") -ErrorAction SilentlyContinue

# ---------- 2. chain service (Solana devnet) ----------
Say "Starting the Solana chain service (devnet)..."
Start-Process -WindowStyle Hidden -WorkingDirectory (Join-Path $root "chain\service") -FilePath cmd.exe -ArgumentList '/c', 'npm start > ..\..\data\chain.log 2>&1'
if (-not (Wait-Url "http://127.0.0.1:8787/health" 60 "ok")) { Die "Chain service did not start. See data\chain.log" }

# ---------- 3. game server ----------
Say "Starting the game server..."
Start-Process -WindowStyle Hidden -WorkingDirectory $root -FilePath cmd.exe -ArgumentList '/c', '.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port 8000 > data\server.log 2>&1'
if (-not (Wait-Url "http://127.0.0.1:8000/api/state" 60 "phase")) { Die "Game server did not start. See data\server.log" }

# ---------- 4. public HTTPS link + fresh camera QR (retry the tunnel if Cloudflare is slow) ----------
$base = $null
for ($try = 1; $try -le 3 -and -not $base; $try++) {
  Say "Opening the public tunnel (attempt $try of 3)..."
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'cam_link|tools\\bin\\cloudflared' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Remove-Item (Join-Path $data "public_url.txt") -ErrorAction SilentlyContinue
  Start-Process -WindowStyle Hidden -WorkingDirectory $root -FilePath cmd.exe -ArgumentList '/c', 'set PYTHONIOENCODING=utf-8&& .venv\Scripts\python.exe -u tools\cam_link.py > data\tunnel.log 2>&1'
  $urlFile = Join-Path $data "public_url.txt"
  for ($i = 0; $i -lt 40 -and -not (Test-Path $urlFile); $i++) { Start-Sleep -Seconds 1 }
  if (-not (Test-Path $urlFile)) { Warn "No tunnel address yet."; continue }
  $candidate = (Get-Content $urlFile).Trim()
  if (Wait-Tunnel $candidate 45) { $base = $candidate }
  elseif (Wait-Url "$candidate/ui/" 90 "AutoPoker") { $base = $candidate }          # 1.1.1.1 blocked on this network: use normal DNS
  else { Warn "Tunnel $candidate is not answering yet." }
}
if (-not $base) { Die "Could not open a public tunnel (Cloudflare). Check your internet and re-run. See data\tunnel.log" }

# ---------- 5. card reader ----------
if (-not $NoVision) {
  Say "Starting the card reader..."
  Start-Process -WindowStyle Hidden -WorkingDirectory $root -FilePath cmd.exe -ArgumentList '/c', '.venv\Scripts\python.exe -u -m vision.service --source web:http://127.0.0.1:8000 > data\vision.log 2>&1'
}

# ---------- 6. links ----------
$tok = (Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/dealer/login" -ContentType "application/json" -Body (@{ pin = $pin } | ConvertTo-Json)).token
$st = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/dealer/state" -Headers @{ Authorization = "Bearer $tok" }
$sol = $st.sol
$lines = @()
$lines += "AUTOPOKER - links for this run  ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))"
$lines += ""
$lines += "PLAYERS (one link per seat; open in a browser with Phantom / Solflare / Backpack set to DEVNET):"
foreach ($n in 1..4) { $lines += "  Seat ${n}: $base/play/${n}?code=$($st.seat_codes.PSObject.Properties[[string]$n].Value)" }
$lines += ""
$lines += "CAMERA PHONE : $base/cam      (QR image: data\cam_qr.png - enter the PIN, tap Start camera)"
$lines += "DEALER       : $base/dealer   (PIN: $pin)"
$lines += "ON THIS PC   : http://localhost:8000/dealer"
if ($sol) {
  $lines += ""
  $lines += "Chip price   : 1 CHIP = $($sol.lamports_per_chip) lamports  (1000 CHIP = $([math]::Round(1000 * $sol.lamports_per_chip / 1e9, 6)) SOL)"
  $lines += "House wallet : $($sol.house)"
  $lines += "CHIP mint    : $($sol.mint)"
}
$lines | Set-Content -Path (Join-Path $data "links.txt") -Encoding ASCII

Write-Host ""
$lines | ForEach-Object { Write-Host $_ -ForegroundColor Green }

try {
  $bal = (Invoke-RestMethod -Method Post -Uri "https://api.devnet.solana.com" -ContentType "application/json" -Body (@{ jsonrpc = "2.0"; id = 1; method = "getBalance"; params = @($sol.house) } | ConvertTo-Json -Compress)).result.value
  Write-Host ("House wallet balance: {0} devnet SOL" -f ($bal / 1e9)) -ForegroundColor Green
  if ($bal -lt 20000000) { Warn "House wallet is low. Cash-outs need SOL in it: send devnet SOL to $($sol.house) (faucet.solana.com)." }
} catch { }

Write-Host ""
Write-Host "Running with NO bots. Players need a Solana wallet with devnet SOL (the join screen shows their real devnet balance)." -ForegroundColor Cyan
Write-Host "Links are also saved in data\links.txt. To stop everything: scripts\stop.ps1" -ForegroundColor Cyan
Start-Process "http://localhost:8000/dealer"
