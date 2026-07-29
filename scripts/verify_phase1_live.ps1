[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

Set-Location -LiteralPath $Root

python 'scripts\smoke_phase1.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npm run test:e2e
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output 'PHASE1_LIVE_VERIFY_OK'
