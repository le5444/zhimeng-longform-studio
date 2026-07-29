[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual environment is missing: $Python"
}

Set-Location -LiteralPath $Root

& $Python 'scripts\verify_lifecycle_docs.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m pytest tests/test_lifecycle_docs.py -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python 'scripts\verify_phase1_baseline.py'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npm run typecheck
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npm run test:workbench
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npm run lint
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npm run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output 'PHASE1_VERIFY_OK'
