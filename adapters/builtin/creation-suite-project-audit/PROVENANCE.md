# Creation Suite Project Audit Provenance

## Managed Copy

- Package ID: `novel-creation-suite.project-audit`
- Imported on: `2026-07-20`
- Source root: `%USERPROFILE%\.codex\skills\novel-creation-suite`
- Managed package root: `adapters/builtin/creation-suite-project-audit`
- Copy mode: ordinary files, byte-for-byte, with no links
- Imported source files: 15
- Imported source bytes: 271,932

The source tree is evidence only. Runtime execution resolves the entrypoint,
validators, and schemas inside this managed package and does not read or import
from the external source path.

The optional continuation and AI-smell execution paths are intentionally not
part of this minimal package. The runtime contract exposes only the default
read-only `project_audit.py --json` route and disables its extension flags.

## Source File Hashes

All hashes are SHA-256. Paths are relative to both the source root and the
managed package root.

| Relative path | Bytes | SHA-256 |
|---|---:|---|
| `SKILL.md` | 12097 | `3df879184f6adb8105e183c7f67777e748cc53a30e7dbd07a0fd784cc367ea26` |
| `references/bundled-skills/novel-suite-orchestrator/SKILL.md` | 32563 | `e27e8e436c8eba1039367e64979e0ed9a540a299bbb091c999d2851d3e3672eb` |
| `references/bundled-skills/novel-suite-orchestrator/scripts/project_audit.py` | 127697 | `3071fd1a2da322d82b2083568b2ce844ba12a1f455768ab42150dba4d5fbc238` |
| `references/bundled-skills/novel-suite-orchestrator/scripts/init_novel_project.py` | 13939 | `23eb57db081196ca6f40d8ea46d99aa058fb35327c9e26da08993fa11390b662` |
| `scripts/validate_packaging_triage.py` | 7343 | `bc0bd505fd8a3f9a5f06130cf44801e4bdf790f563387ea1ff27c61b6f376d5a` |
| `scripts/validate_unified_issue_card.py` | 9903 | `432a17925268f1bd6d431debfc7f8e7ec153981db99fc9f5a8b220ddc3d75bbe` |
| `scripts/validate_ai_smell_bridge.py` | 5360 | `69ae24820bc4ab504063dc84d907c8c8df6ce76ac8b1c40c6c6817a4107b77ec` |
| `scripts/validate_prompt_goal_budget.py` | 7839 | `e2194fe7c6de44dbd08fc0f910e39428cefa5dea224a755f1e1a73b9c898d138` |
| `scripts/validate_control_card_contract.py` | 11994 | `d00eb0d70e1863108e0e425837f7c38f015e5e15b27be6d93e09c107a5cb303b` |
| `scripts/validate_review_acceptance.py` | 16993 | `273a87d921ad42fe5bbd70cc0bc9b1ee238bbe07d802c5a6076de67c729be7ca` |
| `references/packaging-triage-schema.json` | 4280 | `06b063ddd09733fe90baaf266ffe8da279347e5e6ddc0d72036d735c51f9f595` |
| `references/unified-issue-card-schema.json` | 6923 | `9020daed91f7343c0b113fe8d4c41a436f807d55e90551a19ef0cb60465e0d14` |
| `references/ai-smell-bridge-schema.json` | 1096 | `5bff55d17e03c4ed6de32f05f90324343c4a7ba614f5cc27f6b5a40d90d8c169` |
| `references/prompt-goal-budget-schema.json` | 1785 | `0a716f3e67ca625bd460da7bb72a615e9a115cdabbf8f75876bc52523744a2b3` |
| `references/review-acceptance-schema.json` | 12120 | `cd1c3df1e890afe329b26767e86a1215c24e47a61a6ae534794376fabc80f412` |

## Managed Adapter And Fingerprint

The managed manifest sets `source.modified` to `true`. `run_project_audit.py`
is a thin, project-owned adapter: it validates the request envelope, invokes
the copied audit implementation, writes the declared artifact, and normalizes
the deterministic Runtime envelope. The copied audit logic itself is not
modified. The adapter and manifest are package files, not imported source
files.

The Runtime computes the package fingerprint at registration and verifies it
again immediately before and after every execution. It covers *every* regular
file in this package, including `PROVENANCE.md`, `skill.manifest.json`, and the
thin adapter, not only the 15 imported source files. Links, junctions,
reparse points, hardlinks, case-folded duplicate paths, and files over the
configured count/size/depth budgets are rejected.

The algorithm is deterministic and content-addressed:

1. Enumerate portable relative paths, sorted by `(path.casefold(), path)`.
2. For each file, hash its exact bytes with SHA-256 and append compact,
   sorted-key UTF-8 JSON `{"path": ..., "size": ..., "sha256": ...}` followed
   by a NUL byte to a SHA-256 hasher seeded with
   `zhimeng-managed-skill-package-v1\0`.
3. The resulting 64-character lowercase digest is the package fingerprint;
   the manifest fingerprint is the SHA-256 of the exact manifest bytes.

Because this document is itself included in the package hash, the final
package fingerprint is intentionally not hard-coded here. The active value is
the one recorded in the managed Skill Catalog and each execution Receipt.

At import, the non-self-referential adapter file hashes are:

| File | Bytes | SHA-256 |
|---|---:|---|
| `run_project_audit.py` | 2370 | `eb2b0409c344febeb2024fadf0da192d6f9a3ef633b08f38e6d9369fbcdac0a7` |
| `skill.manifest.json` | 1083 | `1f2ceea040454614807fd2ceb0e4a88dff6b4cf2cfc3a67f50e123bad6f91ef4` |
