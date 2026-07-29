# Creation Suite Handoff

This builtin managed adapter materializes the first Creation-consumable handoff artifact after the Runtime-owned prewrite gate.

It reads the immutable request from stdin and writes `creation-handoff.json` to Runtime staging. It does not call a model, does not open network or secrets, and never writes project truth. In M3j it still returns `formal_draft_allowed=false`; the artifact is for repair planning or risk-draft analysis only, not formal prose authorization.
