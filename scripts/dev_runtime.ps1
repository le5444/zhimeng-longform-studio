[CmdletBinding()]
param(
    [int]$Port = 8787,
    [string[]]$TrustedProjectRoots = @(),
    [string[]]$TrustedSkillRoots = @(),
    [string[]]$AllowedOrigins = @(
        'http://127.0.0.1:5173',
        'http://localhost:5173'
    )
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual environment is missing: $Python"
}

$env:ZHIMENG_ENVIRONMENT = 'development'
$env:ZHIMENG_HOST = '127.0.0.1'
$env:ZHIMENG_PORT = [string]$Port
$env:ZHIMENG_DATABASE_PATH = Join-Path $Root '.runtime\writing-runtime.sqlite3'
$env:ZHIMENG_ALLOWED_ORIGINS = $AllowedOrigins -join ','
$env:ZHIMENG_TRUSTED_PROJECT_ROOTS = $TrustedProjectRoots -join ';'
$env:ZHIMENG_TRUSTED_SKILL_ROOTS = $TrustedSkillRoots -join ';'
$env:PYTHONPATH = Join-Path $Root 'services\writing-runtime\src'

Set-Location -LiteralPath $Root
& $Python -m zhimeng_runtime
