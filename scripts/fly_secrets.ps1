# Sends the hosted site's secrets to Fly from this PC. Run it yourself:
#     powershell -ExecutionPolicy Bypass -File scripts\fly_secrets.ps1
# Values go straight from .env and the house wallet file to Fly over stdin; they are never printed or written anywhere.
param([string]$App = "pokervis-live")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$fly = Join-Path $HOME ".fly\bin\flyctl.exe"

$env_ = @{}
foreach ($line in Get-Content (Join-Path $root ".env")) {
  if ($line -match '^\s*([A-Z_]+)=(.*)$') { $env_[$Matches[1]] = $Matches[2].Trim() }
}
foreach ($name in "SESSION_SECRET", "CHAIN_API_KEY") {
  if (-not $env_[$name]) { throw "$name is missing from .env" }
}
$key = (Get-Content (Join-Path $root "chain\service\.keys\server.json") -Raw) -replace '\s', ''

$lines = @("SESSION_SECRET=$($env_['SESSION_SECRET'])", "CHAIN_API_KEY=$($env_['CHAIN_API_KEY'])", "HOUSE_KEYPAIR=$key")
if ($env_['DATABASE_URL']) { $lines += "DATABASE_URL=$($env_['DATABASE_URL'])" }

$lines | & $fly secrets import --app $App --stage
if ($LASTEXITCODE -ne 0) { throw "fly secrets import failed" }
Write-Host "Secrets staged on $App. They take effect on the next deploy." -ForegroundColor Green
