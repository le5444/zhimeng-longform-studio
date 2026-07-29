[CmdletBinding()]
param(
    [string]$ProjectRoot = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$env:VITE_PROJECT_ROOT = $ProjectRoot
Set-Location -LiteralPath $Root
npm run dev:workbench
