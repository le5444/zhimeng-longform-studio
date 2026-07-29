---
name: novel-creation-suite
description: "长篇小说、网文和系列小说的全链路创作套件；当需要创建或维护小说项目文件夹、市场定位、世界观、人物、剧情大纲、正文章节、写作规范、运营数据、素材库、完本复盘、去AI化、朱雀/AI检测报告反向修稿、人工特征补强、审稿评分、低分重写、动态上下文、跨文件联动、下载番茄对标书并蒸馏入库时使用。用户常见触发语：写第X章、帮我写小说、继续写、续写、接着写、写下一章、开新书立项、搭大纲/人设/世界观、这章帮我审一下、去AI味/降AI率、按朱雀报告改稿、这章评分/重写。"
---

# 小说创作全链路套件

## 全局硬规则
- `01-09` 真值文件、已定稿正文和用户明确指令优先于蒸馏卡、提示词模板、图谱、AI 结果和旧摘要。
- 每次生成、修改或审稿前先建上下文健康标记；`blocked/degraded` 时只交修复范围或带风险草稿，不交定稿。
- 蒸馏只学机制，不学来源专名、设定壳、名场面结构、标志句和作者声纹；写作前先检索，禁止整库塞上下文。
- 若任务来自 `novel-kb-manager` / NLCL，写作层先走 `references/writer-entrypoint.md` 的交接预检：优先读取 `assemble-context --out` 生成的完整 context pack JSON，而不是长 stdout；读取 `chapter_call_list`、`route_health`、`context_health`、`state_freshness`、`selected_ops_rules` 和 `selected_distillation_entries`，并确认 `handoff_api_contract` 与 spec 成功标准；缺契约、缺成功标准或 `context_degraded=true` 时，不得把正文标为定稿。
- 若 NLCL 提供 `operation_package_like`、`operation_package_audit`、`style_retrieval_trace` 或 `mandatory_usage_closure`，写作层必须先读并消费：执行包未通过不得正式起稿；文风卡 selected 但无正文/审稿证据不得记 `used`；mandatory 项缺 `used/unused/missed/suppressed_for_project_truth` 闭环时不得把路线标为 `validated`。正式章 closure 还必须用同一 `run_id` 和 `context_pack_sha256 / operation_package_sha256 / prose_sha256` 绑定本轮证据；旧 call_usage 或跨轮报告不能替代。
- 若 NLCL 同步给出 `spec_success_audit`、`operation_package_audit`、`route_evidence_audit` 或其他带 `failure_signatures` 的报告，写作层必须先按 `references/nlcl-failure-signature-bridge.md` 消费这些签名；未解决的 `L1/L2/L3/L4/L6` 签名，不能靠去 AI、顺读感或审稿话术洗成定稿。
- 正式长章、2000 字以上正文、男频/修仙/玄幻/系统流章节、或任何要声称 `approved/finalized/writeback_done/validated` 的任务，起稿前必须先过 `references/prewrite-p0-kill-gate.md`；P0 未过只能输出风险稿、修复任务单或回上游，不能交定稿正文。
- 正式章节写作、审稿、去 AI 或写回后，若要声称 `approved/finalized/writeback_done/validated`，必须生成或等价记录 `CHxxx_writer_result_envelope.json`，并通过 `scripts/validate_writer_result_envelope.py`；总信封必须指向审稿接收、写回 manifest、正文纯净门、AI 味顺读门和 `fatigue_pattern_density_probe`，防止散落产物被口头包装成闭环。
- 章节、项目和正文返工必须带 `Layer ID`、失败签名和 `rewrite_from`；上游错误回上游，不用下游润色硬盖。
- 正文和项目文件必须对齐章节使命、信息边界、人物状态、伏笔、章末钩子、平台承诺和写后回填。
- 作者样章和已采纳改稿优先用于校准本书声音，不用通用去 AI 规则抹平作者指纹。
- 章节字数有专用权威链：用户本轮明确要求或本章控制卡 > `04_剧情大纲/章节细纲.md` 的单章目标 > `04_剧情大纲/分卷大纲.md` 的卷级预算 > 项目/平台通用区间 > writer fallback default。正文 skill 只执行目标字数，不自行发明新数字；若用户说“一章 2000 多字”，不得被默认 2000-2500 或 2500-3500 覆盖。

## 任务
把整本小说当作持续演化的创作工程：立项、建目录、生成 01-09 全套项目文件、写正文、去模板化、审稿评分、低分返工、写后回填和定期复盘。

## 使用顺序
1. 读 `references/manifest.md`，定位目标文件或流程。
2. 读 `references/core-writing-principles.md`，确认爽点、毒点和平台取舍。
3. 跨文件任务读 `references/context-assembly-protocol.md`，先建全项目索引，再按关联度读取上游、同区和下游。
4. 新书立项读 `references/bundled-skills/novel-project-intake/SKILL.md`。
5. 多文件任务读 `references/bundled-skills/novel-suite-orchestrator/SKILL.md`；若用户要下载小说、下载对标书、下载并蒸馏或多本书自动入库，先交给 `$tomato-novel-auto-distill` 准备源包，再交给 `$novel-distillation` 蒸馏入库。
6. 若输入包含 `novel-kb-manager` 的 context pack、项目 `.kb`、`route_health`、`chapter_call_list`、`spec_success_scaffold` 或 `selected_ops_rules`，先读 `references/writer-entrypoint.md`，正式交接优先要求 NLCL 使用 `assemble-context --out "<context_pack.json>"`，必要时运行 `consume_kb_manager_context.py` 读取该 JSON，把 NLCL 输出压成 creation-suite 可消费 handoff；`handoff_api_contract` 缺失/无效时只能人工复核或风险 dry-run，不能声称 API 链路已闭环。
7. 单文件任务读 manifest 对应的专属子 skill。
8. 章节正文固定走：`novel-chapter-brief` -> `novel-volume-folder` 或卷包装 skill -> `novel-style-deai` -> `novel-chapter-reviewer` -> 必要时 `novel-score-improver`；若用户提供朱雀/AI 检测、AI 率、人工特征低或疑似 AI 段落，`novel-style-deai` 使用反向诊断和人工特征补强流程；第三卷及以后、任意新增卷统一走 `novel-volume-folder`，不限制卷数。
9. 项目文件阶段性完成后走 `novel-project-reviewer`；低分交给 `novel-score-improver`。

## 全局依赖链
- `01_项目定位` 决定读者承诺、平台模式、卖点、毒点、文风和术语。
- `02_世界观设定` 决定可写长度、规则边界、冲突资源和社会可信度。
- `03_人物设定` 决定读者是否在乎角色，以及正文是否 OOC。
- `04_剧情大纲` 决定章节推进、爽点节奏、情绪波、伏笔回收和结局闭环。
- `05_正文内容` 消耗并反向更新前四类设定。
- `06_写作规范` 约束正文、对话、节奏、章末钩子和章节验收。
- `07_运营数据` 约束更新、追读、平台敏感词和反馈调整。
- `08_素材库` 给正文提供可复用描写、梗、资料、爽点和卡文急救。
- `09_完本复盘` 反哺下一本书、续作和素材复用。

## 牵一发而动全身
每次写或改一个文件，都必须检查它的上游事实、同区咬合和下游影响。若只是局部任务，仍先建立全项目索引；若发现冲突、主角能力/关系/世界规则/主线结局/核心卖点变化，升级到全项目扫描。

事实裁决：已定稿正文 > 最近人工修改 > 项目硬规则 > 状态表 > 章节细纲 > 早期大纲 > 素材库。章节字数裁决单独遵守上方字数权威链，不用一般事实优先级削弱章节细纲和本轮用户要求。

## 审稿闭环
- 项目级文件生成后，用 `novel-project-reviewer` 按 100 分制审稿。
- 章节完成后，先用 `novel-style-deai` 做真实感和去模板化，再用 `novel-chapter-reviewer` 审稿。
- 朱雀/AI 检测报告、人工特征为 0、疑似 AI 或机器味集中时，交 `novel-style-deai` 的 strict 反向模式；先补角色偏见、现场摩擦、具体锚点和不均匀节奏，再删模板痕迹。
- 低于 80 分必须重写；80-84 大修；85-89 局部修；90+ 才算稳定可用。
- `novel-score-improver` 先修结构，再修体验，最后修表达。连续 5 轮仍不升分时，停止硬改，回到上游设定或让用户裁决。

## 压力测试
当用户要求随机造书、验证整套技能、跑完整闭环或反向修 skill 时，用 `novel-suite-orchestrator` 的压力测试模式：创建完整 01-09 测试项目，抽样跑第 1 章或高风险章的控制卡 -> 初稿 -> 去模板化 -> 审稿 -> 重写 -> 回填，并把暴露缺口写回相关 skill。

## 共享资料
- `references/auto-workflow-usage-guide.md`：总控入口、自动路由和常用一键流程说明。
- `references/context-assembly-protocol.md`：动态上下文、全项目索引、冲突升级。
- `references/platform-retention-rules.md`：番茄、起点等平台留存与节奏取舍。
- `references/packaging-title-synopsis-rules.md`：爆款书名、简介、标签、推荐位文案和黄金三章兑现。
- `references/workbench-distillation-workflow.md`：写作台式上下文、蒸馏机制、分段写作和两遍润色。
- `$tomato-novel-auto-distill`：下载番茄小说、解 EPUB、生成蒸馏源包，并串联 `$novel-distillation` 入库。
- `references/observable-pipeline-and-event-ledger.md`：可观测章节流水线、T0/T1/T2/T3 上下文预算、章后事件流、因果边和叙事债务。
- `references/writer-entrypoint.md`：writer 视角的最小接入路径，串接控制卡、上下文、审稿与写回。
- `references/prewrite-p0-kill-gate.md`：正式起稿前的 P0 熔断门；检查真值、控制卡成功标准、执行包、检索预算、男频合同、字数承重、去 AI 范围、审稿验收与写后闭环。
- `references/nlcl-failure-signature-bridge.md`：把 NLCL / `novel-kb-manager` 的 `failure_signatures` 映射到 `L1-L7` 层级、审稿、去 AI 与返工热路径；遇到“到底该回控制卡、正文、去 AI 还是审稿门”时先看。
- `references/writer-entrypoint-walkthrough.md`：按 entrypoint 5 步走完一次的最小演练，每步附带可校验产物。
- `references/external-workflow-absorption-protocol.md`：外部工作流战略吸收边界，确保只吸收机制，不替代本套件。
- `references/layer-traceability-protocol.md`：审稿层级溯源、Layer ID 和精确返工起点。
- `references/pressure-test-report-template.md`：随机造书和完整闭环压测的报告模板。
- `references/chapter-control-card.md`：每章正文前的执行合同。
- `references/chapter-state-machine.md`：长篇章节状态、作者反馈降级、返工路由和控制卡落文件。
- `references/authenticity-deai-rules.md`：去模板化、真实感、人味和声音保护。
- `references/material-output-gate.md`：环境、战斗、心理、段子、爽点、台词和备用片段进入正文前的素材出库协议。
- `references/material-gate-result-schema.json`：素材出库审稿结果的机器可读 schema，供章节审稿、压力测试和低分返工共用。
- `references/validation-and-review-gates.md`：多层验证、项目/章节评分和返工阈值。
- `references/writeback-and-dynamic-state.md`：章节和项目文件写后回填。
- `references/writeback-state-schema.json`：`.writeback_state.json` 的机器可读 schema，约束 hop 状态与恢复元数据。
- `references/review-writeback-artifact-contract.md`：`review_acceptance.json` 与 `writeback_manifest.json` 的机器可读交接契约。
- `references/review-acceptance-schema.json`：章节审稿接收产物 schema，约束 hard/soft 双轨、可读性封顶、issue cards 和 accepted version。
- `references/writeback-manifest-schema.json`：写后回填 manifest schema，约束 facts_delta、event_ledger、state_updates、NLCL 同步和蒸馏反馈。
- `references/absorbed-mechanisms.yaml`：结构化外部机制注册表、`mechanism_id` 和落地文件索引。
- `references/source-integration-ledger.md`：本次融合的外部资料台账。
- `references/consistency-audit-report.md`：79 个 skill 的一致性巡检结果。
- `references/bundled-skills/`：78 个专属内部 skills。
