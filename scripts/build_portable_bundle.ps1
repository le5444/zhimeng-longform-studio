param(
  [Parameter(Mandatory = $true)][string]$ProductVersion,
  [string]$OutputDir = "",
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)
$ErrorActionPreference = "Stop"
if (-not $OutputDir) {
  $OutputDir = Join-Path $RepoRoot "artifacts/portable-bundle-$ProductVersion"
}
$env:PYTHONPATH = $RepoRoot
$python = Join-Path $RepoRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  $python = "python"
}
& $python -m desktop_packaging.portable.build_bundle `
  --repo-root $RepoRoot `
  --product-version $ProductVersion `
  --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
