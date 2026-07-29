# Provenance

This managed adapter is a clean in-repository rewrite of the narrow body
cleanliness rules from the read-only `novel-creation-suite` validator
`validate_draft_body_cleanliness.py`. It owns its implementation and contracts;
the external Skill directory is not a runtime dependency.

The adapter also executes Runtime-owned bounded term rules from formal native
validator cards. Those rules and their source/version provenance are supplied
in a hash-bound Context/NLCL bundle; arbitrary regular expressions and model
judgment are not accepted. This extension is original product code.

The adapter deliberately excludes prose-quality scoring, AI-smell evaluation,
reader simulation, model calls, project writes, and formal draft authorization.
Candidate bytes are supplied by the caller and the only output written by the
process is the declared staging artifact.
