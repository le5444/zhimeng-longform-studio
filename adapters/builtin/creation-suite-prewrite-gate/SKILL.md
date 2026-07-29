---
name: novel-creation-suite.prewrite-gate
description: Deterministically audits one Runtime-owned prewrite operation package after the managed Creation project audit. It emits blocked or risk_draft_only evidence and never authorizes formal prose in v1.
---

# Managed Creation Prewrite Gate

This package is an application-owned deterministic subset informed by the read-only
`novel-kb-manager` operation-package contract and the Creation Suite P0 prewrite gate.

It receives only:

- one immutable Runtime `zhimeng.prewrite-operation-package.v1`;
- hashes and domain status from the immediately preceding managed project audit.

It reads the isolated bound-project snapshot, writes only `prewrite-gate.json` to run staging,
uses no network or secrets, and never edits project truth. Version 1 cannot return formal prose
authorization; it records either a blocking repair route or a risk-draft-only boundary.
