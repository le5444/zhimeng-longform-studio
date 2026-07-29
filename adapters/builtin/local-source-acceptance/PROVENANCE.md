# Local Source Acceptance Snapshot Provenance

This directory is a Runtime-managed snapshot. Runtime execution must use these
files and must not depend on `~/.codex/skills` at run time.

## Upstream sources

| Managed file | Read-only upstream source | Upstream SHA-256 |
| --- | --- | --- |
| `scripts/prepare_downloaded_sources.ps1` | `%USERPROFILE%\.codex\skills\tomato-novel-auto-distill\scripts\prepare_downloaded_sources.ps1` | `72787ac4cf74c2f505b246e975a870529d9345eb9276890f0c813d60e9a2adfc` |
| `scripts/extract_epub_text.py` | `%USERPROFILE%\.codex\skills\tomato-novel-auto-distill\scripts\extract_epub_text.py` | `136ee6e9575dbad7dcab7095cff322e212d2d72ef7ba2052c91005ca26cfb5f4` |
| `scripts/distill_source_packet.py` | `%USERPROFILE%\.codex\skills\novel-distillation\scripts\distill_source_packet.py` | `71068828f516d28eac081e1c64988e3541c394adcc0fbdf955f5a99e4ef9c132` |

The hashes above were measured when the snapshot was imported on 2026-07-22.

## Managed snapshot hashes

| Managed file | Managed SHA-256 |
| --- | --- |
| `scripts/prepare_downloaded_sources.ps1` | `bdf03a80843834407591ca398121fb882469a91362ca2c90fe76ad8d648c81cf` |
| `scripts/extract_epub_text.py` | `0c7224fa3087b3e9f25fba00b9341d74ae1dc8f398e0a29851db0b5604894f99` |
| `scripts/distill_source_packet.py` | `71068828f516d28eac081e1c64988e3541c394adcc0fbdf955f5a99e4ef9c132` |

## Managed adaptation

`prepare_downloaded_sources.ps1` uses ASCII `\uXXXX` regex escapes for Chinese
chapter markers. Windows PowerShell 5.1 decodes a UTF-8 script without a BOM
through the active system code page; keeping the executable pattern ASCII
prevents silent chapter-count corruption. The change does not alter source
bytes, packet bytes, or the supported `.txt`/`.epub` formats.

`extract_epub_text.py` is Runtime-hardened after import. It rejects encrypted,
unsafe-path, oversized, over-compressed, structurally incomplete, empty, and
mojibake-suspected EPUBs before candidate evidence can reach distillation.

The Runtime records the managed-file hashes at execution time. A hash mismatch
must stop acceptance instead of falling back to the external Skill copy.

## Versioned acceptance fixture

`services/writing-runtime/tests/fixtures/local-source/archive-night-sample.manifest.json`
pins the source bytes, managed snapshot hashes, adjacent prepare/distillation/
writer contracts, required artifacts, expected card types, and claim boundary.
The adapter, source-import, and writer E2E tests load that manifest and compare it
to live outputs from these managed scripts.

This evidence verifies the managed local TXT vertical slice. It does not prove a
remote Tomato downloader plus `book_id` flow, complete Runtime-owned project
`.kb` state, or a generic Creation Skill workflow. Those claims remain
`not_proven` in the versioned fixture.
