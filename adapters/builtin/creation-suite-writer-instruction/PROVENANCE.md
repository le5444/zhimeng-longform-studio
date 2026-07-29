# Creation Suite Writer Instruction Provenance

## Managed Source

- Package ID: `novel-creation-suite.writer-instruction`
- Imported on: `2026-07-23`
- Source root: `%USERPROFILE%\.codex\skills\novel-creation-suite`
- Managed package root: `adapters/builtin/creation-suite-writer-instruction`
- Copy mode: ordinary file, byte-for-byte, with no links

| Relative source path | Bytes | SHA-256 |
|---|---:|---|
| `references/bundled-skills/novel-volume-folder/SKILL.md` | 26836 | `5d0ece9fbdaec1b51465dcaac2752c8e1344104dbc513bf8ee3639b28c835a38` |

Runtime materialization reads only the immutable managed snapshot registered from this package. It does not read `~/.codex/skills`, follow links, access the network, or use secrets. `instruction-map.json` covers every source line and freezes the included/excluded range hashes before a Run may dispatch the Provider.

The selected source is the Creation Suite's canonical generic chapter writer. The package is an `instruction_source`, not an executable deterministic Tool. Runtime policy remains authoritative: `repair_only` can submit only a repair plan, `author_review_change_proposal` can only create a reviewable ChangeSet, and neither mode writes project truth directly.
