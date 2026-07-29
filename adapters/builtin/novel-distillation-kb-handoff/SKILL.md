# Novel Distillation -> Novel KB Manager Handoff

受管 thin adapter。它把已经由作者正式化的 `novel-distillation` 三类知识卡（work / voice / validator）转换为 `novel-kb-manager` 可消费的 distillation_index 相邻交接产物。

边界：
- 不读取或执行用户级 `%USERPROFILE%\.codex\skills` 原始脚本。
- 不复制蒸馏卡全文，只输出 active 卡入口、路由词、标签、安全摘要与版本/hash。
- 必须由 Runtime 提供项目 revision/fingerprint、formalization receipt、managed Skill activation 与作者审批。
- `formal_draft_allowed=false`；本 adapter 不生成正文、不写项目真值。
