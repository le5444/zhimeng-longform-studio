---
name: novel-creation-suite.draft-gate
description: Deterministically checks accepted candidate prose bytes for delivery contamination and executes bounded formal validator-card term rules. It writes only a staging artifact and never claims prose quality or formal authorization.
---

# Managed Creation Post-Draft Gate

This package is a narrow, Runtime-owned boundary after a candidate draft has
been materialized. The request supplies candidate file bytes and may bind them
to a file manifest with expected SHA-256 and byte length. The adapter checks
only transport/delivery hazards: empty or invalid UTF-8 bodies, JSON/code
fences, workflow metadata, role labels, model-error text, and explanatory
delivery commentary.

The result is `passed` or `blocked`, includes per-file hashes and locations,
and always sets `quality_assessed=false` and `formal_draft_allowed=false`.
When Runtime supplies a validator bundle bound to the reviewed Context Pack and
NLCL route, the adapter executes only the bounded `forbid_terms` and
`require_any_term` rules. It records each card/version/rules hash and per-rule
result; missing or invalid executable rules block rather than becoming advice.
It does not score style, continuity, originality, AI smell, reader response, or
whether an author should accept a ChangeSet. It has no network or secret
permissions and writes only `draft-gate.json` to Runtime staging.
