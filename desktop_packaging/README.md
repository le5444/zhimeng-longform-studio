# Portable packaging (Local v1 package-test scaffold)

This directory freezes the portable data/app layout used by Local v1 package-tests
before a desktop shell/bundler ADR is chosen.

## Layout

```text
<root>/
  app/<version>/                 # immutable app bundle for one version
  data/
    runtime/writing-runtime.sqlite3
    diagnostics/
    skills/
    knowledge/
  logs/
  backups/
    runtime/runtime-backup-*/    # Runtime SQLite backup packages
    portable-backup-*/           # upgrade/user portable backups
  config.v1.json
  runtime.env                    # non-secret ZHIMENG_* path overrides
  install-receipt.json
```

## App bundle payload

`app/<version>/` is produced by `desktop_packaging.portable.bundle.assemble_portable_bundle`:

```text
app/<version>/
  VERSION
  MANIFEST.json                  # file hashes + not_claiming
  workbench/                     # Vite static dist
  runtime/src/zhimeng_runtime/   # Runtime package source
  runtime/pyproject.toml
  launch/start-runtime.ps1
  launch/open-workbench.ps1
  managed-skills/                 # optional seeded managed skill packages
```

Build from repo (requires existing `apps/workbench/dist`):

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python -m desktop_packaging.portable.build_bundle `
  --product-version 0.1.0-dev `
  --output-dir artifacts/portable-bundle-0.1.0-dev
```

## Covered package-test paths (current)

- clean install
- upgrade with pre-upgrade backup (includes data + runtime-backups)
- repair (rewrite roots/config, keep data)
- rollback after upgrade
- reinstall-restore from portable backup
- uninstall-preserve (default)
- uninstall-delete-confirmed
- Runtime SQLite backup/restore path wired to `backups/runtime`
- forced migration failure (portable simulation) + reinstall-restore
- real Workbench dist + Runtime package bundle assembly + hash verify
- managed-skills snapshot seed into data/skills on install (no overwrite)
- E2E: real bundle assemble/verify -> clean install -> Runtime `/health/ready` -> backup under `backups/runtime` -> mutate DB -> restore by manifest hash -> ready again
- E2E: real bundle upgrade + uninstall-preserve + reinstall-restore keeps Runtime DB marker and runtime backups, then Runtime becomes ready again
- E2E: portable diagnostics package export under data/diagnostics (redacted, no secrets/novel body)
- signed-package readiness gate: Authenticode/MSI signature presence + version/checksum checks; unsigned portable/EXE never claim `local_v1_exe`

## Signed package readiness gate

`desktop_packaging.portable.signed_package_gate.evaluate_signed_package_readiness`
is an authorization input for future release claims:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python -c @"
from pathlib import Path
from desktop_packaging.portable import evaluate_signed_package_readiness, write_signed_package_readiness_receipt
response = evaluate_signed_package_readiness(Path('artifacts/portable-bundle-0.1.0-dev'))
write_signed_package_readiness_receipt(response, Path('artifacts/signed-package-readiness.json'))
print(response['receipt']['signature_ready'], response['receipt']['local_v1_exe_claim_allowed'], response['not_claiming'][:3])
"@
```

Fail-closed rules:

- portable bundles are never `signature_ready`
- unsigned EXE/MSI are never `signature_ready`
- even a Valid Authenticode MSI still keeps `local_v1_exe_claim_allowed=false` until clean-VM matrix exists
- response always lists `local_v1_exe` under `not_claiming`

Not yet claimed:

- signed Windows EXE / MSI release artifacts
- full OS installer black-box matrix
- local_v1_exe delivery
- four_skills_generic_exactly_once

## Test

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python -m pytest desktop_packaging/tests -q
```

## Clean VM install matrix

Use structured evidence to productize the `clean_vm_install_matrix` gate without claiming `local_v1_exe`:

```python
from desktop_packaging.portable import (
    evaluate_clean_vm_install_matrix,
    evaluate_signed_package_readiness,
    write_clean_vm_install_matrix_receipt,
)

matrix = evaluate_clean_vm_install_matrix(Path('evidence/clean-vm-matrix.json'), product_version='0.2.0')
write_clean_vm_install_matrix_receipt(matrix, Path('artifacts/clean-vm-matrix-receipt.json'))
signed = evaluate_signed_package_readiness(
    Path('artifacts/Zhimeng-0.2.0.msi'),
    product_version='0.2.0',
    clean_vm_matrix_evidence=Path('evidence/clean-vm-matrix.json'),
)
assert signed['receipt']['local_v1_exe_claim_allowed'] is False
```

Required cells: win10/win11 × clean_install/upgrade/uninstall_preserve/reinstall_restore.
