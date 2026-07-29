# Zhimeng Contracts

本目录保存前后端、Agent、Skill Adapter 和桌面壳之间的稳定 JSON 合同。

运行基础合同（始建于 Phase 1）：

- `health.schema.json`
- `runtime-status.schema.json`
- `run-event.schema.json`
- `error-envelope.schema.json`
- `skill-manifest.schema.json`

只读 Skill Registry 合同：

- `skill-registry.schema.json`：显式 trusted roots、每个 canonical source
  实例、manifest 指纹/版本状态、安全诊断和有界扫描 inventory。

`skill-registry` 与 `skill-manifest` 不是同一个概念：前者描述实际发现的
`SKILL.md` 来源且绝不执行；后者是受管 Adapter 的产品能力声明。Registry 保留跨根同名
实例和显式 root priority，不按 name 合并。`manifest_fingerprint` 只证明读取了该
`SKILL.md`，`package_fingerprint_status=not_scanned` 明确表示未递归哈希语料和代码包。
该合同由 `services/writing-runtime/scripts/export_skill_contracts.py` 从 Pydantic
模型生成，并由合同漂移测试校验。

字段语义、四套真实 Skills 的只读收据、测试结果和未实现边界见
`../../docs/PHASE3_SKILL_REGISTRY_IMPLEMENTATION.md`。

M3a-M3l 受管确定性 Skill 与生命周期合同：

- `managed-skill-catalog.schema.json`：项目可见的受管版本、manifest 与动态 package fingerprint。
- `managed-skill-lifecycle-catalog.schema.json`：应用/项目 scope 可见版本、来源、Adapter、合同/隔离
  validation 与 configured/effective activation。
- `managed-skill-import-request.schema.json` / `managed-skill-import.schema.json`：显式来源、可选预期
  package fingerprint、内容寻址导入结果与不可变导入收据。
- `managed-skill-activation-request.schema.json` / `managed-skill-activation.schema.json`：带
  revision/version CAS 的 enable/disable/upgrade/rollback/inherit 请求、有效状态和决议收据。
- `deterministic-skill-output.schema.json`：领域结果和声明 Artifact 的通用进程 envelope。
- `skill-execution-receipt.schema.json`：项目/Run/ToolCall、版本、权限、输入输出 hash、执行/领域
  状态、退出码、Artifact 和错误。
- `creation-project-audit-request.schema.json` / `creation-project-audit-result.schema.json`：
  Creation Suite 项目审计领域合同。
- `creation-prewrite-gate-request.schema.json` / `creation-prewrite-gate-result.schema.json`：绑定
  Operation Package 与前序 audit execution receipt 的第二段写前检查合同。

当前 `skill-manifest.v1` 只实现 adapted managed deterministic 子集；后续加入 Instruction/Workflow
时必须扩展同一权威模型并做兼容审查。Manifest 的 input/output URI 由本地安全合同注册器读取，
导入时保存两份合同与 bundle fingerprint，重启装载时复核；不允许外部 `$ref` 或网络 fallback。
project-audit、prewrite-gate、creation-handoff 和 draft-gate 可执行，不代表四套 Skills 连续链已完成。
实现与限制见 `../../docs/M3A_MANAGED_CREATION_SKILL_IMPLEMENTATION.md`、
`../../docs/M3E_PREWRITE_GATE_CHAIN_IMPLEMENTATION.md` 和
`../../docs/M3L_MANAGED_SKILL_LIFECYCLE_IMPLEMENTATION.md`。

只读项目数据面合同：

- `project-bindings.schema.json`：Runtime 中已绑定项目及持久 revision。
- `project-inspection.schema.json`：01-09、卷章树、稳定文件 ID 与内容哈希快照。
- `project-file-content.schema.json`：按稳定文件 ID 读取的 UTF-8 文本与 ETag。

三份 Project 合同由
`services/writing-runtime/scripts/export_project_contracts.py` 从 Pydantic 模型生成；
合同漂移测试会拒绝只改后端模型、不重新导出的提交。

Runtime-owned ChangeSet 与写回合同：

- `change-set-create.schema.json`：绑定 base revision/fingerprint 的 create/update/delete 候选输入。
- `change-set.schema.json`：文件改动、稳定 hunk、决策、状态和写回/回滚 manifest 的完整记录。
- `change-set-list.schema.json`：项目内 ChangeSet 摘要队列。
- `change-set-decisions.schema.json`：带 `expected_review_revision` 的逐 hunk 决策更新。
- `change-set-apply.schema.json`：带审阅版本前置条件的应用请求。
- `change-set-rollback.schema.json`：带审阅版本前置条件的回滚请求。
- `writeback-manifest.schema.json`：base/result revision、文件结果、备份引用和新建目录收据。
- `writeback-evidence-response.schema.json`：Apply/Rollback 后的产品原生写回证据收据与历史。
- `nlcl-writeback-sync-request.schema.json`：作者触发的 NLCL 写回证据同步请求（绑定 evidence receipt hash）。
- 
lcl-writeback-sync-response.schema.json：产品原生证据连续性同步收据；固定 original_nlcl_chapter_ops_usage_synced=false。
- `nlcl-call-usage-continuity-request.schema.json`：作者触发的产品原生 call_usage 连续性请求（绑定 writeback sync receipt hash）。
- `nlcl-call-usage-continuity-response.schema.json`：产品原生 selected-card call_usage 连续性收据；固定 original_nlcl_call_usage_recorded=false。

这些合同由 `services/writing-runtime/scripts/export_change_set_contracts.py` 从 Pydantic
模型生成，并由合同漂移与负例测试校验。公共创建接口只接受 `origin=manual`；
`origin=agent` 只能由 Runtime 内部 Tool Executor 产生，并以 `source_run_id`、
`source_tool_call_id`、`source_request_hash` 和独立 provenance 记录绑定真实 Run、Context Pack、
ToolCall 与项目 revision/fingerprint。`skill_workflow` 的受管执行与引用完整性仍由 M3 收口，合同
中的预留字段不代表该链路已经接线。

Source import 合同：

- `tomato-search-request.schema.json` / `tomato-search.schema.json`：书名查询、唯一自动选择、
  同名人工选择和未找到结果。
- `tomato-resolve-request.schema.json` / `tomato-resolve.schema.json`：显式 `book_id` 或 URL 解析入口，返回可直接 create 的候选；不替代真实外部下载。
- `source-import-create.schema.json` / `source-import.schema.json`：保留 Tomato v1 创建与单任务合同。
- `local-source-inspect-request.schema.json` / `local-source-inspect.schema.json`：受信根内 TXT/EPUB
  检查请求与不可变来源候选。
- `local-source-import-create.schema.json` / `local-source-import.schema.json`：本地 TXT/EPUB 创建与
  单任务合同。
- `local-source-import-execution-receipt.schema.json`：本地来源、Runtime 快照和提取文本的独立
  hash/size、工具指纹与蒸馏草稿收据。
- `source-import-list.schema.json`：Tomato/`local_file` 联合持久 job、source packet、draft artifacts、
  进度、取消、重试与中断状态。
- `source-import-execution-receipt.schema.json` / `source-import-execution-receipt-record.schema.json`：
  request、run/revision/idempotency、工具指纹、source/draft artifacts 与 verified/legacy 状态。

固定 Tomato Adapter 与受管本地 TXT/EPUB Adapter 都在 `awaiting_review` 交付 verified execution receipt；
本地来源另由 Runtime 保存可信根/稳定来源 hash、逐 attempt receipt 和失败清理真值。单张候选晋升
不会把整批误标完成。作者提交整批 decisions 后，Runtime formalization 事务才从 `awaiting_review`
原子推进到 `completed`；`formalizing` 保留为兼容中间态。Tomato 详细边界见
`../../docs/TOMATO_SOURCE_IMPORT_IMPLEMENTATION.md`。合同由
`services/writing-runtime/scripts/export_source_import_contracts.py` 导出。

原生知识复核与版本合同：

- `knowledge-candidate.schema.json` / `knowledge-candidate-list.schema.json`：带不可变 source origin、
  revision/content hash、问题、决定和 candidate-set hash 的 v2 候选。
- `knowledge-candidate-update.schema.json` / `knowledge-promotion.schema.json` /
  `knowledge-candidate-decision.schema.json`：候选修改、staged 晋升和拒绝条件。
- `knowledge-version-lineage-receipt.schema.json`：版本、候选 origin、作者批准内容、替换授权和
  activation CAS 基线。
- `knowledge-formalization-request.schema.json` / `knowledge-formalization-record.schema.json` /
  `knowledge-formalization-receipt.schema.json`：整批 decisions、finalize 前置 hash 和 immutable receipt。
- `knowledge-provenance-ref.schema.json`：Source -> Candidate -> Version -> Formalization 的跨层引用。
- `knowledge-card.schema.json` / `knowledge-card-list.schema.json`：active/disabled 卡、staged/verified/
  abandoned/legacy 版本和只统计 active verified 的全库 fingerprint。
- `knowledge-card-state.schema.json` / `knowledge-card-rollback.schema.json`：正式状态与版本切换的乐观并发条件。
- `project-kb-state-record.schema.json`：Runtime-owned 项目级 `.kb` v1 的
  `missing/current/stale/tampered` 状态、不可变 snapshot manifest 与原子 `current.json` 指针。
- `project-kb-rebuild-request.schema.json` / `project-kb-rebuild-result.schema.json`：绑定项目 revision/fingerprint、
  knowledge scope revision/fingerprint 的四项重建 CAS，以及内容寻址 snapshot 的复用结果。

合同由 `services/writing-runtime/scripts/export_knowledge_contracts.py` 从 Pydantic 模型确定性导出并由
漂移测试校验。当前 `work`、`voice`、`validator` 均有独立严格复核合同，可进入 staged promotion、
activation CAS 与整批 formalization；其他 unsupported 卡型仍保持 deferred。三类 active verified 卡可由
FormalKnowledgeSelector 以同一正式批次显式绑定到 Context/NLCL。Runtime-owned 项目级 `.kb` v1 已以
不可变 snapshots 和原子 `current.json` 落盘，是从小说项目真值与 Runtime SQLite 重建的派生状态；
Context 编译不能只相信该状态查询的标签：request 必须携带 current `project_kb_state_id` +
`project_kb_manifest_hash`，Runtime 在编译/幂等 replay 与后续运行边界重新校验磁盘 manifest。
这仍不表示全部卡类型、原始 NLCL 章节/ops/usage/writeback 语义、通用 Adapter、统一搜索/反馈或
四套 Skills 连续执行已完成。
实现与边界见 `../../docs/M3B_NATIVE_KNOWLEDGE_IMPLEMENTATION.md` 和
`../../docs/M3F_VERIFIED_SOURCE_LINEAGE_IMPLEMENTATION.md`。

只读蒸馏预览合同：

- `distillation-health.schema.json`：正式库结构健康、验证成熟度、卡片计数和库/工具指纹。
- `distillation-coverage-request.schema.json`：仅允许 `query/profile` 的固定覆盖请求。
- `distillation-coverage.schema.json`：source/formal coverage、writing gate、hard-rule/risk-ready
  compact cards 和显式风险/正式注入布尔值。

这些合同由 `services/writing-runtime/scripts/export_distillation_contracts.py` 导出。它们自身只描述
Health/Coverage Preview，单独存在不能证明 Context Pack、NLCL 装配或 Writer 注入；当前 M1
Context 事实由下节合同与 Runtime/Workbench 实现证明。详细 Adapter 边界见
`../../docs/DISTILLATION_COVERAGE_PREVIEW_IMPLEMENTATION.md`。

Context Compiler 合同：

- `context-pack-create.schema.json`：写作目标、profile、活动章节、预期项目 revision/fingerprint，以及
  必填的 current `project_kb_state_id` + `project_kb_manifest_hash`；不包含 Runtime 路径、数据库或
  Provider 参数。
- `context-pack.schema.json`：不可变 Context Pack，绑定 session/project/history ordinal、
  可空知识指纹、字符/token 双预算、included/excluded/Gate、实际 Provider messages 和
  `pack_hash` / `injection_hash` / `provider_messages_hash`，并保存编译时 `.kb` pair。
- `nlcl-route-receipt.schema.json`：Context-bound selected cards、下一调用、边界、route receipt hash 与
  Pack 完全相同的 `.kb` pair。
- `prewrite-operation-package.schema.json`：Runtime-owned 项目真值、证据、信息边界、输出合同与 readiness。
- `nlcl-route-execution-state.schema.json` / `nlcl-route-execution-receipt.schema.json`：三段受管调用的
  计划/运行状态与终态 receipt；终态 receipt 继承并锁定 Route 的 `.kb` pair。

`excluded` 只保留来源身份、version/source hash、fragment hash、估算和排除原因，不复制风险卡或
超预算正文。Pack 的公开判别字段名是 `schema`，不是 Python 属性名 `schema_id`。这些合同由
`services/writing-runtime/scripts/export_context_contracts.py` 以 UTF-8、alias 字段和确定性键顺序
导出，并由漂移与严格负例测试校验。Runtime 已使用该合同持久化 Pack、绑定单次 Run、注入 exact
Provider messages 并保存收据；Workbench 已手写对应 client，当前流程为“编译 -> 证据/Writer 模式审查
-> 明确确认”三阶段。M1 历史回归和浏览器同链 hash 证据见
`../../evidence/context-pack-validation-20260720.md`；当前 FormalKnowledgeSelector 已把同一正式化
`work/voice/validator` 批次的 provenance 接入 Context/NLCL/Prewrite；项目级 `.kb` v1 也已经作为
Runtime-owned 可重建派生状态发布。SQLite v20 保持 33 张表，只给 Pack/Route/Execution 三表增加两列
nullable pair；v19 历史 NULL 记录可由 response 合同读取，但不能执行、继续或 recovery adopt，也不允许
迁移伪造回填。Workbench 在 Context 审查面显示 `project_kb_state_id` 与 `project_kb_manifest_hash`，pair
缺失/不一致或磁盘状态非 current 时禁用“使用这批知识开始写作”和 Run 确认。合同与 UI 存在仍不表示原始 NLCL 章节/ops/usage/writeback 语义、
通用 Adapter、统一搜索/反馈、原始四 Skills 通用编排或完整写作链已完成。

M3i 固定 durable Workflow 合同：

- `workflow-plan.schema.json`：不可变、逐步 fingerprint 绑定的固定 Workflow Plan。
- `workflow-step-checkpoint.schema.json`：previous checkpoint CAS/hash 链及每步执行证据。
- `provider-io-boundary.schema.json`：Provider dispatch 前的 messages、tools、模型、配置指纹、
  executor 和 request hash 边界。

三份合同由 `services/writing-runtime/scripts/export_workflow_contracts.py` 从 Pydantic 模型确定性导出，
并由漂移和严格负例测试校验。M3i 历史纵切最初表达 `project-audit -> prewrite-gate`；当前 M3j
canonical Plan 已扩展为 `project-audit -> prewrite-gate -> creation-handoff`，仍使用 at-most-once
恢复语义。它不是四套 Skills 的通用 DAG、循环、审批或有副作用 exactly-once 执行器。实现与边界见
`../../docs/M3I_DURABLE_WORKFLOW_IMPLEMENTATION.md` 和
`../../docs/M3J_CREATION_HANDOFF_IMPLEMENTATION.md`。

M3k Agent 候选与 Writer instruction 合同：

- `agent-candidate-output.schema.json`：当前 `zhimeng.agent-candidate-output.v3` Envelope。除 Plan、最终
  checkpoint、Provider boundary/request、Context、Operation Package、项目 revision/fingerprint、handoff
  限制、内容、Runtime-owned references 与 Provider turn receipt chain 外，还绑定 WriterTask、instruction
  materialization、committed-before-dispatch receipt，以及 author 模式下唯一同 Run ChangeSet。
- `agent-candidate-output-v2.schema.json`：历史 v2 只读兼容合同；它保留 Provider identity receipt chain，
  但缺少当前 Writer/instruction dispatch 绑定，不能完成新 Run 或被 recovery adopt 为当前成功终态。
- `agent-candidate-output-v1.schema.json`：历史 v1 只读兼容合同；实际响应模型身份未验证且不得补造，
  同样不能再作为当前写入或恢复采用格式。
- `provider-turn-receipt.schema.json` / `provider-turn-receipt-list.schema.json`：每轮 requested/reported model、
  显式 alias snapshot、request/receipt hash、连续 ordinal 与整链 hash。
- `creation-instruction-activation.schema.json`：同 Run 当前有效的受管 Writer instruction scope、版本、
  activation receipt、Manifest/package/map identity 与 binding hash。
- `creation-instruction-materialization.schema.json`：绑定 WriterTask 和 activation 的有界指令块、精确
  Provider instruction bytes/hash、包含/排除轨迹及 materialization hash。
- `creation-instruction-dispatch-receipt.schema.json`：在网络调用前原子提交的 WriterTask、materialization、
  Provider boundary/request、messages/tools/config fingerprint 与 `automatic_provider_replay_count=0` 收据。
- `writer-task.schema.json`：Provider dispatch 前由 Runtime 派生的 Writer 模式、活动章节正文目标、
  三步 Workflow/Context/Route/Operation Package/knowledge 与项目 revision 绑定。
- `writer-candidate-result.schema.json`：Provider 后 v3 候选与 WriterTask、可选唯一同 Run ChangeSet、精确
  目标和作者审查要求的不可变结果绑定。

Provider 的 `AgentCandidateSubmission` 只有 `kind + content`；Provider 不能提交 references、Writer identity
或 ChangeSet identity。Runtime 自动构造这些绑定。只有最终 Skill 为 `creation-handoff` 的 managed Workflow
才广告 `candidate_output_submit`；提交必须是唯一、最后的 ToolCall 且不能与文本混发。当前 v3 候选固定
`formal_draft_allowed=false`、`requires_author_review=true`，必须绑定完整有序 Provider receipt chain，且
`rejected_mismatch` 不能进入成功候选。Candidate/receipt 合同由
`services/writing-runtime/scripts/export_agent_candidate_contracts.py` 确定性导出；三份 instruction 合同由
`services/writing-runtime/scripts/export_skill_contracts.py` 导出。

两份 Writer 合同由 `services/writing-runtime/scripts/export_writer_contracts.py` 确定性导出；它们固定
`formal_draft_allowed=false`、`writes_project_truth=false`，不会把 author-review ChangeSet 解释为已写入正文。
Run 创建请求的 `writer_mode` 默认 `repair_only`。该模式只允许
`WriterTask -> candidate_output_submit -> WriterCandidateResult`、`repair_plan` 且没有 ChangeSet。作者只有在
Workbench 明确选择 `author_review_change_proposal` 后，才允许
`WriterTask -> change_set_propose -> candidate_output_submit -> WriterCandidateResult`、`change_proposal` 和
恰好一个同 Run、完全相同正文目标的 ChangeSet。两种模式都要求作者审阅，永不直接写 project truth。

Runtime 通过 `GET /api/v1/agent/runs/{run_id}/writer-trace` 聚合返回 Workflow、WriterTask、instruction
activation/materialization/dispatch、Agent Candidate、WriterCandidateResult 与 recovery；单项
`/writer-task` 和 `/writer-candidate-result` GET 保留为只读接口。Workbench 的“使用这批知识开始写作”
CTA 只负责显式选择 author-review 模式并编译 Context，RunDrawer 对聚合 Trace 的每层 identity/hash
fail closed。上述记录复用既有 `artifact`/`run_event`/候选表，不表示 O2 或 Local v1 已完成。

Provider recovery standalone 合同：

- `provider-recovery-dossier.schema.json`：Provider boundary 后的不可变恢复材料，远端结果固定
  `unknown`，Provider replay 计数固定为 `0`，并绑定同一 instruction dispatch receipt identity/hash。
- `provider-recovery-decision-request.schema.json`：dossier revision/hash CAS 与二选一 action。
- `provider-recovery-decision.schema.json`：同时绑定前一 dossier hash 和 dispatch receipt 的单次
  adopted/abandoned 决议及 resulting Run status。
- `provider-recovery.schema.json`：GET/POST 返回的一份 dossier 加可空 terminal decision。

四份合同由 `services/writing-runtime/scripts/export_provider_recovery_contracts.py` 从 Pydantic 模型以
Draft 2020-12 确定性导出，并由漂移测试校验。派生 ID/hash、candidate state、dispatch identity、revision
单步推进和同 Run 持久证据绑定仍由 Pydantic/Store 强制；`adopt_persisted_candidate` 只接受仍有效的 v3
候选，并在同一决议事务补建或校验 WriterCandidateResult。JSON Schema 文件存在不表示可以自动推断
远端 Provider 结果，也不授权请求重放。

这些合同不包含 MyTemple 或其他外部知识应用的连接、进程、安装目录和私有数据路径。外部材料只有
经作者显式导入并复制为本项目受控、带 provenance/version/hash 的快照后，才能成为合同输入。

供应商中立 Agent 执行合同：

- `execution-session.schema.json`：握手完成后的 executor、版本和能力快照。
- `execution-run.schema.json`：一次目标执行的状态、项目 revision 和生命周期。
- `activity-item.schema.json`：不可变的细粒度执行活动，使用 `kind` 判别 payload。
- `execution-protocol.schema.json`：上述合同共享的 `$defs`，不作为 API 响应。

`ExecutionSession -> Run -> ActivityItem` 是领域协议。现有
`run-event.schema.json` 仍只是 SSE 传输外壳，可在 `data` 中承载活动项；两者不能合并。
活动消费者以 `activity_id` 去重，并按单个 run 内从 1 开始的 `sequence` 连续应用。
`causation_id` 只能指向同一 run 中已经出现的 Activity，所有 Activity 必须保持同一个
`correlation_id` 和 project revision；compaction 只能覆盖它自身之前的序列。
Project revision 与 Project Reader 使用同一含义：`revision` 是从 1 开始的持久整数，
`fingerprint` 是对应只读快照的 64 位 SHA-256。ChangeProposal 必须同时携带并匹配
`base_revision` 与 `base_fingerprint`，不能只靠其中一个判断提案是否陈旧。
Session 和 Run 中的 `capabilities` 是 adapter 握手后的协商快照，不表示 Runtime
已经实现相应功能。标准能力使用枚举，供应商能力进入 `x-*` extension，不能向核心对象
追加私有字段。错误活动和失败 Run 复用 `error-envelope.schema.json`，不另造错误结构。
JSON Schema 无法用标准关键字表达跨字段相等、集合成员和数值字段顺序，因此这些规则以
`x-zhimeng-invariants` 明示，并由 Runtime 的 Pydantic 模型强制执行。

Python Runtime 以 Pydantic 模型实现这些字段，前端类型后续从 OpenAPI/JSON Schema 生成。任何字段变更先修改合同并增加兼容性测试，不能只改单侧代码。

Agent Kernel Decision Harness 的实验合同：

- `kernel-harness-case.schema.json`（v2）：固定 fixture、同场步骤、step timeout 与 assertion/gate 定义。
- `kernel-harness-run.schema.json`（v2）：adapter/binary/schema/config 指纹、独立 Provider profile、游标、final snapshot 与最终断言。
- `kernel-harness-observation.schema.json`：有界观察项，只引用原始证据，不复制大段事件或正文。
- `kernel-harness-assertion-result.schema.json`（v2）：`pass/fail/blocked/not_measured`，后三者不能汇总为通过。
- `candidate-change-set.schema.json`：Kernel harness 使用的 base ProjectRevision、content-addressed patch 和稳定 hunk identity。
- `kernel-transport-probe.schema.json`（v2）：Kernel transport 身份、二进制/协议/配置指纹、能力声明/实测和逐项检查。
- `kernel-harness-cleanup.schema.json`（v3）：fixture、disposable project、进程、用户根、资源上限和 evidence secret scan 的清理收据。

transport/cleanup 收据分别使用 `kernel-transport-probe.v2` 和
`kernel-harness-cleanup.v3`，现在均有独立公开 Schema。这些 schema 用于比较候选内核，
不等于上面的 Runtime ChangeSet API，也不证明任一 Kernel 已生成、审阅或写回正式 ChangeSet。
它们由 `services/writing-runtime/scripts/export_kernel_harness_contracts.py` 生成。

OpenAI-compatible Provider capability 合同：

- `provider-capability-probe.schema.json`（v3）：当前 Provider/model 的 transport security、
  resource limits、models、JSON object 差分、named-function tool choice、SSE streaming 和
  明确未测 cancel 结果。
- `provider-capability-probe-v2.schema.json`：只读历史合同，仅用于解释已有 v2 收据，
  不能校验或冒充当前 v3 probe。

该合同不含 API key、Authorization header、请求/响应正文或模型文本，也不代表任一 Agent
Kernel、Skill、审批或写回能力。它由
`services/writing-runtime/scripts/export_provider_probe_contract.py` 生成。
