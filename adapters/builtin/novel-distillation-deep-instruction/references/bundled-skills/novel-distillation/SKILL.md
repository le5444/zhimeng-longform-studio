---
name: novel-distillation
description: "Distill fiction into a reusable, searchable writing knowledge base. Use when the user asks for 小说蒸馏, 小说拆解, 网文拆解, 作品蒸馏, 作者蒸馏, 类型蒸馏, 角色蒸馏, 爆款小说机制, 文风资料库, 蒸馏知识库, 动态检索, 写作机制卡, narrative engine analysis, reusable story templates, chapter hooks, reader emotion loops, or wants to send novels one by one for classification, storage, retrieval, and later writing integration without copying protected prose."
---

# Novel Distillation

## 全局硬规则
- 蒸馏目标是提取机制，不是仿写原文；输出只存读者承诺、结构、节奏、冲突、表达 DNA 和验证问题。
- 不复刻受版权保护文本或在世作者独特声纹；不存长原文，不输出“照 X 写”。
- 每张入库卡必须有结构化 `taxonomy`、`type_payload`、`tags`、`retrieval`、`safety` 和证据范围；`scene` 卡必须有 `scene_shell_signature`。
- 入库后必须跑 `validate` 和 `health`；库为 `blocked/degraded` 时，不直接供写作系统使用。
- `health` 必须同时看 `structural_health` 与 `validation_maturity`：结构健康但高风险卡 validation ladder 只有 warn 时，库是 `degraded/risk_ready`，不能误报 healthy，也不能把它误判成“缺书”而盲目下载。
- 写作联动必须先检索再压缩上下文，按 profile 配额召回，不整库加载，不固定 3-8 张。
- 防污染边界高于模仿效果；实体泄漏、设定壳泄漏、场景壳相似或项目真值污染命中时，从 `L3_DISTILLATION_RETRIEVAL` 重来。
- 审稿后用 `outcome`、`reason`、`layer` 回灌反馈，区分错卡、错用、结果差和污染。

## Overview

Distill novels, web novels, genres, authors, scenes, voices, and character archetypes into reusable story mechanics and searchable knowledge cards. The core principle is: extract how the fiction works, not what it said.

Do not treat this as prose imitation. Convert source material into narrative engines, constraints, reader promises, pacing rules, and generation tests.

This skill now has two modes:

- **Single distillation**: produce a report or one card from supplied material.
- **Knowledge-base mode**: when the user says "我发一本你蒸馏一本", create/update a local distillation library, classify the work, store compact cards, and make them retrievable by writing skills.
- **Benchmark mode**: when the user says "对标这本、这本为主其他为辅、模仿并超过", build a benchmark contract from one book or one primary book plus supporting books. This aligns reader experience and craft mechanisms, not protected expression or source setting shells.

## Object Router

Classify the user's target first:

| Target | Distill | Best output |
|---|---|---|
| Author | Repeated narrative instincts across works | Author-level story operating system |
| Single work | The mechanism that makes one novel work | Work dossier + reusable template |
| Genre/type | Shared rules and major variants | Genre framework + differentiation map |
| Character/archetype | Desire, wound, mask, agency, change pattern | Character engine + scene behaviors |
| Platform/audience | Reader promise, cadence, retention devices | Serialization and hook playbook |
| Scene/function | Repeated scene mechanics | Scene card: use case, trigger, turn, payoff |
| Voice/style DNA | Abstract prose behavior | Voice card: sentence rhythm, POV distance, dialogue rules, forbidden imitation |
| Packaging | Title, tags, blurb, first-click promise, fulfillment chain | Packaging card: title patterns, hook formulas, A/B dimensions, promise ladder |
| Benchmark | One primary work, or one primary + supporting works | Benchmark card: source weights, emulate mechanisms, surpass targets, differentiation rules, forbidden overlaps |

If the target is vague, ask one concise question: "Are we distilling an author, a single work, a genre, or a character?"

## Source Strategy

Prefer source material in this order:

1. User-provided chapters, outlines, notes, interviews, or author essays.
2. Public-domain text or legally accessible excerpts.
3. Author interviews, creation notes, platform serialization data, reader reviews, and reputable criticism.
4. Plot summaries only as navigation aids, never as the main evidence.

When current platform rankings, reader reception, or recent author activity matter, browse for up-to-date sources. Avoid relying on unverifiable reposts, low-quality summaries, or commentary that has no examples.

## Safety Rules

- Do not reproduce long copyrighted passages.
- Do not generate close imitations of a living author's prose style.
- Do not output "write it exactly like X"; translate X into abstract craft rules and create original examples.
- Do not store long source excerpts in distillation cards. Store evidence scope, tags, mechanisms, and short non-substitutive notes.
- Keep direct quotations short and only when needed for evidence.
- Separate evidence from inference. Label inferred mechanics as inference.
- External prompt/workflow sources are absorbed as mechanisms only: pass reproducibility, transferability, and system-compatibility checks before storing; do not copy their module topology, proprietary wording, absolute style rules, fake quantitative claims, or direct SKILL.md fusion habit.

## Knowledge Base Workflow

Use `scripts/distill_kb.py` for durable storage and retrieval:

```bash
python scripts/distill_kb.py init "<library_dir>"
python scripts/distill_kb.py add "<library_dir>" --card card.json
python scripts/distill_kb.py coverage "<library_dir>" "电竞 群像 训练赛 角色同声" --profile scene
python scripts/distill_kb.py search "<library_dir>" "电竞 群像 版本理解 训练赛" --profile scene
python scripts/distill_kb.py context "<library_dir>" "电竞 群像 版本理解 训练赛" --profile scene
python scripts/distill_kb.py context "<library_dir>" "番茄 男频 玄幻 第一章 金手指激活 爽" --profile draft_assist
python scripts/distill_kb.py feedback "<library_dir>" "<card_id>" --outcome success --reason success_fit --layer L4_DRAFT_EXECUTION --task draft_scene
python scripts/distill_kb.py index "<library_dir>" --dimension genre --dimension subgenre --dimension scene_function
python scripts/distill_kb.py taxonomy
python scripts/distill_kb.py schema --type scene --json
python scripts/distill_kb.py type-schema --type scene
python scripts/distill_kb.py profiles
python scripts/distill_kb.py validate "<library_dir>"
python scripts/distill_kb.py health "<library_dir>"
python scripts/distill_kb.py source-hygiene "<library_dir>" --json
python scripts/distill_kb.py quarantine-bad-jsonl "<library_dir>" --json
python scripts/distill_kb.py problem-guidance "赢了也不改局势" --json
python scripts/distill_kb.py audit-draft "<library_dir>" "<draft.md>" --profile review --query "奇幻 历史 反转 去AI"
python scripts/kb_web.py "<library_dir>" --port 8765
python scripts/distill_source_packet.py "<source_packet_dir_or_packet.manifest.json>" --out-dir "<draft_out_dir>"
```

Default packaged library location is `%USERPROFILE%\.codex\skills\novel-distillation\library\`. A project may override it with `distillation_library_path.txt`, and older workspaces may still keep a local `novel-distillation-library/`. Writing skills should never load the whole library; run `coverage` first, then `context`, and read only the top matching cards.

`validate / health / coverage / context / stats / index / audit-draft / source-hygiene` 都是只读命令；遇到坏 `cards.jsonl` 行时只报告，不自动创建隔离文件。只有用户或维护者显式运行 `quarantine-bad-jsonl` 才会把坏行复制到 `reports/quarantine/`，原 `cards.jsonl` 仍不改。

`distill_source_packet.py` 是 `source packet -> distillation cards` 的 API/file-handoff 补强层：它读取 `packet.manifest.json`、`.txt` 和 `.metadata.json`，输出 `cards.draft.json`、`distillation_report.md`、`tool_result_envelope.json`，并补一层深蒸馏审计面：

- `source_index.json`：输入文件、章节索引、chunk 计数和 artifact 路径。
- `metrics.json`：开篇句长、短句比例、章节规模和 feature density 摘要。
- `chapter_features.jsonl`：按章节落盘的统计/feature 行，不存长原文。
- `chunk_index_small.jsonl` / `chunk_index_big.jsonl` / `chunk_index_super.jsonl`：small / big / super 三层 chunk 索引，只存跨度、hash、章节归属和统计，不存 chunk 正文。
- `validation_report.md`：draft 级 validation ladder 与 artifact contract 检查。

这些卡默认带 `draft_candidate=true` 和 `requires_human_distillation=true`，只证明源包已形成可审计蒸馏草案，不等于已完成机制蒸馏；正式入库前仍要人工/模型把 pending 字段改成可迁移机制，再运行 `distill_kb.py add`、`validate` 和 `health`。

标准入库链路写成一条事务：`source packet -> draft distillation cards -> reviewed formal cards -> add/replace -> validate -> health -> NLCL post-distill sync`。`draft_candidate=true`、`requires_human_distillation=true` 或 pending 字段未清的草案不得同步成 NLCL 正式 entries，也不得被 creation-suite 当硬规则消费。

### Distillation vs Project Adaptation

Keep two layers separate:

- **Neutral distillation layer**: default output for downloading, sweeping rankings, distilling one book, or distilling a genre. Cards should store platform/genre/packaging/scene/voice/validator mechanisms in abstract form. Do not mention the user's current novel, its title, protagonist, custom power system, chapter-by-chapter rules, or project-specific execution plan unless the user explicitly asked for a benchmark or project application.
- **Project adaptation layer**: created only when the user names a project, asks for 对标/主辅对标/模仿并超过, starts 立项/章纲/正文/审稿, or says to use the distilled material for a specific book. This layer translates neutral mechanisms into project rules and should be stored as benchmark/project cards or in the novel-creation-suite project files, not as generic source distillation.

Example: a neutral card may say "前三章必须兑现标题承诺" or "清算类场景要闭合谁欠、欠什么、如何偿还、双方状态变化"; a project-adapted rule may say "《替全宗挡劫十年》第一章必须写清替谁挡了什么劫，第三章完成第一次可见收债." The second form belongs to benchmark/project execution, not generic genre distillation.

### Library Layout

```text
library/
  manifest.json
  cards.jsonl
  cards/
    work/
    author/
    genre/
    scene/
    voice/
    trope/
    platform/
    packaging/
    benchmark/
    validator/
  raw/        # optional source pointers or generated card batches; packaged skill libraries should not include raw EPUB/TXT source packets
  reports/    # optional distillation reports
```

### "One Book In, One Set Of Cards Out"

When the user sends a book or file:

1. Identify source scope, genre, platform, target use, and whether the material is complete or sampled.
2. Distill in layers: work card, genre/type tags, scene cards, voice/style DNA, packaging cards if commercial positioning is relevant, benchmark card only if the user explicitly asked for one-book/multi-book 对标, validator card if useful. For a full book or large sample, produce a V2 card set instead of a single card: 1 work card, 1-3 genre/trope cards, 3-12 scene cards, 1-3 voice cards, 0-2 packaging cards, 0-1 benchmark card, and 1-5 validator cards.
   - `voice` 卡不是“短句/口语”标签。必须抽取 `human_web_prose`、`sentence_variety`、`scene_anchor_rules`、`dialogue_pressure_rules`、`anti_ai_failure_signatures`、`readability_acceptance_tests`，专门记录真网文顺读、角色声纹、人味来源和过度短句/语义残缺的失败边界。
   - 若用户目标是去 AI 味或吸收小说精髓，优先补 `voice` + `validator` 卡，而不是继续增加泛泛 `work` 卡数量。
3. Classify each card with `references/taxonomy.md`: structured `taxonomy` first, compact `tags` second. Tags should be precise enough for retrieval but not so narrow that they never match.
4. Add anti-contamination boundaries from `references/anti-contamination.md`: forbidden source entities, setting shells, signature phrases, recognizable scene shells, and risk labels.
5. Write compact JSON cards following `references/distillation-card-schema.md` and `references/card-type-schemas.md`; every card must include a type-specific `type_payload`. Every `scene` card must include `type_payload.scene_shell_signature` so later drafts can be audited for source-shell order leakage.
6. Add them with `scripts/distill_kb.py add`.
7. Run `scripts/distill_kb.py validate`.
8. Run `scripts/distill_kb.py health` and report whether the library is `healthy`, `degraded`, or `blocked`.
9. Run the NLCL post-distill sync gate, usually through `$tomato-novel-auto-distill/scripts/sync_nlcl_after_distill.ps1 -LibraryDir "<library_dir>" -StrictSeed`, so `novel-kb-manager` refreshes active-card entries, source inventory, duplicate packet audit, and latest sync report.
10. Return a short import report: cards added, strongest taxonomy dimensions, best use cases, caveats, retrieval examples, anti-contamination boundaries, packaging lessons if any, distillation health, NLCL sync status, health gaps, and weak spots still needing more source evidence.
11. 对 `scene / voice / benchmark / validator` 高风险卡，补或检查 `validation_ladder`。至少记录 internal_consistency、predictive_validation、blind_sample_validation、extreme_scenario_stress_test 中已完成的状态；未验证卡只能作为 `risk_ready` 入口，不得在 NLCL/creation-suite 中当硬规则。写作层使用后必须通过 feedback 或 NLCL `last_usage_evidence` 回流，`selected` 不等于 `validated`。
12. 定期运行 `source-hygiene` 只读盘点 raw/source packet：检查绝对路径可移植性、缺 metadata/manifest、失效引用、duplicate source/text hash。该报告不能自动移动、删除或归档原文；重复源必须比较章节覆盖和抽取差异后人工裁决。

Do not run `add/replace/archive/delete` in parallel with `validate/health/coverage/context/audit-draft`. Ingestion is a sequential transaction: mutate first, then validate, then health, then target coverage. Parallel validation can read the previous card count and produce a false stale result.

### Batch Distillation vs Benchmark Mode

Multiple books do not automatically mean benchmark writing. By default, one book or several books are simply distilled as separate sources, classified into the same long-term library, and later combined by retrieval when a writing task needs that type of mechanism. Every distilled result still goes into `cards.jsonl` and the card folders, so the user can inspect and reuse it later.

Benchmark mode is a special overlay. Trigger it only when the user explicitly says "对标", "主书", "辅书", "主辅", "这本为主其他为辅", "单书主对标", "多书主辅对标", or "模仿并超过". Do not infer benchmark mode from "一本书", "几本书", or "多本书" alone.

### Benchmark Mode: Explicit One Book Or Several Books

Use this when the user wants to "对标", "主书为主、辅书为辅", or "模仿并超过". Keep the normal ingestion flow; benchmark mode adds one extra contract layer.

- **Single-source benchmark**: one book is the primary source. Create ordinary `work/scene/voice/packaging/validator` cards as useful, plus 1 `benchmark` card with `benchmark_mode=["单书主对标"]`.
- **Primary + supporting benchmark**: the user gives several books and names the main one. The primary source controls reader promise, chapter rhythm, opening pressure, and core loop. Supporting books only repair named weaknesses such as packaging, first payoff, pressure model, relationship pull, or anti-AI texture. Create 1 `benchmark` card with `benchmark_mode=["多书主辅对标"]`.
- Source weights should be explicit: one-book defaults to primary=1.0; multi-book usually uses primary=0.55-0.75, with supporting books split by purpose.
- A benchmark card must include `primary_sources`, `supporting_sources`, `source_weights`, `emulate_mechanisms`, `surpass_targets`, `differentiation_rules`, `forbidden_overlaps`, `retrieval_bias`, and `acceptance_tests`.
- "模仿并超过" means: match reader effect and improve structure efficiency, opening clarity, payoff density, packaging fulfillment, or validation strictness. It never means copying prose, names, unique setting shell, scene order, signature lines, or a living author's recognizable voice.
- 番茄扫榜或 benchmark 额外启用 `validator-fanqie-benchmark-low-logic-shell-20260609`：只学习目标读者、点击承诺、脑洞新鲜点、节奏效率和前 800 字兑现方式；低逻辑外壳、热梗皮、榜书声纹、粗糙表达、源书场景顺序和换皮设定必须写入 `forbidden_overlaps` 或防污染边界，不得当成平台感。
- Writing from benchmark mode must run `coverage --profile benchmark` before `context --profile benchmark`. If no same-route benchmark card exists, pause and ask for a primary book or primary+supporting books/samples.

Example:

```bash
python scripts/distill_kb.py template --type benchmark
python scripts/distill_kb.py coverage "<library_dir>" "番茄 男频 玄幻 单书 对标 开篇超越 金手指激活 爽" --profile benchmark
python scripts/distill_kb.py context "<library_dir>" "番茄 男频 玄幻 主书 读者体验对标 节奏超越 第一章 金手指激活" --profile benchmark
```

## Single Distillation Workflow

1. **Frame the task**: identify target, language, genre, intended use, and whether the user wants analysis, a writing template, or a new story seed.
2. **Collect evidence**: use supplied text first. If sources are weak, say so and narrow the claim.
3. **Layer the reading**:
   - Surface: setting, tropes, powers, roles, plot events.
   - Middle: conflict structure, relationship web, pacing, reveal order.
   - Deep: reader promise, desire economy, emotional loop, value tension.
4. **Extract engines**: use `references/narrative-engines.md` for the seven core engines.
5. **Synthesize rules**: produce 3-7 narrative models and 5-10 decision heuristics. Prefer "when X, do Y because Z" rules with examples.
6. **Build the kit**: use `references/output-templates.md` when the user wants a full dossier, reusable template, chapter outline, or character system.
7. **Validate generativity**: use `references/quality-check.md` to test whether the distilled system can generate new, non-infringing fiction.

## Default Output

For a normal request, answer in Chinese unless the user uses another language. Use this structure:

1. **一句话判断**: what makes the target work.
2. **叙事操作系统**: 3-7 core engines/models.
3. **可复用规则**: concrete writing heuristics.
4. **误用风险**: what shallow imitators copy incorrectly.
5. **生成测试**: one original story seed, character, chapter hook, or twist that proves the mechanism can run.

For quick questions, compress the output. For serious distillation, read the references and produce the full dossier.

## Writing Integration

When a writing skill asks for distillation support:

1. Build a retrieval query from platform, audience, genre, subgenre, chapter function, scene type, protagonist pressure, relationship mode, desired emotion, problem symptom, and style constraints.
2. Choose a retrieval profile by task: `scene/chapter/draft_assist/review/rewrite/volume/deai/packaging/benchmark`. Do not treat 3-8 as universal; profile quotas decide card count and card types. Use `draft_assist` for first drafts when project truth is already strong and the risk is formulaic over-control.
3. Run `scripts/distill_kb.py coverage --profile <profile>` before writing. `healthy` continues; `degraded` requires user choice or risk flag; `blocked` pauses formal writing and asks the user to feed 1-3 same-platform/same-audience/same-genre works or samples before continuing.
4. Run `scripts/distill_kb.py context --profile <profile>` when passing results into writing. Use `search` for exploration/debugging.
5. Use only abstract mechanisms, reader promises, scene rules, style DNA, and validation tests.
6. Never let retrieved cards override the current project's truth files, user instructions, safety constraints, or chapter control card.
7. Apply card conflict arbitration: user instruction > project truth > chapter control card > platform/audience promise > genre contract > scene function > distilled scene/trope/work cards > external voice cards > validator cards.
8. Enforce the anti-contamination gate exported by `context`: no source entity leakage, no setting-shell leakage, no scene-shell similarity, no signature-phrase similarity, no project-truth override.
9. Enforce the anti-formula gate: if the draft satisfies the cards but reads like a checklist, reduce card pressure, mark `fail_formulaic/rejected_formulaic`, and rewrite from `L4_DRAFT_EXECUTION` or `L5_DEAI_STYLE`.
10. Before accepting a draft that used distillation, run `scripts/distill_kb.py audit-draft` or perform the same checks manually. If a generated draft fails a retrieved card's validator or contamination gate, rewrite from the traced layer: retrieval/card issue from `L3`, draft execution issue from `L4`, style-only issue from `L5`.
11. When a review issue names a `problem_symptom`, use `scripts/distill_kb.py problem-guidance "<symptom>" --json` to fetch single-symptom `review_subcategory` and `repair_guidance` without scanning the whole library. Pass it to `novel-chapter-reviewer` / `novel-score-improver` as auxiliary routing evidence only; project truth, user cold-read, and reviewer evidence still win.
12. After review, record feedback with `scripts/distill_kb.py feedback --reason ... --layer ...` so successful cards rise and rejected cards fall for the right reason.

For title, tag, blurb, first-click, or promise-fulfillment tasks, use `--profile packaging`. A packaging context should improve clickability only when the first 500 words, chapter 3, chapter 10, and chapter 30 can fulfill the promise; otherwise lower the promise or rewrite the opening.

For one-book or multi-book benchmark writing, use `--profile benchmark`. A benchmark context should contain exactly one benchmark contract plus supporting work/genre/scene/voice/packaging/validator cards as available. If benchmark coverage is blocked, ask for 1 primary book or 1 primary + 1-3 supporting books/samples and do not claim benchmark alignment until the card exists.

For Fanqie benchmark or ranking-sweep adaptation, the context should include a low-logic-shell validator when available. If the benchmark or draft learns only a hot-rank skin, low-logic setting shell, rough prose, source scene order, or recognizable benchmark-book voice, reject it from `L3_DISTILLATION_RETRIEVAL` with `fanqie_benchmark_low_logic_shell` / `rejected_contamination` before it reaches prose polishing.

Voice/style distillation may use source prose as evidence, but only to extract abstract craft behavior: POV distance, sentence rhythm bands, action/description ratio, dialogue pressure, emotion handling, human web prose readability, and anti-AI texture. Do not distill a living author's recognizable voice for imitation. Put reusable craft in `voice` cards and forbidden fingerprints in `do_not_copy`, `signature_phrases`, and `contamination_risks`.

When a draft is rejected as “AI味重” even if a detector or local probe does not flag it, treat that as a real reader signal. Requery with `--profile deai` and problem terms such as `真网文感 自然顺读 语义残缺 句式过短 生活语序弱 角色同声`, then repair from `L5_DEAI_STYLE` or `L4_DRAFT_EXECUTION` instead of claiming the detector passed.

## Web Library Manager

The library can be browsed and edited through a local web UI:

```bash
python scripts/kb_web.py "<library_dir>" --port 8765
```

The web UI edits the same `cards.jsonl` used by `search`, `coverage`, `context`, `audit-draft`, and writing skills. It supports browsing, filtering, card JSON editing, creating cards from templates, archive/restore/delete, validation, health checks, and coverage/context tests. Archived cards stay visible when requested but are excluded from normal retrieval, coverage, health, taxonomy index, and contamination audit.

## References

- `references/narrative-engines.md`: seven narrative engines and diagnostic questions.
- `references/output-templates.md`: reusable dossier, genre, character, and chapter templates.
- `references/quality-check.md`: source quality, originality, and generativity checks.
- `references/distillation-card-schema.md`: required card fields and card types.
- `references/knowledge-base-protocol.md`: ingestion, classification, retrieval, and maintenance protocol.
- `references/writing-integration.md`: how writing skills query and validate against the distillation library.
- `references/taxonomy.md`: canonical tag dimensions, query shape, and retrieval precision rules.
- `references/anti-contamination.md`: hard prompt constraints and rewrite gates that keep external sources from polluting the current novel.
- `references/card-type-schemas.md`: JSON-schema-level required `type_payload` fields for each card type.
- `references/retrieval-protocol.md`: hybrid retrieval ranking, task profiles, card quotas, and feedback reasons.
