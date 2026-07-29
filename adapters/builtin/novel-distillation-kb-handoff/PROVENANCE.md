# Provenance

O2-D managed snapshot derived from the real adjacent Skill contracts observed in:

- `%USERPROFILE%\.codex\skills\novel-distillation\SKILL.md`
  - standard transaction: source packet -> draft distillation cards -> reviewed formal cards -> add/replace -> validate -> health -> NLCL post-distill sync.
  - `distill_source_packet.py` produces auditable draft cards and tool result envelope.
  - formal cards must be reviewed before downstream writing use.

- `%USERPROFILE%\.codex\skills\novel-kb-manager\SKILL.md`
  - supported downstream commands include `sync-after-distill`, `import-distillation-index`, `assemble-context`, and `record-execution-trace`.
  - hard rule: `distillation_index` stores only active card entries, not full distillation card text.

This package is an owned Runtime snapshot and does not execute or permanently depend on the external user-level Skill directories.
