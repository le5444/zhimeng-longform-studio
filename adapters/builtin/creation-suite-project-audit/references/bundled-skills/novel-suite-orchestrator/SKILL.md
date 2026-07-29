---
name: novel-suite-orchestrator
description: "统筹小说创作项目的全链路技能路由、立项字数问诊、动态上下文、01-09 项目目录生成、跨文件联动、多层校验、章节去AI化、朱雀/AI检测报告反向修稿、人工特征补强、项目审稿评分和低分返工循环；当任务牵一发而动全身或一次生成/修改多份小说文件时使用。"
---

# 小说全链路总控

## 全局硬规则
- `01-09` 真值文件、已定稿正文和用户明确指令优先于蒸馏卡、提示词模板、图谱、AI 结果和旧摘要。
- 每次生成、修改或审稿前先建上下文健康标记；`blocked/degraded` 时只交修复范围或带风险草稿，不交定稿。
- 蒸馏只学机制，不学来源专名、设定壳、名场面结构、标志句和作者声纹；写作前先检索，禁止整库塞上下文。
- 章节、项目和正文返工必须带 `Layer ID`、失败签名和 `rewrite_from`；上游错误回上游，不用下游润色硬盖。
- 正文和项目文件必须对齐章节使命、信息边界、人物状态、伏笔、章末钩子、平台承诺和写后回填。
- 生成、续写或修改任何正文 `.md` 后，默认同步运行 `scripts/export_epub.py "<项目根目录>"`，把可读 EPUB 输出到项目内 `10_导出文件/`；除非用户明确说“这次不用导出”。EPUB 只收正式正文 `第XXX章_标题.md` 或压力测试/链路验证中的 `CHXXX_定稿.md`，默认排除 `章纲/`、控制卡、报告、回填、dry-run handoff、writer handoff、镜像目录和项目设定文件。
- EPUB 版式必须按小说阅读版式清洗：空行不生成空段，Markdown 围栏/反引号/标题符号/列表符号不作为无意义字符进入正文；系统面板、雷石等级等短提示转成干净的内嵌提示段；不得把生产注记、问题卡、审稿报告混入 EPUB。
- 作者样章和已采纳改稿优先用于校准本书声音，不用通用去 AI 规则抹平作者指纹。

## 定位
把小说项目当作工程维护。每次操作先定位目标，再建立全项目索引，读取上游、同区和下游，调用对应专属 skill，最后执行审稿、返工和回填。用户只点总控时，也要能统筹全部内部 skill。

## 必读共享协议
- `../../context-assembly-protocol.md`
- `../../core-writing-principles.md`
- `../../platform-retention-rules.md`
- `../../packaging-title-synopsis-rules.md`
- `../../workbench-distillation-workflow.md`
- `../../layer-traceability-protocol.md`
- `../../pressure-test-report-template.md`
- `../../validation-and-review-gates.md`
- `../../chapter-state-machine.md`
- `../../writeback-and-dynamic-state.md`
- `../../manifest.md`
- 若任务输入包含 `novel-kb-manager` / NLCL 的 `assemble-context` 结果、`chapter_call_list_md`、`route_health`、`context_health`、`context_degraded`、`state_freshness` 或项目 `.kb` 路径，先读取对应 `chapter_call_list`、`selected_ops_rules.md`、`selected_distillation_entries.md`、`route_health` 和 `state_freshness`；它们是本轮写作层调用合同，不是可忽略报告。
- `route_health` 只说明库路线成熟度；`context_health/context_degraded/state_freshness` 说明项目状态是否已覆盖上一章。两者不得互相替代。`route_health=ready` 但 `context_degraded=true` 时，不允许标定稿或 `approved/finalized`，只能先补写后回填，或按用户明确要求交风险草稿。

## 执行模式
若本轮输入来自 `novel-kb-manager` / NLCL，先执行写作层交接预检，再选择 `quick/standard/strict`。最低要求：
- 读取 `writer-entrypoint.md`，明确本轮是 `dry_run`、`risk_draft` 还是 `live_draft`。
- 用 `consume_kb_manager_context.py` 或等价人工复核确认 `handoff_api_contract.status=loaded`，且 `success_criteria / failure_codes / fallback_manual_step` 非空；缺失时只能人工复核或 dry-run，不得声称 API 链路已闭环。
- `route_health=ready` 只代表路线可调用；还必须检查 `context_health/context_degraded/state_freshness`。`context_degraded=true`、上一章 state 未覆盖、或 spec 成功标准缺字段时，不进入正式正文，只能返 `L1_PROJECT_STATE` / `L2_CONTROL_CARD` 或交风险稿。
- 把 `selected_ops_rules.md`、`selected_distillation_entries.md` 和 `chapter_call_list` 传给控制卡、正文、去 AI 和审稿四段；被选中的 mandatory 项后续必须通过 `record-call-usage` 记录 used/unused/missed，不能把 selected 当 used。
- 若输入已含 `context_inclusion_trace / activation_mode_audit / distractor_file_budget / block_context_bundle`，总控必须要求下游显式消费；缺这些字段时，要么回 NLCL 重装配，要么在控制卡阶段补齐，不能让正文层自行“凭经验读库”。
- 若输入已含 `delegated_edit_checkpoint_gate / accepted_version_guard / readonly_bible_gate / append_only_timeline / story_tracker_delta`，总控必须把它们串入章节闭环：谁能改结构、谁只能改 L5、accepted version 是哪版、哪些真值只读、写后要追加哪些 delta，都要留证据。

先选择 `quick/standard/strict`：
- `quick`：状态询问、局部片段、轻量试写，只做最小上下文和红线检查，不标定稿。
- `standard`：默认模式；用于正常写章、单文件维护和项目审稿，走索引、上下游、审稿、返工和必要回填。
- `strict`：用于发布前、卷末、黄金三章、重大设定改动、低分返工后、项目迁移或用户要求全自动闭环。先运行 `novel-creation-suite/references/bundled-skills/novel-suite-orchestrator/scripts/project_audit.py` 或等价结构体检，再全量扫描 01-09、最近正文、版本链和旧目录污染。

发现真值冲突、旧目录污染、连续低分、关键状态缺失或 P0/P1 脚本体检项时，升级到 `strict`；不要静默降级。

## 新项目流程
1. 调 `novel-project-intake` 确认目标字数、平台倾向、题材、单章字数、卷数、章节规模和工作强度。
2. 创建用户指定的 `《[书名]》创作项目/` 目录，保留 01-09 完整结构；短篇可占位，不删除专属文件。
3. 先生成作者意图/当前焦点（可写入项目定位或文风规则），再按顺序生成：项目定位 -> 世界观 -> 人物 -> 剧情大纲 -> 写作规范 -> 正文骨架 -> 运营数据 -> 素材库 -> 完本复盘占位。项目定位阶段必须跑包装链：书名与标签 -> 核心卖点 -> 小说简介 -> 黄金三章兑现表。
4. 每完成一个阶段运行 `novel-project-reviewer`；低于 80 分交给 `novel-score-improver` 修上游后再同步下游。
5. 若用户提供旧文、对标书或外部素材，先用“转化而非移植”：只提取情绪功能、冲突架构、信息释放、节奏技法和读者承诺，不照搬桥段外壳。

旧项目、迁移项目或用户要求“体检/全自动处理”时，优先运行只读体检脚本：

```bash
python novel-creation-suite/references/bundled-skills/novel-suite-orchestrator/scripts/project_audit.py "<项目根目录>"
```

把脚本输出的 `health`、P0/P1/P2、旧目录污染和下一步路由作为上下文健康证据；脚本不替代人工审稿，P0/P1 必须进入统一问题卡。

快速开书或用户只要轻量推进时，可用四阶段流：灵感 idea（脑洞、题材承诺、读者情绪）-> 架构 architecture（世界/人物/主线/规则）-> 编排 arrangement（分卷、章节 beats、追读钩子）-> 写作 writing（正文、去模板化、审稿、回填）。但最终仍要映射回 01-09 文件和专属 skills。

若采用雪花式从零开书，阶段性询问用户是否满意；用户确认后再进入下一层。非交互批量生成时，用项目审稿分数代替满意门，低于 80 分不进入下游。

## 写作台式工作流
用户提供网站、提示词库、蒸馏卡或 AI 工作台项目时，先按六阶段归类：开书构思、设定与结构、正文写作、修改与诊断、平台投放、进阶工具。每条提示词或资产都要归入对应子 skill，而不是当作孤立模板复制。

采用人/AI 责任比例：构思人 70/AI 30，大纲人 60/AI 40，初稿人 50/AI 50，修改人 80/AI 20。AI 可以生成变体、细化、拆解、审稿和消痕；核心创意、人物灵魂、价值选择和最终取舍由作者或项目规则决定。

写作、审核、修文必须分角色执行。即使当前环境实际调用的是同一个模型，也要按三套口径跑：写作只负责让场景成立，审核负责找结构、体验、平台和冷读问题，修文只在结构基本通过后处理自然顺读和人味。不得用“写作模型自评通过”替代章节审稿，也不得用去 AI 润色覆盖 `L2_CONTROL_CARD` 或 `L4_DRAFT_EXECUTION` 的失败。

若存在蒸馏资产，按 `workbench-distillation-workflow.md` 接入上下文栈：技能层 -> 作者指纹层 -> 蒸馏层 -> 关联文件 -> 当前正文 -> 验证层 -> 用户指令。蒸馏只学习生成机制；若出现原书专名、桥段或可识别句式，必须重写为原创机制。

## 蒸馏知识库联动
`novel-distillation` 是独立蒸馏系统；本总控只做检索和写作联动，不把蒸馏库当真值文件。

入库路由：
- 用户说“下载小说、下书、下载并蒸馏、自动蒸馏番茄、把这几本番茄下下来拆解、我不给文件你直接下”时，先调用 `$tomato-novel-auto-distill`：搜索/确认候选 -> 下载到桌面 `番茄下载` -> 解 EPUB -> 生成 `<库路径>/raw/tomato-downloads/` 源包；随后调用 `novel-distillation` 知识库模式拆卡、validate、health。
- 用户说“发一本蒸馏一本、蒸馏入库、建立资料库、文风知识库、分类 skills”时，交给 `novel-distillation`，按其知识库协议生成 work/genre/scene/voice/validator 卡。
- 用户只说“一本/几本/多本书”时，交给 `novel-distillation` 的普通批量蒸馏：每本书拆卡、分类入库，后续写作按任务检索组合调用。
- 用户明确说“对标这本、这本为主其他为辅、模仿并超过、主书/辅书/主辅”时，交给 `novel-distillation` 的 benchmark 模式：一本书生成 `单书主对标` 合同，多本书生成 `多书主辅对标` 合同；普通卡照常入库，额外生成 1 张 `benchmark` 卡。
- 若对标书只有书名/book_id/番茄链接而没有本地文件，先走 `$tomato-novel-auto-distill` 获取源包；不得要求用户手动下载或拖文件，除非搜索结果重名且无法判断目标书。
- 入库卡必须包含结构化 `taxonomy`、类型化 `type_payload`、6-14 个核心 `tags`、`retrieval.preferred_queries/negative_queries` 和 `safety` 禁用边界；缺 taxonomy、type_payload 或防污染字段时，先让 `novel-distillation` 补卡，不直接给写作系统用。
- 默认库路径优先级：用户指定路径 > 项目根目录 `distillation_library_path.txt` > `%USERPROFILE%\.codex\skills\novel-distillation\library\` > 旧工作区 `novel-distillation-library/` 兼容兜底。
- 入库后把高价值卡的标签、适用任务和检索例句写入项目修改记录或文风规则的“可用蒸馏资产”摘要，但不把卡全文复制进 01-09。

写作召回：
- 写章、续写、审稿、去 AI、低分返工和包装任务前，若存在蒸馏库，先运行目标题材覆盖度检查：

```bash
python novel-distillation/scripts/distill_kb.py coverage "<库路径>" "<平台 受众 题材 子类型 章节功能 场景功能 情绪目标 问题症状>" --profile chapter
```

- `coverage.target_health=healthy`：继续导出 `distillationContext` 并进入对应写作 skill。
- `coverage.target_health=degraded`：向用户说明缺口、缺少的卡型和风险；用户同意继续时只能交带风险草稿，并在控制卡标记 `distillation_coverage=degraded`。
- `coverage.target_health=blocked` 或 `writing_gate=pause_and_request_sources`：暂停正式写作，不交定稿；主动询问用户是否先喂 1-3 本同平台、同受众、同题材或同场景功能的书/样章。用户提供后交给 `novel-distillation` 蒸馏入库、validate、health、coverage 复验，再恢复写作。
- 用户明确要求“别等资料，先硬写”或当前是压力测试时，可以绕过 blocked，但必须标记 `coverage_override=user_requested_risk_draft` 或 `coverage_override=pressure_test_risk_draft`，且只交风险草稿，不标定稿。优先使用 `build_distillation_context.py --allow-risk-context` 或 `coverage --profile mechanism_transfer` 导出风险上下文；若仍无卡可用，才标记 `distillationContext: none`。
- 写章、续写、审稿、去 AI、低分返工前，若存在蒸馏库，先用平台、受众、题材、子类型、章节功能、场景类型、情绪目标、当前问题和风格限制组装查询。
- 按任务选择 profile：单场景用 `scene`，单章/控制卡用 `chapter`，审稿用 `review`，返工用 `rewrite`，分卷规划用 `volume`，去 AI 用 `deai`，书名简介包装用 `packaging`。若上一章存在 `due/debt_interest_due` 叙事债务，先补跑 `debt_recall`，召回“如何兑现或加压这类债务”的机制卡，再进入正式写章。
- 对标写作用 `benchmark`：检索前查询必须带 `单书主对标/多书主辅对标 + benchmark_goal + 平台/受众/题材 + 章节/场景任务`。`writing_gate=pause_and_request_benchmark_sources` 时，暂停正式写作，请用户喂 1 本主书或 1 本主书 + 1-3 本辅书/样章。
- 运行 `novel-distillation/scripts/distill_kb.py context "<库路径>" "<查询>" --profile chapter` 或等价检索。`search` 只用于调试。
- 优先使用统一桥接脚本生成 `distillationCoverage + distillationContext`，避免每次手拼 `coverage/context` 两段命令：

```bash
python novel-creation-suite/references/bundled-skills/novel-suite-orchestrator/scripts/build_distillation_context.py "<项目根目录>" "<查询>" --profile chapter
```

- 该脚本会先跑 `coverage --json`，只有 `target_health=healthy` 才继续跑 `context --json`，并把结果压成写作系统可直接消费的上下文包。
- 压力测试或用户明确硬写时可追加 `--allow-risk-context`。此时 `blocked/degraded` 不会被伪装成健康，只导出 `selected_cards`、`quota_gaps`、`missing_types`、`risk_flags` 和 `formal_coverage_blocker`；正文必须标为风险草稿，并在控制卡写入覆盖度降级原因。
- 召回卡必须形成 `distillationContext`：`card_id`、taxonomy、type_payload、选中理由、可用机制、验证问题、禁用边界、`forbidden_source_elements` 和 `anti_contamination_gate`。
- 检索结果与当前项目冲突时丢弃；用户样章和本书文风规则优先于外部 voice 卡。
- 生成正文提示词必须带“蒸馏防污染硬约束”：只用机制和验证，禁止源专名、设定壳、名场面结构、标志句和声纹过拟合；项目真值高于蒸馏卡。
- 对标模式下，生成提示词还必须带“主书只管读者承诺和节奏，辅书只管指定短板，原创外壳必须重做”的约束；不得把“模仿并超过”解释成复刻原书表达或设定。
- 审稿时检查：是否用了错误卡、是否只学表层、是否复刻原书壳、是否出现实体泄漏/设定壳泄漏/场景壳相似/标志句相似/项目真值污染、是否未通过卡片验证。失败则从检索或控制卡层重来，不只改句子。
- 章节审稿或作者反馈后，调用 `distill_kb.py feedback --reason ... --layer ...` 记录卡片结果：`success/mixed/fail/rejected` 加细分 reason。连续被拒绝的卡降权或回到 `novel-distillation` 重蒸馏。

四系统 dry-run 联动验证：
- 当 `novel-kb-manager assemble-context` 已输出上下文包，且目标是验证接口链路而不是生成正式正文，可运行 `scripts/dry_run_writer_harness.py`。
- 正式或 dry-run 接收 NLCL 上下文前，优先运行 creation-suite 主入口 `novel-creation-suite/scripts/consume_kb_manager_context.py <kb_context_pack.json> --out <writer_handoff.json>`，把 context pack 压成 creation-suite handoff。bundled 目录中的同名脚本仅保留旧 Python API 兼容，其 CLI 默认只打印短 envelope；需要查看 canonical handoff 时显式加 `--json`，不得把 `--legacy-json` 的大对象塞进写作上下文。输出中的 `handoff_api_contract` 必须指向 `nlcl_assemble_context_to_creation_suite`，并列出输入、输出、成功标准、失败码和 fallback。
- 该脚本只把上下文包转成控制卡、mock 草稿骨架、审稿清单和 `.writeback_state.json`，用于验证 `tomato-novel-auto-distill -> novel-distillation -> novel-kb-manager -> novel-creation-suite` 的交接形状。
- 若使用 `--project-root` 生成 `05_正文内容/dry_run_handoff/` 镜像，普通 `project_audit.py` 可能因缺少完整 01-09 文件返回 `blocked`；这只说明镜像不是完整小说项目，不能直接当作 handoff 失败。要评价完整项目健康，先创建完整 01-09 测试项目。
- `route_health=ready` 才能继续；`needs_library_work` 必须拒绝生成草稿产物；`risk_ready` 只能作为风险稿人工复核，不得标定稿。
- 若 `consume_kb_manager_context.py` 返回 `handoff_api_contract.status=missing/invalid`，只能继续人工复核或风险 dry-run，不能声称 API handoff 已验证。
- `context_degraded=false` 才能生成正式 dry-run / 正式正文链路；`context_degraded=true` 时，dry-run 只能输出状态修复任务单或风险骨架，并把 `state_freshness` 缺口写入控制卡。
- dry-run harness 不替代 `novel-volume-folder`、`novel-style-deai`、`novel-chapter-reviewer`，正式正文仍按章节闭环执行。
- 正式正文闭环中，若 NLCL 已同步 `selected_ops_rules.md`，总控必须把它传给写作、审稿和去 AI 三段；命中 `ops-role-separated-write-review-polish` 时，不能用同一份“自评 PASS”替代分角色证据。
- 命中 `ops-execution-pack-before-draft` 时，总控必须要求控制卡产出 `execution_pack`，并把正文阶段输入压到执行包、项目真值、上一章状态和必要库条目；不要把长细纲、长 skill、长素材和正文任务一次性交给写作模型。
- 命中 `ops-head-three-chapters-20k-ai-flavor-gate` 或当前章属于头三章/黄金三章/前两万字时，总控把去 AI 与审稿提升到 stricter gate：模型自评、检测器 PASS、route_health=ready 都不能替代冷读顺读。
- 命中 `ops-prompt-list-not-dead-command` 时，把外部提示词、网站模板和润色清单压成当前任务 3-5 个验收目标，不把长提示词全文或几十条死命令交给正文模型。
- 命中 `ops-first-chapter-scope-lock-before-plot-extraction` 时，先要求 `novel-chapter-brief` 输出 `chapter_scope_lock`，把当前章可写 beats、future_seed_queue 和 do_not_consume_yet 分开；不得把后续剧情点直接交给正文阶段。
- 命中 `ops-first-chapter-problem-not-prompt` 时，首章/黄金三章失败先回查标题承诺、前 500-800 字局面、主角目标、核心变量信号和冲突改价；不要只换提示词或套“黄金开篇生成器”。
- 命中 `ops-emotion-injection-becomes-explanatory-narration` 时，补情绪先补目标、误判、旧伤、关系压力、遮掩动作和选择代价；若新增内容只是解释性旁白或心理总结，返 `L4_DRAFT_EXECUTION`。
- 命中 `ops-fanqie-benchmark-reader-not-low-logic-shell` 时，番茄扫榜只提取目标读者、点击承诺、脑洞新鲜点和前 800 字兑现方式；不得复制低逻辑外壳、粗糙表达、榜书声纹或换皮设定。
- 命中 `ops-volume-outline-routing-lock` 时，先确认 `volume_id`、`volume_outline_source`、当前卷章节范围和防跨卷消费；若错读卷纲，返工从 `novel-volume-outline`、`novel-chapter-outline` 或 `novel-chapter-brief` 开始，不进入正文硬写。
- 命中 `ops-low-reads-check-packaging-first` 时，先走书名/封面文案/标签/简介/首章前 500 字包装急救，再判断是否需要正文级返工。
- 命中 `ops-direct-draft-smooth-is-floor-not-ceiling` 时，AI 直出稿只可标 `draft_done/risk_draft`，必须继续过控制卡、去 AI、审稿和回填，不能因“读通顺”直接定稿。
- 正式正文闭环后必须做 selected card usage closure：对照 NLCL `selected_distillation_entries.md`、`chapter_call_list` 和正文/审稿证据，把关键卡标成 `used / unused / missed`。若卡被选中但正文没有可见读者效果、场景机制或 validator 响应，不能记 `success`；先判断是 `L3_DISTILLATION_RETRIEVAL` 错召、`L4_DRAFT_EXECUTION` 没执行，还是 `L5_DEAI_STYLE` 把机制磨掉，再回填 `record-call-usage` 和 `distill_kb.py feedback`。
- dry-run 或正式链路若没有 `context_inclusion_trace`，总控不得把“上下文文件齐了”误当“上下文已消费”；至少要说清本章为什么带入这些 truth/state/lib/card、哪些被 suppress、哪些只是 validator_only。
- 命中 `ops-openwrite-rag-query-trace-budget-20260703`、微信群库或外部机制卡时，总控必须把 query/retrieved_ids/selected_suppressed_trace 压进 `context_inclusion_trace + activation_mode_audit + distractor_file_budget`，避免“检索很多、写作没用”。
- 若存在 `accepted_version_guard` 或 `chapter_version_graph`，总控不得让 dry-run、deAI、review 或 score improver 回退到旧版本自动稿；任何跨版本返工都要留下 checkpoint。

## 去 AI 与检测报告路由
用户提到 `朱雀`、`AI 检测`、`AI 率`、`人工特征`、`疑似 AI`、`AI 特征`、`未发现明显人工创作特征`、`去 AI`、`去AIGC`、`消痕`、`机器味`、`模板腔` 时，优先调用 `novel-style-deai`，并读取 `../../authenticity-deai-rules.md`。

路由规则：
- 用户提供真实样章、已发布章节、作者偏好或被采纳/拒绝的改稿时，先抽取作者指纹并回填 `文风规则.md` 或 `人物口头禅台词库.md`，再做去 AI。不要用通用 Human Style 覆盖作者自己的句法和情绪温度。
- 只有片段或单章正文：直接交 `novel-style-deai`；若用户给的是正文文件，可先运行 `novel-creation-suite/references/bundled-skills/novel-style-deai/scripts/zhuque_deai_probe.py` 做代理预检。
- 已处于章节闭环：先确认第一遍逻辑/人设/设定已通过，再进入 `novel-style-deai`；未通过时先交 `novel-score-improver` 修结构。
- 有朱雀截图或报告：记录 `人工特征 / 疑似 AI / AI 特征`，若人工特征为 0 或很低，要求 `novel-style-deai` 先用内隐角色工作台补人工特征，再删模板痕迹。
- 多章或全项目检测：抽样高风险章跑预检，按问题模式回填到 `01_项目定位/文风规则.md`、`06_写作规范/节奏控制.md`、`06_写作规范/对话描写规范.md` 或修改记录。
- 检测或审稿发现素材库反复带入机器腔时，回到对应素材 skill，按素材出库闸门补 `适用场景 / 功能 / 可变槽 / 禁用条件 / 人味来源 / 复用记录`，不要只在正文末端反复擦痕。
- 外部提示词库如自然写作 prompt：只吸收可转化为小说质量的机制，不复制“隐藏 thinking 代码块”、随机跑题、故意错字或泛化口语化要求。
- 消痕或去 AI 后记录字数漂移。若 `deai_length_delta` 超过约 15% 或超出目标平台/章节预算，先让 `novel-style-deai` 删弱解释、重复心理、装饰性环境和报告腔；不要把润色膨胀误当质量提升，也不要为了压字数删章节使命。

总控输出时注明已调用 `novel-style-deai`、是否运行预检、主要人工特征缺口、已回填的规则文件和残余风险。不得承诺“包过检测”。

## 压力测试模式
当用户要求测试、压测、随机造书、验证整套 skill 或“跑一遍流程”时，执行最小完整闭环：
1. 随机或半随机生成一本测试书，明确平台模式、题材、目标字数和读者承诺。
2. 创建完整 `01-09` 文件，并填入足够支撑写作的核心内容；不要只留下空占位。
   - `init_novel_project.py` 只负责创建 scaffold 和占位文件；它输出 `Mode: scaffold_only` 时，不得把项目当作已生成真值。压力测试必须继续填实核心链路文件，或把占位文件作为 P1 缺口写入报告。
3. 生成第 1 章控制卡，写初稿，做去模板化，运行章节审稿，低分则定向重写。
4. 定稿后回填人物状态、伏笔、正文总览、追读、进度和修改记录。
5. 输出压力测试报告：通过项、低分项、断点、暴露出的技能缺口、已反向修复的 skill。

压力测试项目是验证产物，不替代用户正式项目。若要保留样例，放在独立测试目录，并标明模拟数据与真实读者数据的区别。

压力测试报告必须按 `pressure-test-report-template.md` 输出，至少记录测试目标、测试书规格、产物清单、闭环分数、`missingObligations`、`repairability`、暴露缺口、反向修复和复验结果。报告不是为了证明流程完美，而是为了找到 skill 仍然不够硬的地方。

## 真值层与辅助层
已定稿正文和 `01-09` Markdown 是真值层。实体索引、关系图谱、章节摘要、运行日志、读者热区和 JSON/YAML 快照是辅助层，只用于查询、影响矩阵、遗忘提醒和复盘；冲突时回到真值层裁决。每次跨文件任务先标记上下文状态 `healthy/degraded/blocked`，状态不健康时先修状态或输出安全范围，不把猜测写成定稿。

## 文件路由
先用 `manifest.md` 定位文件对应 skill。每个用户给出的项目文件都有专属 skill；第一卷和第二卷的固定目录保留兼容入口，第三卷及以后统一交 `novel-volume-folder`。

## 动态联动
- 改定位：同步读者画像、卖点、简介、文风、黄金三章、爽点节奏、追读设计。
- 改书名/简介/标签/推荐位文案：同步核心卖点、目标读者、黄金三章、追读设计、章节校验清单和可能受影响的已写前三章。
- 改世界规则：同步力量、金手指、物品、地点、势力、人物能力边界、剧情冲突。
- 改人物：同步弧光、动机、关系、状态、口头禅、主线支线、名场面和正文。
- 改剧情：同步暗线、伏笔、冲突升级、分卷、章节、情绪、结局和运营。
- 改正文：同步人物状态、伏笔、时间线、正文总览、进度、修改记录、追读和 EPUB 导出文件。
- 改运营/反馈：同步后续大纲、爽点节奏、毒点避雷、更新计划和复盘。

## 多层验证
每次生成、更新或重写，都检查自身完整、上游一致、下游不破坏、读者价值和平台风险。任一层失败，先按 `layer-traceability-protocol.md` 标注 Layer ID、失败签名和 `rewrite_from`，再从对应层返工；不要用下游补丁硬盖上游设定错误。

## 章节闭环
1. 按 `chapter-state-machine.md` 判定当前章状态；缺卡或上游大改时标 `control_card_pending`。
2. `novel-chapter-brief` 生成章节控制卡，并保存到 `05_正文内容/第N卷文件夹/章纲/第XXX章_控制卡.md`，验收后标 `control_card_ready`。
3. 写前按动态上下文协议强制重读关键文件，并建立 12 维事实快照、相关性提取、上下文健康标记和滚动 5 章窗口。
4. `novel-volume-folder` 或卷包装 skill 写正文，状态依次推进 `drafting -> draft_done -> deai_done -> review_pending`。
5. 默认正文热路径只做最低可写性预检、正文生成、接收闸门和最多一次局部修复；状态快照、角色资源、伏笔账本、图谱索引和质量资产走异步或批处理回灌。
5.1 若控制卡有 `execution_pack`，正文优先按执行包逐节点展开；没有执行包且上下文很大时，先回 `novel-chapter-brief` 拆包，避免写作阶段同时处理全量资料和正文生成。
5.1.1 若已生成 `block_context_bundle`，正文默认只消费当前 500-800 字节点 bundle，不跨节点扩读整库；bundle 外的文件若想进入正文，必须先更新 `context_inclusion_trace` 与 `distractor_file_budget`。
5.1.2 若存在 `activation_mode_audit`，`validator_only / quarantine_only` 条目只能进检查链，不得直接进正文或事实写回；`always_on` 条目必须有常驻理由和预算。
5.2 多卷项目写第 N 卷时，正文前必须核对 `volumeOutlineLock` 或等价字段；发现当前章调用了错误卷纲、全书大纲替代当前卷纲、或提前消费下一卷核心事件时，标 `wrong_volume_outline_loaded`，回控制卡/分卷大纲层修。
6. 长章或高风险章先按 500-800 字节点或 2000-3000 字段落分段写作，禁止跨节点或跨下一章。默认长章目标 2000-2500 字，除非用户指定其他字数或短篇模式。
7. 第一遍只修逻辑、情节、人设和设定；第二遍再交 `novel-style-deai` 做语言、节奏和去模板化。若有朱雀/AI 检测报告或人工特征过低，使用 `novel-style-deai` 的 strict 反向模式。
7.0 若存在 `delegated_edit_checkpoint_gate`，写作、去 AI、审稿和人工改稿必须各守 edit scope；跨 scope 的动作没有 checkpoint artifact 时，章节状态不得升级。
7.1 初稿接收时检查可修空间：若章节短到只剩梗概、结果跳转、酷感短句和说明书面板，先回 `novel-volume-folder` 或 `novel-score-improver` 补现场、阻力、错误尝试、关系压力和余波；不要直接进入去 AI。
8. `novel-chapter-reviewer` 评分，必要时执行场景级/章节级/批次级审稿、剧情-正文对齐、AI 工具七维审稿、流水账四维雷达和专项雷达。
9. `<80` 必须 `novel-score-improver` 重写；80-84 大修；85-89 局部修；90+ 且作者无阻断反馈才可进入 `approved`。
10. 作者反馈高于模型评分；用户指出“不对、别扭、不匹配、不该知道、不该这样问、开头太长、爽点不对”等问题时，把章节降级为 `revision_needed`，转统一问题卡，再按 `patching/scene_rewrite/chapter_rewrite/upstream_replan` 修复。
11. 定稿后按写后回填协议先抽取 facts_delta，再压缩进 snapshot，更新状态、伏笔、总览、进度、修改记录、追读、图谱/索引、角色日志、场景笔记和质量资产；若已启用 `append_only_timeline / story_tracker_delta / readonly_bible_gate`，此时才允许把变化以 append-only delta 写回，而不是静默覆盖旧真值；随后运行 `python novel-creation-suite/references/bundled-skills/novel-suite-orchestrator/scripts/export_epub.py "<项目根目录>"` 同步刷新 `10_导出文件/` EPUB，写回和导出完成后才标 `writeback_done/next_ready`。

章节产物若用于复盘或压测，保留版本链：控制卡、初稿、去模板化稿、审稿报告、定稿、回填记录。用户只要正文时不展示全部版本，但内部仍要把分数、问题卡和采纳原因写入修改记录。

## 事实裁决
已定稿正文优先于旧大纲；最近人工修改优先于旧状态表；项目硬规则优先于素材库。发现正文和大纲冲突时，不默认正文错，要先判断是否旧大纲未同步。

## 定期复盘
每 10 章、每卷结束、追读波动或大纲大改时，检查世界观 bug、人物 OOC、伏笔过期、主线漂移、爽点真空、遗忘角色/支线、平台风险和更新稳定性。复盘后用 `novel-project-reviewer` 打分，必要时启用读者面板和双 persona 审稿；低分交 `novel-score-improver`，过修风险则按停止条件收束。

## 输出要求
跨文件任务输出：执行模式、目标文件、调用技能、上下文状态、章节状态、已读上游、已查下游、脚本体检结果、改动摘要、冲突处理、审稿评分、需回填文件、下一步。若任务来自 NLCL，还要显式交代 `context_inclusion_trace / activation_mode_audit / distractor_file_budget / block_context_bundle / delegated_edit_checkpoint_gate / accepted_version_guard / story_tracker_delta` 的消费情况。复杂审稿或返工要输出统一问题卡，便于 `novel-score-improver` 精准接手。
