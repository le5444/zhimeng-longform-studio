# Creation Suite Writer Instruction

This managed package supplies the canonical chapter-writing source from the bundled Creation Suite snapshot. `instruction-map.json` classifies every source line as included or excluded. Writing rules are materialized by the Runtime after the three-step Creation workflow; export, state-transition, versioning, and project-writeback instructions remain excluded.

This package has no entrypoint, execution contract, network access, secrets, or write permission. The Runtime owns materialization and dispatch evidence, never edits project truth directly, and never grants formal-draft authority. Writer output remains a candidate that requires author review.
