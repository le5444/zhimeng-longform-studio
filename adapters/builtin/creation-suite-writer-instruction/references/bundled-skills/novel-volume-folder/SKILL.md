---
name: novel-volume-folder
description: "生成、修订和审稿 05_正文内容/第N卷文件夹/ 下任意卷数的章节正文；当写第一卷兼容目录、第二卷、第三卷以后或新增卷正文时使用。"
---

# 通用卷正文入口

## 全局硬规则
- `01-09` 真值文件、已定稿正文和用户明确指令优先于蒸馏卡、提示词模板、图谱、AI 结果和旧摘要。
- 每次生成、修改或审稿前先建上下文健康标记；`blocked/degraded` 时只交修复范围或带风险草稿，不交定稿。
- 蒸馏只学机制，不学来源专名、设定壳、名场面结构、标志句和作者声纹；写作前先检索，禁止整库塞上下文。
- 章节、项目和正文返工必须带 `Layer ID`、失败签名和 `rewrite_from`；上游错误回上游，不用下游润色硬盖。
- 正文和项目文件必须对齐章节使命、信息边界、人物状态、伏笔、章末钩子、平台承诺和写后回填。
- 生成、续写或修改任何正文 `.md` 后，默认同步运行总控脚本 `novel-suite-orchestrator/scripts/export_epub.py "<项目根目录>"`，把小说阅读版 EPUB 刷新到项目内 `10_导出文件/`；除非用户明确说“这次不用导出”。
- EPUB 只收正文 `第XXX章_标题.md`，默认排除 `章纲/`、控制卡、报告和项目设定文件；导出时必须清理空段、Markdown 围栏、反引号、列表符号、生产注记和无意义字符，系统面板/雷石等级等短提示转成干净的小说提示段。
- 作者样章和已采纳改稿优先用于校准本书声音，不用通用去 AI 规则抹平作者指纹。

## 必读
- `../../context-assembly-protocol.md`
- `../../chapter-control-card.md`
- `../../chapter-state-machine.md`
- `../../authenticity-deai-rules.md`
- `../../workbench-distillation-workflow.md`
- `../../validation-and-review-gates.md`
- `../../writeback-and-dynamic-state.md`
- 本章控制卡或 `novel-chapter-brief` 输出。
- 上一章正文、下一章计划、人物状态、伏笔表、爽点节奏表、文风规则、章节校验清单。
- 若本章由 `novel-kb-manager` / NLCL `assemble-context` 触发，必须先读取上下文包中的 `chapter_call_list_md`、`route_health`、`context_health`、`context_degraded`、`state_freshness`、`continuation_state`、项目库 `selected_ops_rules.md` 和 `selected_distillation_entries.md`；ops 规则、蒸馏入口或 project/domain/style/platform 库命中可修空间、短句过度、人工冷读、分角色闭环、规则代价、系统面板现实改价、信息揭露阻力等 mandatory 项时，写作接收门必须逐条响应，不得只写“已调用库”。

## 写作边界
只输出正文，除非用户明确要章纲或报告。开头快速接住上一章，不复述；中段必须发生实质变化；结尾必须形成下一章牵引。不得提前越过控制卡边界，不新增未铺垫的大设定或核心角色。

若控制卡或上下文状态为 `blocked`，不写定稿正文；先输出缺口、冲突和应修文件。状态为 `degraded` 或 NLCL `context_degraded=true` 时只能写带风险提示的草稿或局部试写，不能标为可发布稿。若 `state_freshness` 指出 `character_state/plot_state/foreshadowing_state` 滞后，正文不得自行补上一章事实；先要求回填，或在压力测试稿顶部标注 `context_degraded_risk_draft`。

长篇 `CH002+` 若 NLCL 给出 `continuation_state.status=fresh`，正文必须优先执行控制卡里的 `continuation_state_contract`。最低要求：接住 `characters.*.post_chapter_state`，遵守 `characters.*.continuation_constraints`，用 `plot.next_chapter_interface` 打开本章入场压力，推进或暂存 `plot.active_plot_threads`，并在 `foreshadowing.open_debts` / `foreshadowing.available_evidence` 中至少做一次可见触碰、误导、证据递进或禁止提前揭露。若正文只换了新开头、人物状态回滚、上一章资源/伤病/关系压力蒸发，标 `continuation_state_not_consumed`，返工 `L4_DRAFT_EXECUTION`；若控制卡本身没有转义这些字段，先返工 `L2_CONTROL_CARD`。

若缺少已保存的控制卡，或控制卡状态不是 `control_card_ready`，先回到 `novel-chapter-brief`；只有用户明确要求即兴试写时，才可输出草稿，并在状态里标 `control_card_pending` 或 `drafting`，不能标定稿。

## 执行包接收门

如果控制卡包含 `execution_pack`，正文阶段必须优先按执行包展开，而不是重新吞全量细纲、全量 skill、长素材和长聊天。执行包不是新大纲，它是当前章节的写作减负包：本节点写什么、POV 要什么、谁或什么阻止、用哪个物件/信息/关系推进、心理如何外化、语感锚点是什么、禁止越界在哪里、段末把什么递给下一节点，以及本节点写完用什么 `node_acceptance_gate` 验收。

正文起稿前必须先读控制卡或章节细纲的 `word_count_authority_chain`。优先服从用户本轮明确要求、本章控制卡 `target_length`、章节细纲单章目标和允许浮动；再看卷级预算与项目/平台通用边界。默认长章 2000-2500 字只是上游缺失时的兜底，不得覆盖用户说的“一章 2000 多字”或项目细纲。2000 字以上不是机械凑字，而是给现场、阻力、错误尝试、关系压力和余波留出可修空间；若只写 800-1200 字骨架却声称完成长章，标 `L4_DRAFT_EXECUTION`。

写作阶段只能把执行包当成展开合同，不得把它当成项目真值覆盖已定稿正文、人物状态、伏笔状态和用户明确指令。执行包缺少心理外化、关键物件/信息锚点、段末接口或 `node_acceptance_gate` 时，先回 `novel-chapter-brief` 补包，或在压力测试中标 `execution_pack_degraded_risk_draft`。

若控制卡包含 `mandatory_usage_plan`，正文阶段必须把每个 mandatory 项落到对应 `execution_pack` 节点或 beat，并在节点写作前确认：
- `expected_visible_effect` 会在正文里变成可见局面变化、信息阻力、规则代价、角色声纹、场景机制或去 AI 约束，而不是只在提示词里点名。
- 蒸馏入口只使用抽象机制和 validator，不复制来源书名、人名、设定壳、场景顺序、标志表达或作者声纹。
- ops 规则要改变实际写法或验收门；若正文没有可见响应，后续 `record-call-usage` 只能记 `unused` 或 `missed`，不能记 `used`。
- mandatory 项与项目真值冲突时，先停在 `L2_CONTROL_CARD` 或 `L3_DISTILLATION_RETRIEVAL` 处理，不允许正文自行折中。

`node_acceptance_gate` 是每个节点的最低顺读验收，不是正文后的长报告。写完一个节点，至少逐条确认：读者能看懂谁在做什么和为什么；压力句短但普通承接不断气；台词有交换、威胁、遮掩、索取、误会或资源变化；心理落到动作、物件、称呼、关系压力或选择代价；本节点确实改变了资源、风险、关系、信息或下一步行动。若任一节点只是酷感短句、心理总结、互讲设定或动作流水账，不能继续累加下一节点，先在 `L4_DRAFT_EXECUTION` 修本节点。

## 正文要求
- 第一段尽快进入当前压力、选择、异常或利益损失。
- 每章至少改变局面、关系、信息、风险、目标或资源之一。
- 按控制卡 beats 写作，每个关键剧情块都要落到正文场景，不多写漂移内容。
- 若控制卡带有 `distillationContext`，只使用其中的抽象机制、读者承诺、节奏规则和验证问题；不得复制来源作品的人名、地名、势力名、道具名、功法名、系统名、设定壳、名场面结构、标志句或作者声纹。
- 正文生成前把 `distillationContext.forbidden_source_elements` 写入硬禁区；蒸馏卡与本书项目真值、控制卡、人物状态或用户指令冲突时，丢弃蒸馏卡。
- 若项目存在用户样章或已定稿章节，写作和第二遍润色都要遵守 `文风规则.md` 中沉淀的作者指纹，不用通用口语化规则覆盖本书既有声音。
- 主动场景要有目标、冲突和挫折；被动场景要有反应、困境和决定。
- 对话服务试探、冲突、遮掩、索取或施压，不互相讲已知信息。
- 伏笔按控制卡操作，不提前揭露 forbid 项。
- 结尾不是总结，而是把未解问题、危险、收益、关系或选择递给下一章。

## 真网文草稿接收门
初稿不能只靠“AI 检测低”“看起来短句爽”或“调用了很多卡”放行。正文刚生成时只能标 `draft_generated` 或 `draft_candidate`；进入 `draft_done` 前，先过本门；失败时标 `L4_DRAFT_EXECUTION`，先重写正文执行，不交给去 AI 润色硬盖。

- 2000 字以上章节要能顺读：读者能连续读下去，知道谁在做什么、为什么此刻做、造成什么变化。
- 2000 字以上章节要有可修空间：初稿不能为了显得干净而只剩剧情骨架，至少保留现场锚点、阻力、错误尝试、关系压力和事件余波；这些是后续审稿和人工精修能下手的材料。
- 头三章、黄金三章和前两万字默认启用更严格的 AI 味接收门：不仅要剧情通顺，还要角色声纹可辨、心理描写外化、短句不过碎、每 500-800 字有可见推进，并留下冷读可复核证据。
- 短句只用于压力点、动作点、反应点和信息落点；连续 3-4 句短碎句后必须有清楚承接，不能形成断气、跳义、主谓残缺。
- “短句爽文”不是“全篇切碎”。爽感来自压力、选择、收益和局面变化，不来自把普通叙述切成词组。
- 执行 `sentence_mix_check` 时不能只靠肉眼感觉。2000 字以上风险稿若本地预检提示 `短句比例(≤12字)` 约 45%+、`连续短句(≤12字)` 达到 5+，或用户冷读觉得“刻意太短/断气”，先区分 `format_short_fragments` 和 `narrative_short_fragments`：规则、小票、系统面板、短信、订单、碑文等作品内格式可以短，但必须改变信息、资源、风险、身份或下一步行动；普通旁白、动作连续和因果承接不能借格式豁免断成词组。若非格式正文仍短碎，先合并无功能碎句：保留压力点短句，把普通旁白、动作连续、因果承接恢复成 20-60 字自然句；若合并后仍像梗概，按 `L4_DRAFT_EXECUTION` 补场景承重。
- 每 500-800 字至少有一次可见变化：压力升级、信息增量、关系转向、资源变化、小兑现或新债务。
- 核心事件不能从 A 直接跳到 B：至少写清铺垫、过程、余波。铺垫让读者产生预期，过程展示尝试/阻力/代价，余波改变关系、风险、资源或下一章债务。
- 番茄、短篇和投稿稿不能只满足“能读”：还要避开拒稿四病。开篇或卡点前若同质化严重、主线不明、节奏太慢或没有付费感，先回本章场景设计重写。
- 付费感不是故意拖着不兑现，而是在兑现前让读者看见压迫、代价、未完成的反击和下一步债务；爽点释放过早且没有新债务时，本章不能标 `draft_done`。
- 系统面板、任务栏、属性栏和资源账本必须改变现实处境：让主角获得/失去资源、暴露风险、被人误解、引发追问或逼出选择；不能只当说明书插入。若控制卡含 `system_panel_effect_contract`，正文必须写出 `panel_text` 被看见、`trigger_condition` 与当前压力咬合、`active_probe` 被行动验证、`first_real_world_effect` 改变资源/身份/关系/风险/价格/流程或敌我行动、`external_witness_or_pressure` 改变选择、`resource_or_status_delta` 有限制和后果、`new_cost_or_debt` 留下下一步压力。若正文只有面板、属性、到账或主角理解规则，标 `failure_signature=system_panel_without_real_world_effect`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点，不交给去 AI 润色。
- 修仙/玄幻必须执行“境界-资源-规矩-代价-兑现”链：若控制卡含 `xianxia_xuanhuan_chapter_contract`，正文必须让读者看见 `realm_state` 的差距或变化、`resource_ledger` 的获得/消耗/欠债/资格变化、`sect_or_rule_pressure` 对选择的限制、`technique_cost_limit` 的代价/冷却/暴露风险、`visible_trial_or_conflict` 的可见阻力、`earned_payoff` 如何由尝试/受阻/付价挣来、`status_delta` 如何改变身份/名声/危险/关系/路线入口，以及 `future_debt` 如何递到下一章。若正文只有境界名、功法说明、路人嘲讽、突然突破或众人震惊，标 `failure_signature=xianxia_xuanhuan_contract_not_consumed`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点，不交给去 AI 润色。
- 首章或黄金三章开篇若核心卖点是系统、返利、金手指、异能、重生记忆或其他变量改写现实，必须过 `front800_system_signal_gate`：前 800 字内出现变量苗头或主角主动试探，最好 300-500 字给征兆，800 字前挂出“凭什么翻盘”。若变量首次出现晚于 800 字，标 `L4_DRAFT_EXECUTION`，先重写开篇节奏。
- 当前章范围锁必须被正文执行：若控制卡包含 `chapter_scope_lock`，正文只能展开 `allowed_current_beats`。`future_seed_queue` 只能用作轻触伏笔、氛围信号或下一章接口，`do_not_consume_yet` 不得提前揭露、提前解决或写成当前章核心事件。若正文把后续剧情点提前消费，标 `failure_signature=chapter_scope_drift_to_future_plot`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点，不交给去 AI 润色。
- 规则怪谈必须执行规则-边界-代价链：若控制卡含 `rule_horror_boundary_cost_contract`，正文必须写出 `rule_text` 被看见、`boundary_test` 被行动试探、`near_violation` 或 `wrong_interpretation` 诱发错误判断、`visible_cost` 改变人/物/时间/路线/资源/身份暴露/生路宽度，且给读者留下 `reader_inference_window`。若正文只有规则列表、气氛短句、人物读规则或旁白解释，标 `failure_signature=rule_horror_rule_without_boundary_cost`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点，不交给去 AI 润色。
- 首章、黄金三章或前两万字如果读者不进戏，不能把问题简化成“提示词不够好”。正文接收前必须查 `first_chapter_prompt_misdiagnosis`：标题/简介承诺是否在前 500-800 字变成局面，主角是否有清楚目标，核心变量是否有苗头，冲突是否改变资源/关系/风险，是否有小兑现或强可追问债务。控制卡没给义务则返 `L2_CONTROL_CARD`；正文没执行则返 `L4_DRAFT_EXECUTION`；不得交给去 AI 当语言问题。
- 补情绪时必须防 `emotion_injection_explanatory_narration`：新增情绪不能只是“他明白/他意识到/他感到复杂/这一刻他终于懂了”这类解释性旁白。每个情绪节点至少落到目标、误判、旧伤、身体动作、物件、称呼、关系压力、遮掩、选择代价或后续行动中的两项；若只剩心理总结，先按 `L4_DRAFT_EXECUTION` 重写场景压力。
- 反转、成长或段落总结必须防 `grammar_over_drama_substitution`：不要用“不是……而是……”“A、B、C，这是……”或三连排比直接完成戏剧转折。正文必须让读者先看见预期怎样形成、角色怎样误判或行动、阻力怎样逼出选择、结果怎样改变关系/风险/资源/信息。若只靠句式翻面，标 `failure_signature=grammar_over_drama_substitution`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点；只有场景证据已足够但句式僵硬时才交 `L5_DEAI_STYLE`。
- 信息揭露必须防 `information_reveal_without_friction`：若控制卡含 `information_reveal_contract`，正文必须写出 `question_or_secret` 被谁追问、`withholder_or_obstacle` 怎样阻拦/闭嘴/误导、`wrong_lead_or_misread` 怎样让主角先走错、`trace_or_object_anchor` 怎样把信息漏出来、`cost_or_consequence` 怎样改变风险/关系/资源/身份暴露，并且只给 `partial_truth_now`，守住 `delayed_truth_boundary`。若正文只是“主角发现异常 -> NPC/旁白/系统解释设定 -> 主角得到答案/线索 -> 去下一地点”，标 `failure_signature=information_reveal_without_friction`、`layer_id=L4_DRAFT_EXECUTION`，重写对应 500-800 字节点，不交给去 AI 润色。
- 番茄扫榜或对标只能学习读者承诺、节奏效率和兑现方式，不能把低逻辑外壳、热梗皮、粗糙表达、榜书声纹或换皮设定写进正文。命中 `fanqie_benchmark_low_logic_shell` 时，先回包装、控制卡或蒸馏召回层修，不得靠语言润色遮盖。
- 外部提示词、禁用词和润色清单只能通过 `prompt_goal_budget` 进入正文阶段：每章最多执行 3-5 个验收目标，且必须让位于项目真值、执行包、人物声音和章节使命。若正文因为几十条死命令变成机械套模板、禁词恐惧、语序僵硬、角色声音丢失或场景目标缺席，标 `failure_signature=prompt_list_dead_command_overload`。控制卡缺预算返 `L2_CONTROL_CARD`，正文已写坏返 `L4_DRAFT_EXECUTION`，只有局部词句僵硬才交 `L5_DEAI_STYLE`。
- 对话必须带关系压力：称呼、身份、面子、资源、规则、旧账或当场目的至少命中一项；不得让所有角色都像作者在讲设定。
- 心理描写必须带可见执行：目标、误判、身体动作、物件选择、关系压力、未说出口的话或代价至少命中两项；只写“震惊、复杂、释然、坚定”而没有行动后果，不能过初稿接收门。
- 生活语序优先于“酷感压缩”：允许留白，不允许语义残缺；允许简洁，不允许读者补不出因果。
- 若用户、冷读或审稿指出“AI 味重、句子太碎、语义残缺、生硬直白、缺少生活化语感”，即使检测器或脚本未报红，也必须返修。
- 若用户或审稿指出“太像梗概、写得太瘦、修完没剩多少”，优先按 `L4_DRAFT_EXECUTION` 补足场景承重，不要直接交 `novel-style-deai` 做表层润色。
- 若上一章有续写卡/记忆保险丝，本章必须接住其中的伤病、资源、伏笔、系统限制和下一章接口；不能把“右臂反折”压成笼统“受伤”，也不能让男频主角被安全偏好磨成无理由心软。
- 若上一章由 NLCL `continuation_state` 提供，正文接收门必须能指出每个关键人物的 `post_chapter_state` 被哪一段承接、`next_chapter_interface` 被哪一场承接、`open_debts` 或 `available_evidence` 被哪一个动作/信息锚点触碰；找不到证据不能标 `draft_done`。
- 若控制卡或 NLCL 执行包包含 `pov_distance_headhop_gate`，正文必须遵守本场 `pov_character / narrative_distance / knowledge_boundary`：近距/三限段落只能写 POV 人物可感知、可误判、可推断的内容；不得突然进入非 POV 人物内心、替敌人解释动机，或用全知旁白提前泄露信息。需要多 POV/全知时，必须在 `scene_id` 或段落边界切换，并让切换服务信息差、悬念或承压，不得只是方便解释。若正文跳脑袋、知识边界泄漏或过滤词堆叠，标 `failure_signature=pov_headhop/knowledge_boundary_leak/narrator_overexplains_motive/filtering_distance_overuse`；边界缺失返 `L2_CONTROL_CARD`，正文执行错返 `L4_DRAFT_EXECUTION`，只剩局部过滤词/语序问题才交 `L5_DEAI_STYLE`。
- 若控制卡或 NLCL 执行包包含 `kickoff_pack / seam_check / entry_gate`，正文接收门必须逐项回应：开头不能重演上一章结尾、不能重新发现已知事实、不能复述旧遭遇；新事件入口必须能指出谁拉主角入局、拉入者动机是否自洽、主角为什么躲不掉。写后 `seam_check` 任一项 fail 时，先重写开头或入口场景，不交给去 AI 润色。

## 分段写作
长章、首章、高风险章或用户要求稳定产出时，采用工作台分段：
- 节点展开：每次只写 500-800 字，只消费一个 beat 或场景微卡。
- 段落精修：每次只写 2000-3000 字，不跨下一章，不提前消费下一节点核心事件。
- 每段结尾给接口：本段实际字数、涉及角色状态变化、下一段建议承接的一句话；正式正文交付时可隐藏这些生产注记。
- 每 500-800 字至少有情绪转折、信息增量、关系变化、压力升级或小兑现。

## 流程
1. 接收 `control_card_ready` 后，把章节状态推进到 `drafting`。
1.1 若控制卡来自 NLCL 且 `context_degraded=true`，不推进到正式 `drafting`；只能进入 `risk_drafting` 或返回 `novel-chapter-brief` 补状态。
1.2 若控制卡来自 NLCL 且长篇 `CH002+` 有 fresh `continuation_state`，先核对控制卡已生成 `continuation_state_contract`；没有则返回 `novel-chapter-brief`，不要直接写正文。
2. 根据控制卡、上下文健康标记和相关性提取结果写初稿，完成后先标 `draft_generated`；若正文可读但尚未过接收门，标 `draft_candidate`，不得直接标 `draft_done`。
3. 正文热路径只做最低可写性预检、蒸馏卡适配检查、正文生成、接收闸门和必要的单次局部修复；不要把全量状态抽取、伏笔对账和图谱同步塞进写正文这一步。
3.1 接收闸门必须包含“真网文草稿接收门”。若命中语义残缺、短句过度、系统面板说明书化、资源账本无后果、对话同声或 2000 字顺读失败，先在 `L4_DRAFT_EXECUTION` 重写对应场景；不得把失败初稿直接交 `novel-style-deai` 当语言问题擦痕。
3.2 只有接收门通过，并能指出正文证据覆盖章节使命、场景契约、必要库调用和下一章接口时，才把状态从 `draft_candidate` 推进到 `draft_done`。`draft_done` 仍不是定稿，只表示可以进入去 AI、审稿和版本链。
4. 自检章纲必达项、必现蓝图、剧情-正文对齐、信息边界、人物状态、伏笔、世界规则和章末钩子。
4.1 若使用蒸馏卡，执行防污染闸门：实体泄漏、设定壳泄漏、场景壳相似、标志句相似、项目真值污染。任一失败，先替换卡或重写场景，不进入语言润色。
5. 若章节较长，按场景微卡逐场写作，每场必须有目标、冲突、结果或价值转向；主动/被动场景要形成行动循环。若场景只是机械罗列时间、动作和对话，先补铺垫、过程、余波，不把流水账交给去 AI 润色。若本轮是在持续调系统而迟迟不开写，且控制卡、检索、审稿和去 AI 闸门已健康，优先写 2000 字以上实测章节，用问题卡暴露下一轮修复点，避免规则空转。
6. 第一遍只查逻辑、情节、人设、场景和设定，不改文风；通过后再进入语言处理。有作者样章时，先校准作者指纹，再执行通用去模板化。
6.1 若本轮包含人工改稿，先抽 `edit_delta`：人工删改了什么、稳定强化了什么表达、补进了哪些新事实、可能引入哪些真值冲突。`edit_delta` 先服务一致性复查和作者指纹校准，不直接跳过验证闸门。
7. 默认交 `novel-style-deai` 做真实感、节奏和去模板化，通过后标 `deai_done`。但若人工改稿后同时满足 `edit_delta.conflict_candidates` 为空、`truth_consistency_gate=pass`、`voice_coherence >= 0.75`、`template_trace <= low`、`review_readiness=pass`，才可标 `polish_skipped=accepted_manual`，直接进入章节审稿；缺任一客观门槛时降级为 `polish_skipped=needs_review`，避免把它当作通用去模板化的逃生口。
8. 交 `novel-chapter-reviewer` 评分，状态标 `review_pending`，并输出缺失义务与可修复性分类。
9. 低分或作者阻断反馈交 `novel-score-improver`，达标后按写后回填协议更新状态与账本。

## 版本链
正式连载可只保留定稿和修改记录；压力测试、低分返工、A/B 比较或重大章节必须保留版本链：
- 控制卡。
- 初稿或候选稿。
- 人工改稿版（若存在）。
- 去模板化稿。
- 审稿报告和问题卡。
- 定稿。
- 回填记录。

版本链要写明采用/放弃原因；若跳过第二遍润色，要记录 `polish_skipped` 的理由和对应证据，避免后续只看到最终文本却不知道哪类问题被修过，或误把人工稿当成未经判断的直出稿。

## EPUB 同步导出
正文 `.md` 是编辑真值，EPUB 是给作者阅读和复制检查的同步产物。每次完成章节生成、续写、修订、去模板化、定稿或作者反馈返修后，若本轮改动触及正文文件，必须在回填后运行：

```bash
python novel-creation-suite/references/bundled-skills/novel-suite-orchestrator/scripts/export_epub.py "<项目根目录>"
```

导出结果放在 `10_导出文件/`。若只想看局部，可用 `--from XXX --to YYY` 生成局部 EPUB，但全书 EPUB 仍应在正式回填后刷新。不得把章纲、控制卡、审稿报告、修改记录或设定文件导入 EPUB。

## 写后回填
正文通过后，先抽取 facts_delta，再压缩进长期 snapshot，并更新人物状态、伏笔、正文总览、每日进度、修改记录、追读设计、实体索引/关系图谱和质量资产；若新增地点、规则、物品、势力或概念，回填对应世界观文件和术语表。需要机器可读闭环时，内部生成 `---CHANGES---` 变更声明，但不要把它混入用户只要的正文。

每章定稿后同步抽取下一章续写卡和记忆保险丝：结尾定格、未解钩子、人物伤病/疲劳/中毒、资源增减、未研究道具、伏笔债务、系统限制、人设硬锚和下一章开头接口。下一章控制卡必须读取它们；缺失时标 `context_degraded`，不能让 AI 自行补连续性。

同一正文 hash 未变化时，只记录复审或修复结论，不重复刷新状态快照、伏笔账本、图谱索引和角色动态。高风险章、卷尾、重大秘密揭示或 strict 模式才等待必要资产同步后继续下一章。

只有章节审稿通过且作者无阻断反馈，才能从 `review_pending` 进入 `approved/finalized`；写回人物、伏笔、总览、进度和修改记录，并完成 EPUB 同步导出后标 `writeback_done`，下一章入口才标 `next_ready`。

## 兼容
`novel-volume1-001-050`、`novel-volume1-051-100`、`novel-volume2-folder` 是范围包装；第三卷及以后、任意新增卷和自定义卷名都直接使用本 skill，真实正文逻辑以本 skill 为准。
