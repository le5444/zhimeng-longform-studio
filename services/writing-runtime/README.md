# Zhimeng Writing Runtime

FastAPI sidecar for the Zhimeng author workbench. The current bootstrap surface
owns local runtime health, event streaming, trusted project inspection,
binding revisions, durable execution metadata, and bounded read-only Skill
discovery. It also owns fixed Tomato and managed local TXT/EPUB source-import job
surfaces that stop at reviewable draft cards and a fixed read-only distillation Health/Coverage
preview. Novel project files remain the source of truth and are not stored in SQLite.
It now also owns the M1 Context Pack compiler, persistence and review-to-provider
handoff. Runtime, Workbench and browser validation is recorded in
`../../evidence/context-pack-validation-20260720.md`.

## Runtime contract

`web_fetch` reads public HTTP/HTTPS pages and uses a fresh, headless browser
context when a small HTML shell needs JavaScript to load its content. It tries
installed Edge, Chrome, then Playwright Chromium. Install the runtime dependencies
first; machines without Edge or Chrome also need `python -m playwright install chromium`.
The browser does not use the author's browser profile or login cookies. HTTP errors,
verification pages, empty content and failed rendering retain their response evidence
and are recorded as failed reads. Automatic rendering is a heuristic; it does not
guarantee that every site's content has loaded. Long rendered text uses the same saved
full-text and head/tail preview as other pages.

- Python 3.12 and Pydantic v2
- Loopback-only HTTP server
- Exact-origin CORS, configured by environment variable
- SQLite WAL database with schema versioning
- JSON error envelope carrying a request ID
- Server-Sent Events for runtime readiness plus durable Agent Activity replay

Endpoints:

```text
GET /health/live
GET /health/ready
GET /api/v1/runtime/status
GET /api/v1/events/stream
GET /api/v1/projects
POST /api/v1/projects/inspect
GET /api/v1/projects/{project_id}/inspect
POST /api/v1/projects/files/read
GET /api/v1/projects/{project_id}/files/{file_id}
POST /api/v1/projects/{project_id}/change-sets
GET /api/v1/projects/{project_id}/change-sets
GET /api/v1/change-sets/{change_set_id}
PATCH /api/v1/change-sets/{change_set_id}/decisions
POST /api/v1/change-sets/{change_set_id}/post-draft-gate
GET /api/v1/change-sets/{change_set_id}/post-draft-gate
POST /api/v1/change-sets/{change_set_id}/apply
POST /api/v1/change-sets/{change_set_id}/rollback
POST /api/v1/source-imports/tomato/search
POST /api/v1/source-imports/local/inspect
POST /api/v1/source-imports
GET /api/v1/source-imports
GET /api/v1/source-imports/{import_id}
GET /api/v1/source-imports/{import_id}/execution-receipt
GET /api/v1/source-imports/{import_id}/attempt-receipts
POST /api/v1/source-imports/{import_id}/cancel
POST /api/v1/source-imports/{import_id}/retry
POST /api/v1/knowledge/source-imports/{import_id}/candidates
GET /api/v1/knowledge/source-imports/{import_id}/formalization
POST /api/v1/knowledge/source-imports/{import_id}/finalize
GET /api/v1/knowledge/candidates
PUT /api/v1/knowledge/candidates/{candidate_id}
POST /api/v1/knowledge/candidates/{candidate_id}/promote
POST /api/v1/knowledge/candidates/{candidate_id}/reject
GET /api/v1/knowledge/cards
GET /api/v1/projects/{project_id}/knowledge/kb-state
POST /api/v1/projects/{project_id}/knowledge/kb-state/rebuild
GET /api/v1/distillation/health
POST /api/v1/distillation/coverage
POST /api/v1/agent/sessions/{session_id}/context-packs
GET /api/v1/agent/sessions/{session_id}/context-packs
GET /api/v1/agent/context-packs/{context_pack_id}
GET /api/v1/agent/context-packs/{context_pack_id}/nlcl-route
GET /api/v1/agent/context-packs/{context_pack_id}/prewrite-operation-package
GET /api/v1/agent/context-packs/{context_pack_id}/nlcl-route/executions/{call_ordinal}
GET /api/v1/agent/runs/{run_id}/context-pack
GET /api/v1/skills
GET /api/v1/projects/{project_id}/skills
GET /api/v1/agent/status
POST /api/v1/projects/{project_id}/agent/sessions
GET /api/v1/projects/{project_id}/agent/sessions
GET /api/v1/agent/sessions/{session_id}
GET /api/v1/agent/sessions/{session_id}/messages
GET /api/v1/agent/sessions/{session_id}/runs
POST /api/v1/agent/sessions/{session_id}/runs
GET /api/v1/agent/runs/{run_id}
GET /api/v1/agent/runs/{run_id}/activities
GET /api/v1/agent/runs/{run_id}/events
GET /api/v1/agent/runs/{run_id}/workflow
POST /api/v1/agent/runs/{run_id}/resume
GET /api/v1/agent/runs/{run_id}/candidate-output
GET /api/v1/agent/runs/{run_id}/provider-turn-receipts
GET /api/v1/agent/runs/{run_id}/provider-recovery
POST /api/v1/agent/runs/{run_id}/provider-recovery/decision
GET /api/v1/agent/runs/{run_id}/writer-task
GET /api/v1/agent/runs/{run_id}/writer-candidate-result
GET /api/v1/agent/runs/{run_id}/writer-trace
POST /api/v1/agent/runs/{run_id}/cancel
GET /docs
GET /openapi.json
```

Project inspect and file endpoints are read-only. Bound file GETs emit an
`ETag` and honor `If-None-Match`. Runtime-owned ChangeSets are the only current
project write surface: proposals are pinned to a project revision/fingerprint,
reviewed per hunk, serialized by a process-local project lock, applied through
temporary sibling files with compensation, and can be rolled back from verified
backups. This is a recoverable multi-file transaction, not a claim that the
filesystem provides one atomic commit across every target. A writeback journal restores interrupted apply attempts
to the proposal base and interrupted rollbacks to the applied state during the
next Runtime startup. Unknown disk content is never overwritten by recovery.
Agent/Skill-origin accepted prose create/update changes additionally require a
current passing post-draft Gate receipt before any Apply artifact, backup,
temporary file, journal, or project-state mutation is created. The Gate checks
delivery contamination only; it never assesses prose quality or authorizes a
formal draft.

Public ChangeSet creation accepts only `origin=manual`. Agent-origin candidates
can only be created by the Runtime-owned `change_set_propose` tool. SQLite v7
persists a one-to-one provenance record binding the ChangeSet to its Run,
ToolCall, reviewed Context Pack, project revision/fingerprint, and canonical
request hash. Skill-workflow provenance is not stored in this table and must not
be inferred from it; post-draft Gate attempts have their own immutable
receipt and do not retroactively claim which workflow produced a ChangeSet.

The Runtime owns an OpenAI-compatible Agent lane with text streaming and bounded
function Tool Calls. Sessions, user
and assistant messages, Runs, ActivityItems, terminal errors, usage, and replay
cursors are stored in SQLite. Provider deltas commit before the broker wakes
connected clients; `Last-Event-ID` reconnects query the SQLite outbox rather
than an in-memory queue. Streamed ToolCall fragments are strictly reassembled,
validated, bounded, persisted before execution, and paired with a durable
ToolResult before being returned to the Provider. The M2 catalog always retains
`project_list_files`, `project_read_file`, and `change_set_propose`. When the
project-owned managed Creation packages register successfully, M3 adds fixed
`skill_execute` to the Runtime-owned internal catalog and advertises the
`skills` capability. That tool can only run the allowlisted
`novel-creation-suite.project-audit`, `novel-creation-suite.prewrite-gate`, and
`novel-creation-suite.creation-handoff` checks against Runtime-owned inputs and
an isolated Run-bound snapshot;
there is no Shell, network, secrets, MCP, Plugin, Subagent, external knowledge
connector, or direct Apply capability. Empty output, incomplete streams, token
truncation, malformed calls, unknown tools, and unfinished calls cannot become
successful Runs. This is not the final Agent Kernel or complete four-Skills
chain. See `../../docs/M2_TOOL_CAPABLE_AGENT_IMPLEMENTATION.md` and
`../../docs/M3A_MANAGED_CREATION_SKILL_IMPLEMENTATION.md`.

M3i compiles the fixed `project-audit -> prewrite-gate` sequence into an
immutable Workflow Plan, per-step CAS Checkpoints, and one Provider I/O
Boundary. Startup only reconciles terminal SQLite evidence: a pre-boundary Run
is paused for explicit author Resume, while a Run with a boundary is marked
interrupted and never auto-replayed. The boundary binds the exact messages,
Provider-visible tools, model, dispatch-config fingerprint, executor, and final
checkpoint before `stream_chat`. After the two managed steps, `skill_execute`
is removed from the Provider-visible catalog; an unadvertised call fails before
a ToolCall is created. Plan/checkpoint/boundary progress is committed with
Activity/outbox evidence. See
`../../docs/M3I_DURABLE_WORKFLOW_IMPLEMENTATION.md`.

M3j extends that historical two-step primitive into the current canonical
`project-audit -> prewrite-gate -> creation-handoff` chain. The third managed
step binds the Plan, Operation Package, both earlier execution/Skill/ToolResult
receipts, and predecessor Checkpoints. It emits only a repair-plan/risk-draft
handoff with `formal_draft_allowed=false`; it is not a Provider candidate. The
M3j validation remains historically anchored to SQLite schema v16 and 28 tables.

M3k exposes Runtime-owned `candidate_output_submit` only after the final handoff
and Provider I/O Boundary. It must be the only and final ToolCall and cannot be
mixed with Provider text. The Provider submits only `kind` and `content`; Runtime
identities and references are never Provider-owned. Before network dispatch, the
Runtime validates the active managed Writer-instruction snapshot, persists its
activation binding, derives one immutable WriterTask, materializes the bounded
instruction, and atomically commits a
`zhimeng.creation-instruction-dispatch-receipt.v1` with the Provider Boundary.
The Runtime then creates one immutable `zhimeng.agent-candidate-output.v3`, binding
the Plan, final Checkpoint, Provider Boundary/request, Context, Operation Package,
project revision/fingerprint, WriterTask, instruction materialization/dispatch,
content/reference hashes, the ordered Provider-turn receipt chain, and the one
same-Run ChangeSet required by author-review mode. Every response receipt records
requested/reported model identity plus the explicit accepted-alias snapshot before
its ToolCalls execute; a rejected mismatch cannot become a successful candidate.

`repair_only` accepts only `repair_plan` without a ChangeSet.
`author_review_change_proposal` accepts only `change_proposal` with exactly one
same-Run, exact-target ChangeSet. Runtime first commits the succeeded submission
ToolCall and Candidate as a recoverable cutpoint. A second transaction validates
that cutpoint and commits WriterCandidateResult, assistant completion, and Run
success; failure of the second transaction leaves only the reviewable Candidate
for an explicit recovery decision. Historical v1/v2 candidates
remain read-only: v1 has no verified response identity, while v2 lacks the current
Writer/instruction-dispatch binding. Neither can complete a new Run or be adopted
by Provider recovery as a current successful terminal state.

After a Provider Boundary, failed or interrupted Runs receive one append-only
`zhimeng.provider-recovery-dossier.v1` in `run_event`. Its remote result is
always `unknown` and its Provider replay count is always zero. The dossier and
decision bind the same committed instruction-dispatch receipt. An author submits
dossier revision/hash CAS; Runtime revalidates the dispatch identity/hash and
persists it in the immutable decision before adopting a valid v3 candidate or
abandoning the result. Adoption creates or validates
the same-Run WriterCandidateResult in the decision transaction. At most one
decision is appended, and neither action replays the Provider request. The
aggregate `GET /api/v1/agent/runs/{run_id}/writer-trace` returns Workflow, task,
activation, materialization, dispatch, candidate, Writer result, and recovery
evidence as one fail-closed Run-scoped view.

`adopt_persisted_candidate` additionally revalidates the Run-bound current
project `.kb` state ID and manifest hash. `abandon_and_recompile` deliberately
remains available when that old binding is missing, stale, or tampered, so the
author can discard the uncertain result and compile a new Pack without replaying
the old Provider request.

The M1 path compiles one immutable Context Pack from a locked Project Reader
snapshot, completed Session history, and eligible summaries from the fixed
distillation coverage adapter. A Pack records included/excluded sources, Gates,
character/token budgets, exact Provider messages, and pack/injection/message
hashes. Risk or unavailable material is excluded without storing its content.
SQLite schema v20 persists the Pack and allows at most one consuming Run. It
still has 33 tables; v20 only adds nullable `project_kb_state_id` and
`project_kb_manifest_hash` columns to `context_pack`, `nlcl_route_receipt`, and
`nlcl_route_execution_receipt`. A new create request requires that pair, and the
Runtime copies it through Pack/Route/Execution evidence. Context compilation and
idempotent replay, Agent create/resume/context injection, the post-Workflow and
pre-Boundary cuts, every Provider turn, and recovery adoption all revalidate the
current on-disk `.kb`. Run binding and first Provider I/O fail closed on hash,
scope, project/history, readiness, knowledge-fingerprint, project-KB binding, or
receipt drift. The Provider receives only
the persisted exact messages, and binding/injection receipts are emitted as
Artifact Activities. M1 compilation itself still does not promote knowledge;
M2 consumes only the reviewed Pack and project-bound tools described above.

Rows migrated from v19 retain NULL project-KB binding columns. They remain
readable with their historical hashes but cannot create, resume, inject, dispatch,
or recovery-adopt a Run; migration never fabricates a current identity from disk.
The Workbench displays both fields across Pack/Route/Execution evidence and keeps
the start-writing CTA and Run confirmation disabled until the current pointer,
manifest, and complete in-chain pair agree.

The global Skill Registry endpoint remains read-only: it discovers `SKILL.md`
files but never executes or imports code from those external roots. The separate
managed lifecycle imports adapted packages into content-addressed Runtime-owned
snapshots and records immutable application/project import receipts. External
imports are disabled without explicit trusted roots and remain inert until an
author CAS activation. Raw `SKILL.md` sources return
`managed_skill_adaptation_required`.

Application activation is the base layer; project activation may inherit, select
another imported version, or explicitly disable the Skill. Project disable blocks
Agent and persisted Workflow use. Builtin project-audit, prewrite-gate,
creation-handoff, and draft-gate packages bootstrap only when no author state
exists, so restart does not overwrite disable or upgrade/rollback decisions.
Registration failure degrades only that capability and does not make
`/health/ready` fail. The Runtime-owned `skill_execute` path accepts the active
three pre-Provider packages, but M3j does not expose it again to the Provider after
the planned steps. ChangeSetService invokes the active draft-gate over
Runtime-materialized candidate bytes through the same managed executor boundary.
The separate Tomato adapter is a fixed allowlisted workflow, not a general
execution mode for discovered Skills. A Runtime-owned local TXT/EPUB adapter additionally
enforces trusted roots and stable path/size/hash evidence, copies a Source Packet, emits
draft cards, and records append-only attempt/cleanup evidence. EPUB input is rejected on
unsafe/encrypted ZIP entries, resource-budget overflow, invalid mimetype/container/OPF/spine,
empty text, or suspected mojibake; its receipt binds the preserved EPUB snapshot and extracted
text with independent sizes and hashes. Both adapters stop at `awaiting_review`; the Tomato
external-download path still lacks a current live acceptance. The local EPUB API acceptance is
recorded in `../../evidence/local-epub-live-acceptance-20260723.json`. The prepare PowerShell
script currently launches the extractor through the `python` command on `PATH`; managed script
hashes and output contracts are checked, but a packaged interpreter remains an M6 requirement.
See `../../docs/TOMATO_SOURCE_IMPORT_IMPLEMENTATION.md`. SQLite schema v20 atomically publishes
execution receipts containing run/revision/idempotency, request, tool and artifact identities.
The native Knowledge API imports immutable
candidate origins and now applies typed review/promotion contracts to `work`, `voice`,
and `validator`; it stages version lineage and atomically finalizes the complete batch
before moving the source job to `completed`. An explicit FormalKnowledgeSelector can
then bind that same active verified batch into Context, NLCL and the Prewrite Operation
Package. Application foundation and project overlay ownership remain isolated in the
same store. See
`../../docs/M3F_VERIFIED_SOURCE_LINEAGE_IMPLEMENTATION.md` and
`../../docs/M3G_PROJECT_KNOWLEDGE_SCOPE_IMPLEMENTATION.md`.

The separate distillation preview adapter invokes only allowlisted `health` and
`coverage` commands against one operator-configured library. The API accepts
only `query/profile`; it never accepts a library path, command, cwd, output path,
or top value. Responses expose compact card fields and bind the result to
library/tool fingerprints. `degraded/risk_ready` enables only a risk preview,
not formal Context injection. The separate Context Service may consume only
cards for which the adapter reports both `formal_injection_allowed` and
`hard_rule_eligible`; unavailable or changed knowledge fingerprints fail closed
or remain an explicit excluded warning according to the Pack stage. See
`../../docs/DISTILLATION_COVERAGE_PREVIEW_IMPLEMENTATION.md`.

The product does not install, start, embed, call, or connect to MyTemple or any
other external knowledge application, and it does not use their installation
directories, APIs, or private data directories as runtime sources. External
material is eligible only after an explicit author import copies it into
project-controlled storage with provenance, version, and content hashes.

## Trusted project roots

Project access is disabled by default. `ZHIMENG_TRUSTED_PROJECT_ROOTS` accepts
either a JSON array or semicolon-separated absolute paths. Inspection is
limited to those explicit roots; the Runtime does not scan Desktop, Documents,
or the whole machine implicitly.

```powershell
$env:ZHIMENG_TRUSTED_PROJECT_ROOTS='["C:\\path\\to\\novel-projects"]'
```

The reader rejects path traversal and symlink/junction/reparse escapes, applies
bounded scan limits, and returns a retryable `project_snapshot_changed` error
when the project changes during a scan. A trusted root should normally be the
parent directory that contains one or more novel projects.

## Trusted skill roots

Skill discovery is disabled by default. `ZHIMENG_TRUSTED_SKILL_ROOTS` accepts a
JSON array or semicolon-separated absolute paths. Root order is retained as
explicit source priority. Overlapping roots are rejected, and same-name Skills
from different roots remain separate records with duplicate-name diagnostics;
the Registry never silently picks a backup over the current source.

```powershell
$env:ZHIMENG_TRUSTED_SKILL_ROOTS='["C:\\path\\to\\skill-package"]'
```

Discovery supports nested layouts such as
`references/bundled-skills/<name>/SKILL.md`. It rejects symlink, junction,
reparse, traversal, and hardlinked-manifest reads. Directory entries,
directories, depth, manifest count, per-manifest bytes, and aggregate manifest
bytes all have explicit limits. `library`, `data`, `output`, `cache`, and other
content/build trees are pruned before descent. Only bounded `SKILL.md` bytes
are parsed with PyYAML `SafeLoader`; package content is not hashed.

`manifest_fingerprint` and `manifest_revision` therefore identify the exact
instruction file only. `package_fingerprint_status=not_scanned` is deliberate:
it prevents a manifest-only scan from being misreported as package validation
or execution readiness.

The bounded read covers and hashes the complete `SKILL.md`; only its YAML
frontmatter is interpreted as metadata. `content_files_read=0` means the scan
did not read package code, corpora, outputs, or other content files. It does not
mean that the instruction body inside `SKILL.md` was skipped. Implementation
details and the four-root receipt are recorded in
`../../docs/PHASE3_SKILL_REGISTRY_IMPLEMENTATION.md` and
`../../evidence/skill-registry-live-four-roots-20260718.json`.

## Local setup

Create a project-local virtual environment. Do not install the runtime into the
global Python environment.

```powershell
cd C:\path\to\zhimeng-longform-studio\services\writing-runtime
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Configure the desktop origin and database path explicitly:

```powershell
$env:ZHIMENG_ALLOWED_ORIGINS='["http://127.0.0.1:1420"]'
$env:ZHIMENG_DATABASE_PATH="$PWD\data\writing-runtime.sqlite3"
$env:ZHIMENG_TRUSTED_PROJECT_ROOTS='["C:\\path\\to\\novel-projects"]'
$env:ZHIMENG_TRUSTED_SKILL_ROOTS='["C:\\path\\to\\skill-package"]'
$env:ZHIMENG_MANAGED_SKILL_ROOT='C:\path\to\application-managed-skills'
$env:ZHIMENG_TOMATO_SKILL_ROOT='C:\path\to\tomato-novel-auto-distill'
$env:ZHIMENG_TOMATO_DOWNLOADER_SCRIPT='C:\path\to\Download-TomatoNovel.ps1'
$env:ZHIMENG_DISTILLATION_SKILL_ROOT='C:\path\to\novel-distillation'
$env:ZHIMENG_DISTILLATION_LIBRARY='C:\path\to\novel-distillation\library'
$env:ZHIMENG_DISTILLATION_PYTHON_EXECUTABLE='C:\path\to\python.exe'
$env:ZHIMENG_AGENT_PROVIDER_BASE_URL='https://provider.example/v1'
$env:ZHIMENG_AGENT_PROVIDER_API_KEY='local-secret'
$env:ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE='secret-store:provider-primary'
$env:ZHIMENG_AGENT_PROVIDER_MODEL='model-name'
$env:ZHIMENG_SSE_HEARTBEAT_SECONDS='15'
.\.venv\Scripts\python -m zhimeng_runtime
```

`ZHIMENG_ALLOWED_ORIGINS` accepts either a JSON array or a comma-separated
list. Origins must be complete HTTP(S) origins without a path, query,
fragment, credentials, or wildcard. An unlisted origin receives no CORS
headers.

Additional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ZHIMENG_ENVIRONMENT` | `development` | Runtime environment label |
| `ZHIMENG_DATABASE_PATH` | `./data/writing-runtime.sqlite3` | SQLite file |
| `ZHIMENG_TRUSTED_PROJECT_ROOTS` | empty | JSON array or semicolon-separated absolute read roots |
| `ZHIMENG_TRUSTED_SKILL_ROOTS` | empty | Ordered JSON array or semicolon-separated absolute Skill roots |
| `ZHIMENG_MANAGED_SKILL_ROOT` | repository `adapters/builtin` in source development | Application-owned managed Skill packages; empty disables managed execution |
| `ZHIMENG_TOMATO_SKILL_ROOT` | empty | Explicit development-only Tomato adapter root; empty disables source imports |
| `ZHIMENG_TOMATO_DOWNLOADER_SCRIPT` | empty | Explicit development-only downloader path; empty disables source imports |
| `ZHIMENG_TOMATO_DOWNLOAD_DIR` | `~/Desktop/番茄下载` | Generated downloader output directory |
| `ZHIMENG_TOMATO_SERVER_URL` | `http://127.0.0.1:18423` | Loopback downloader service origin |
| `ZHIMENG_TOMATO_MAX_WAIT_MINUTES` | `180` | Full-download wait limit |
| `ZHIMENG_POWERSHELL_EXECUTABLE` | `powershell.exe` | PowerShell executable for the fixed adapter |
| `ZHIMENG_DISTILLATION_SKILL_ROOT` | empty | Explicit development-only `novel-distillation` root; empty disables preview |
| `ZHIMENG_DISTILLATION_LIBRARY` | inferred only from an explicit development Skill root, otherwise empty | One development-only trusted formal library; never supplied by API clients |
| `ZHIMENG_DISTILLATION_PYTHON_EXECUTABLE` | current Runtime Python | Python executable for the fixed adapter |
| `ZHIMENG_DISTILLATION_TIMEOUT_SECONDS` | `120` | Skill subprocess timeout, 5-600 seconds |
| `ZHIMENG_AGENT_PROVIDER_BASE_URL` | empty | OpenAI-compatible API root; HTTPS or explicit loopback HTTP |
| `ZHIMENG_AGENT_PROVIDER_API_KEY` | empty | Optional bearer token; never returned by status APIs |
| `ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE` | `env:ZHIMENG_AGENT_PROVIDER_API_KEY` when a key is set | Non-secret credential slot identity included in the Provider dispatch fingerprint; change it when account or permission semantics change |
| `ZHIMENG_AGENT_PROVIDER_MODEL` | empty | Model name; URL and model must be configured together |
| `ZHIMENG_AGENT_PROVIDER_TIMEOUT_SECONDS` | `120` | Provider stream timeout |
| `ZHIMENG_AGENT_PROVIDER_MAX_TOKENS` | `4096` | Maximum response tokens |
| `ZHIMENG_AGENT_PROVIDER_TEMPERATURE` | `0.7` | Provider temperature |
| `ZHIMENG_ALLOWED_ORIGINS` | loopback ports `1420` and `5173` | Exact CORS allowlist |
| `ZHIMENG_SSE_HEARTBEAT_SECONDS` | `15` | Heartbeat interval |
| `ZHIMENG_PORT` | `8787` | Manual development port |

The production desktop launcher will supply a random port and short-lived
bearer token. Authentication and desktop process supervision remain future
integration work; this service never binds to a non-loopback host.

Source development may explicitly point the fixed Tomato/Distillation adapters
at the user's designated read-only Skill checkouts. There is no implicit
discovery under `~/.codex`, Desktop, WeChat, or another user directory. These
paths are compatibility inputs for development, not a release dependency. M3
owns the managed Skill import/execution contract and M6 must package an approved
version or copy it into application-controlled storage before package-test.

## Fixture and tests

`tests/fixtures/minimal-novel` is a repository-owned 01-09 project used for
Project Reader and Workbench E2E tests. It includes one prose chapter and a
`CH002_续写卡.md` document that must not be classified as prose. Tests use
temporary SQLite files and do not write to an author project.

`tests/fixtures/skills` contains nested and duplicate-name Skill sources. The
excluded-library test uses a small corpus sentinel whose file open is forbidden;
it proves discovery reads only the root manifest while leaving package/content
counters at zero without allocating a multi-gigabyte test file.

Source-import tests cover the Runtime state machine with a fake adapter, and
Workbench E2E uses HTTP route stubs. They do not prove a live external Tomato
download/PowerShell/distillation run.

```powershell
.\.venv\Scripts\python -m pytest
```

After changing public Pydantic models, regenerate the relevant checked-in schemas
and run the contract drift tests:

```powershell
.\.venv\Scripts\python scripts\export_project_contracts.py
.\.venv\Scripts\python scripts\export_skill_contracts.py
.\.venv\Scripts\python scripts\export_change_set_contracts.py
.\.venv\Scripts\python scripts\export_source_import_contracts.py
.\.venv\Scripts\python scripts\export_distillation_contracts.py
.\.venv\Scripts\python scripts\export_context_contracts.py
.\.venv\Scripts\python scripts\export_workflow_contracts.py
.\.venv\Scripts\python scripts\export_agent_candidate_contracts.py
.\.venv\Scripts\python scripts\export_writer_contracts.py
.\.venv\Scripts\python scripts\export_provider_recovery_contracts.py
```
