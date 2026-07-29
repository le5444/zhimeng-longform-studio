# Novel KB Manager Context Handoff

受管 thin adapter。它消费 O2-D 生成的 `distillation_index` 入口，把 `novel-kb-manager.import-distillation-index` 的受控结果转换成后续 `assemble-context` 的写前上下文意图。

边界：
- 不读取或执行用户级 `%USERPROFILE%\.codex\skills` 原始脚本。
- 不复制正式蒸馏卡全文，只使用 O2-D 入口里的 card id、version、content hash、route terms、tags 与安全摘要。
- 必须由 Runtime 提供项目 revision/fingerprint、O2-D handoff receipt/hash、managed Skill activation 与作者审批。
- `formal_draft_allowed=false`；本 adapter 不生成正文、不写项目真值，只产出 Context Pack intent 与 NLCL route receipt intent。
- `route_health=ready` 只表示可装配上下文，不等于 `validated` 写作闭环。
