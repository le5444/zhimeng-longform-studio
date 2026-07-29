# Novel Distillation Deep Instruction

This managed package freezes the reviewed deep-distillation clauses from the bundled `novel-distillation` source. It is an instruction source, not an executable Tool: it has no entrypoint, project read/write authority, network access, secret access, or artifact side effects.

`instruction-map.json` identifies the source ranges. `instruction-contract.json` is the Runtime boundary: one invocation may draft exactly one pending `work` candidate, one pending `voice` candidate, and one pending `validator` candidate. Every candidate must cite Runtime-supplied anchor evidence and remain non-formal until an author reviews it.

The package cannot run source scripts, update a library, synchronize NLCL, write project truth, call a Provider, or claim formal ingestion. Its bundled source is an ordinary immutable snapshot so managed execution never depends on `~/.codex/skills` remaining available.
