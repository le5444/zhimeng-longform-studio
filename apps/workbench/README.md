# @zhimeng/workbench

织梦 B 路线的 React/TypeScript 作者工作台。第一屏直接进入作品、正文与 Writing Agent 协作界面，不包含营销页。

## 当前范围

- 左侧主导航和由 Project API 返回的真实作品卷章树
- 中央 TipTap 只读正文快照及其真实字数
- 右侧 Agent、检索、资产、Context、Gate、Reader 检查器
- 右侧“资产”页可重建/查看领域 markdown 资产索引（人物/剧情/伏笔等），关系图与状态追踪仍未宣称完成
- 右侧真实 Runtime ChangeSet 队列、文件/Hunk 差异、逐 hunk 接受或拒绝、应用和回滚
- 右侧“资料”页的番茄书名搜索、同名选择、任务轮询、source packet 摘要、取消与重试
- 产品内知识候选修改/晋升/拒绝、整批 finalize、版本状态和 source/formalization receipt 展示
- 右侧 `Context` 页的真实蒸馏库健康、写作目标覆盖、hard-rule/risk-ready 风险预览
- Writing Agent 指令先编译 Context Pack，再由作者审查 included/excluded、Gate、字符/token 预算、
  exact Provider messages 与三项 hash；确认后才创建绑定 Run
- 底部 Timeline、Tools、Skills、Artifacts、Terminal 运行抽屉
- 项目级 Writing Agent 会话、增量消息、刷新恢复、取消和真实 Activity Timeline
- Agent 消息安全 Markdown；正文仍由 TipTap 管理，Diff/工具结果不混入 Markdown 正文
- 轮询 Runtime 状态，并通过 `/api/v1/projects*` 绑定、检查和读取项目
- 区分加载、未绑定、示例和错误状态；快照 revision/fingerprint 不一致时拒绝混读
- 900px 以下切换章节、正文和 Agent pane，避免窄屏多栏裁切

当前只读 Project Reader、ChangeSet 审阅面、Tomato source import 资料页、原生知识复核/正式化、蒸馏 Coverage Preview、Context Pack 编译、证据/Writer 模式审查、明确确认三阶段和 Tool-capable Writing Agent 会话已接线。资料页按书名创建任务，唯一精确结果自动继续，同名结果要求作者选择，并在 source packet/draft 生成期间轮询状态。`awaiting_review` 表示候选尚未正式入库；作者可在知识页修改、晋升、拒绝并整批 finalize，成功后才显示 verified 正式化收据并推进任务完成。没有待审 Pack 时 Context 页显示正式库 Health/Coverage 风险预览；提交 Agent 指令后先编译 Pack 并自动进入审查页，作者选择 Writer 模式并明确确认后才把同一 Pack 绑定 Run。详细边界见 `../../docs/TOMATO_SOURCE_IMPORT_IMPLEMENTATION.md`、`../../docs/M3F_VERIFIED_SOURCE_LINEAGE_IMPLEMENTATION.md`、`../../docs/DISTILLATION_COVERAGE_PREVIEW_IMPLEMENTATION.md`、`../../docs/AGENT_CONVERSATION_RUNTIME_IMPLEMENTATION.md` 和 `../../docs/M2_TOOL_CAPABLE_AGENT_IMPLEMENTATION.md`。

Workbench 可以读取已有 ChangeSet，更新 hunk 决策，并通过 Writing Runtime 应用或回滚。M2 的三个项目内工具负责列出稳定文件、读取有界正文和生成 Runtime 暂存 ChangeSet；M3 的固定 `skill_execute` 当前运行 Creation Suite `project-audit -> prewrite-gate -> creation-handoff` 三步链。RunDrawer 会从聚合 `/api/v1/agent/runs/{run_id}/writer-trace` 恢复 Workflow、WriterTask、受管 instruction activation/materialization/dispatch、Candidate v3、WriterCandidateResult、Provider recovery、Tool Call/Result、三段 Skill Receipt、产物和 ChangeSet 审阅跳转；任何 identity/hash 缺失或不匹配都不推定成功。历史 Candidate v1/v2 只作为 legacy read-only 展示，不能显示为新 Run 成功或可 recovery adopt。正文编辑器仍是只读快照；完整文学质量 Gate/Reader、正式起稿授权和四 Skills 通用连续执行尚未接线。Coverage Preview、固定三段 Skill 收据和 Tool-capable Agent 都不能冒充完整长篇质量闭环已经完成。

Workbench 不安装、启动、嵌入或连接 MyTemple 等外部知识应用。外部材料只有经作者显式导入并复制
成项目受控快照后才可显示或参与 Context；界面不接受外部应用 API、安装目录或私有数据目录作为
在线知识源。

公共 ChangeSet 创建只允许 `manual`。`origin=agent` 的候选由 Runtime 内部工具产生，并携带真实
`source_run_id`、`source_tool_call_id` 及数据库 provenance；Workbench 只展示该来源并跳到同一
canonical ChangeSet，不允许前端伪造 Agent 来源。Source -> Knowledge provenance 已由 M3f 收口，但
Skill 直接生成 ChangeSet 的来源链仍未接线。

## 启动

```powershell
cd C:\path\to\zhimeng-longform-studio\apps\workbench
npm install
npm run dev
```

Vite 会显示实际端口，默认从 `http://127.0.0.1:5173` 开始选择。

## Runtime 配置

前端默认使用同源地址。独立启动 FastAPI 时可设置：

```powershell
$env:VITE_RUNTIME_URL='http://127.0.0.1:7341'
$env:VITE_RUNTIME_TOKEN='local-session-token'
$env:VITE_PROJECT_ROOT='C:\path\to\novel-project'
npm run dev
```

Bearer token 只从启动环境注入，不写入源码或浏览器持久存储。

若设置 `VITE_PROJECT_ROOT`，Workbench 首次加载会调用
`POST /api/v1/projects/inspect`；否则使用 Runtime 最近的 binding。Runtime
必须通过 `ZHIMENG_TRUSTED_PROJECT_ROOTS` 显式信任该项目或其父目录。

## 验证

```powershell
npm run typecheck
npm test
npm run build
npm run lint
npm run test:e2e
```

默认 `npm run test:e2e` 是自包含验收：Playwright 使用仓库根 `.venv` 启动
隔离 Runtime（8789）、分时假 Provider（18790）、临时 SQLite、
`services/writing-runtime/tests/fixtures/minimal-novel` 可信根和固定 Vite（5176），
不依赖手工启动的 8787 Runtime。设置
`PLAYWRIGHT_BASE_URL` 时才改为连接外部 Workbench。
