import { expect, test } from '@playwright/test'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const evidenceDir = path.resolve(
  process.env.PLAYWRIGHT_EVIDENCE_DIR ?? path.resolve(process.cwd(), 'test-results/evidence'),
)
const runtimeUrl = process.env.PLAYWRIGHT_RUNTIME_URL
  ?? `http://127.0.0.1:${process.env.PLAYWRIGHT_RUNTIME_PORT ?? '8789'}`
const providerUrl = process.env.PLAYWRIGHT_PROVIDER_URL
  ?? `http://127.0.0.1:${process.env.PLAYWRIGHT_PROVIDER_PORT ?? '18790'}`
const sourceProjectRoot = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/minimal-novel',
)
const localSourcePath = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/local-source/archive-night-sample.txt',
)

type KnowledgeCardType = 'work' | 'voice' | 'validator'
type RuntimeReceiptKind =
  | 'prepared'
  | 'provider_dispatch'
  | 'provider_turn_result'
  | 'candidate_output'
  | 'final_result'

interface RuntimeDeepInvocationSnapshot {
  invocation: {
    invocation_id: string
    status: string
    provider_replay_count: number
    provider_turn_count: number
    last_turn_result_receipt_id: string | null
    last_turn_result_receipt_hash: string | null
    candidate_output_receipt_id: string | null
    candidate_output_receipt_hash: string | null
    final_result_receipt_id: string | null
    final_result_receipt_hash: string | null
    candidate_set_hash: string | null
    work_count: number | null
    voice_count: number | null
    validator_count: number | null
  }
  receipts: Array<{
    kind: string
    receipt_id: string
    receipt_hash: string
  }>
}

interface RuntimeDeepAcceptance {
  invocation_id: string
  status: 'succeeded'
  provider_turn_count: number
  provider_replay_count: 0
  counts: Record<KnowledgeCardType, 1>
  last_turn_result_receipt_id: string
  last_turn_result_receipt_hash: string
  candidate_output_receipt_id: string
  candidate_output_receipt_hash: string
  final_result_receipt_id: string
  final_result_receipt_hash: string
  ledger_kinds: string[]
}

const runtimeInvocationPattern = /^distillation-invocation-[0-9a-f]{32}$/
const sha256Pattern = /^[a-f0-9]{64}$/
const distillationToolNames = [
  'distillation_anchor_search',
  'distillation_anchor_read',
  'distillation_output_submit',
] as const

test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true })
})

async function fillKnowledgeText(
  editor: import('@playwright/test').Locator,
  label: string,
  value: string,
) {
  await editor.getByRole('textbox', { name: label, exact: true }).fill(value)
}

async function openReviewSections(editor: import('@playwright/test').Locator) {
  for (const section of ['路由与安全边界', '可执行机制', '读者效果', '验证与评分']) {
    const summary = editor.locator('summary').filter({ hasText: section })
    if ((await summary.count()) > 0) {
      await summary.first().click()
    }
  }
}

async function assertDocumentWidth(
  page: import('@playwright/test').Page,
  maxWidth: number,
) {
  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document).toBeLessThanOrEqual(maxWidth)
  expect(widths.body).toBeLessThanOrEqual(maxWidth)
  return widths
}

function assertHexHash(value: string | null, label: string): asserts value is string {
  expect(value, label).toMatch(sha256Pattern)
}

function assertRuntimeDeepSnapshot(
  snapshot: RuntimeDeepInvocationSnapshot,
  expectedInvocationId: string,
): RuntimeDeepAcceptance {
  expect(expectedInvocationId).toMatch(runtimeInvocationPattern)
  const head = snapshot.invocation
  expect(head.invocation_id).toBe(expectedInvocationId)
  expect(head.status).toBe('succeeded')
  expect(head.provider_replay_count).toBe(0)
  expect(head.work_count).toBe(1)
  expect(head.voice_count).toBe(1)
  expect(head.validator_count).toBe(1)
  expect(head.provider_turn_count).toBeGreaterThanOrEqual(2)
  const lastTurnResultReceiptId = head.last_turn_result_receipt_id
  const lastTurnResultReceiptHash = head.last_turn_result_receipt_hash
  const candidateOutputReceiptId = head.candidate_output_receipt_id
  const candidateOutputReceiptHash = head.candidate_output_receipt_hash
  const finalResultReceiptId = head.final_result_receipt_id
  const finalResultReceiptHash = head.final_result_receipt_hash
  assertHexHash(lastTurnResultReceiptHash, 'last turn result receipt hash')
  assertHexHash(candidateOutputReceiptHash, 'candidate output receipt hash')
  assertHexHash(finalResultReceiptHash, 'final result receipt hash')
  assertHexHash(head.candidate_set_hash, 'runtime candidate set hash')
  expect(lastTurnResultReceiptId, 'last turn result receipt id').toBeTruthy()
  expect(candidateOutputReceiptId, 'candidate output receipt id').toBeTruthy()
  expect(finalResultReceiptId, 'final result receipt id').toBeTruthy()
  const ledgerKinds = snapshot.receipts.map((receipt) => receipt.kind)
  for (const kind of [
    'prepared',
    'provider_dispatch',
    'provider_turn_result',
    'candidate_output',
    'final_result',
  ] satisfies RuntimeReceiptKind[]) {
    expect(ledgerKinds, `Runtime ledger includes ${kind}`).toContain(kind)
  }
  return {
    invocation_id: head.invocation_id,
    status: 'succeeded',
    provider_turn_count: head.provider_turn_count,
    provider_replay_count: 0,
    counts: { work: 1, voice: 1, validator: 1 },
    last_turn_result_receipt_id: lastTurnResultReceiptId as string,
    last_turn_result_receipt_hash: lastTurnResultReceiptHash,
    candidate_output_receipt_id: candidateOutputReceiptId as string,
    candidate_output_receipt_hash: candidateOutputReceiptHash,
    final_result_receipt_id: finalResultReceiptId as string,
    final_result_receipt_hash: finalResultReceiptHash,
    ledger_kinds: Array.from(new Set(ledgerKinds)),
  }
}

async function scrollKnowledgePanelTo(
  page: import('@playwright/test').Page,
  selector: string,
  block: ScrollLogicalPosition = 'start',
) {
  const locator = page.locator(selector).first()
  await locator.evaluate((element, blockArg) => {
    element.scrollIntoView({ block: blockArg as ScrollLogicalPosition, inline: 'nearest' })
  }, block)
  return locator
}

async function fillSharedKnowledgeReview(
  editor: import('@playwright/test').Locator,
  cardType: KnowledgeCardType,
) {
  const titles: Record<KnowledgeCardType, string> = {
    work: '选择、代价与阶段兑现的长篇推进机制',
    voice: '贴近人物压力的自然叙事声音规则',
    validator: '人物压力与来源污染的正文拒收闸门',
  }
  const summaries: Record<KnowledgeCardType, string> = {
    work: '以可见压力迫使人物选择，让不可逆代价改变行动空间，并把阶段兑现转成下一层义务，持续推进长篇冲突。',
    voice: '保持贴近人物判断的有限视角，用动作、关系和资源变化承接情绪，避免抽象报告腔并保持现场可读性。',
    validator: '正文候选进入作者审阅前，检查人物压力是否落到行动和代价，并阻断来源表达污染，否则立即退回重写。',
  }
  const promises: Record<KnowledgeCardType, string> = {
    work: '每次阶段兑现都改变人物下一步能做什么，并留下可追溯的新义务。',
    voice: '读者能看清人物正在做什么、为什么犹豫，以及这个选择要承担什么代价。',
    validator: '只有人物行动、关系和代价可追溯且没有来源污染的候选才能进入审阅。',
  }
  const cardIds: Record<KnowledgeCardType, string> = {
    work: 'distilled-work-runtime-pressure-loop',
    voice: 'distilled-voice-runtime-natural-pressure',
    validator: 'distilled-validator-runtime-source-gate',
  }

  await fillKnowledgeText(editor, 'card_id', cardIds[cardType])
  await fillKnowledgeText(editor, '标题', titles[cardType])
  await fillKnowledgeText(editor, '摘要', summaries[cardType])
  await fillKnowledgeText(editor, '读者承诺', promises[cardType])
  await fillKnowledgeText(
    editor,
    '标签',
    cardType === 'work'
      ? '番茄\n长篇推进\n阶段兑现'
      : '番茄\n男频\n玄幻\n正文审阅\n来源隔离\n作者确认',
  )
  await fillKnowledgeText(editor, '关键词', '人物压力\n来源隔离\n阶段兑现')
  await fillKnowledgeText(editor, '适用任务', 'draft_scene\nrewrite\nreview')
  await fillKnowledgeText(editor, '来源范围', '本地 TXT 样本与三章相邻片段的作者复核范围')
  await editor.getByRole('combobox', { name: '证据强度', exact: true }).selectOption('medium')
  await fillKnowledgeText(
    editor,
    '证据说明',
    '作者复核了三章完整样本，只保留可迁移机制，并排除来源措辞、人物名称和场景顺序。',
  )

  await openReviewSections(editor)
  await fillKnowledgeText(editor, '推荐 profiles', 'chapter\ndraft_assist\nreview')
  await fillKnowledgeText(editor, '路由词', '玄幻\n人物压力\n阶段兑现')
  await fillKnowledgeText(editor, '负路由', '来源复刻\n只做文案建议')
  await fillKnowledgeText(editor, '版权边界', '只保留抽象机制，不复制来源人物、表达、设定壳或完整场景顺序。')
  await fillKnowledgeText(editor, '禁止复用边界', '来源人物名称\n来源标志句\n来源场景顺序')
  await fillKnowledgeText(editor, '机制名', '选择、代价与后续压力闭环')
  await fillKnowledgeText(editor, '适用时机', '章节需要同时推进人物行动空间、关系压力和后续义务时。')
  await fillKnowledgeText(editor, '规则', '阶段兑现前必须出现不可逆选择，兑现后必须改变下一步行动空间。')
  await fillKnowledgeText(editor, '失败模式', '先给奖励再补选择，会让人物决定失去重量，也无法形成后续压力。')
  await fillKnowledgeText(editor, '作者意图', '让每次推进同时改变人物能力、关系站位和后续责任。')
  await fillKnowledgeText(editor, '观察效果', '眼前压力得到一次释放，但人物因自己的选择背上更大的义务。')
  await fillKnowledgeText(editor, '效果证据', '三个连续章节都能从人物决定追踪到资源变化和下一次冲突。\n相邻章节能看到兑现后的义务继续压回主角。')
  await fillKnowledgeText(editor, '失效边界', '只有数字奖励而没有行动空间或关系变化时，此机制失效。')
  await fillKnowledgeText(editor, '误读风险', '照搬来源名词和表层情节会掩盖真正需要迁移的因果机制。')
  await fillKnowledgeText(editor, '转坏节点', '兑现没有影响下一次选择，或所有人物只负责解释设定时。')
  await fillKnowledgeText(
    editor,
    '验收测试',
    '确认兑现改变下一步可选行动，而不只是增加数值。\n确认新义务由本章人物选择和代价直接造成。',
  )

  for (const label of ['内部一致', '预测验证', '盲样验证', '极端压力']) {
    await editor.getByRole('checkbox', { name: label, exact: true }).check()
    await fillKnowledgeText(
      editor,
      `${label}证据`,
      `作者已记录 ${label} 的可复核样本，并确认规则在不复制来源表达时仍然成立。\n第二条证据用于验证多证据不会在保存时被截断。`,
    )
  }
  for (const [label, value] of [
    ['证据', '0.78'],
    ['具体', '0.82'],
    ['生成', '0.84'],
    ['检索', '0.76'],
    ['风险', '0.12'],
  ] as Array<[string, string]>) {
    await editor.getByRole('spinbutton', { name: label, exact: true }).fill(value)
  }
}

async function fillWorkKnowledgeReview(editor: import('@playwright/test').Locator) {
  await fillKnowledgeText(editor, '题材分类', '玄幻\n长篇成长')
  await fillKnowledgeText(editor, '场景功能', '阶段兑现\n压力递进')
  await fillKnowledgeText(editor, '情绪分类', '压迫感\n获得感后置')
  await fillKnowledgeText(editor, '技法分类', '选择代价闭环\n章节尾部反压')
  await fillKnowledgeText(editor, '前提模式', '受限人物为获得新的行动空间，必须先接受一个不可逆代价。')
  await fillKnowledgeText(editor, '核心承诺', '每次选择都会兑现眼前收益，同时生成更高一级且可追溯的义务。')
  await fillKnowledgeText(editor, '开篇合同', '先展示一项可见损失，再给出机会有限且伴随风险的选择。')
  await fillKnowledgeText(editor, '成长路线', '把当前资源与关系压力外化\n迫使人物作出后果明确的选择\n兑现收益并生成更高一级压力')
  await fillKnowledgeText(editor, '循环场景', '资源受限 -> 风险试探 -> 阶段兑现 -> 新义务压回人物')
  await fillKnowledgeText(editor, '迁移边界', '不得复制来源人物名称\n不得复制来源世界设定壳\n不得复制来源完整场景顺序')
}

async function fillVoiceKnowledgeReview(editor: import('@playwright/test').Locator) {
  await fillKnowledgeText(editor, '平台', '番茄')
  await fillKnowledgeText(editor, '受众', '男频')
  await fillKnowledgeText(editor, '题材分类', '玄幻')
  await fillKnowledgeText(editor, '子题材', '升级流\n冒险')
  await fillKnowledgeText(editor, '风格分类', '现场压力\n自然网文')
  await fillKnowledgeText(editor, '视角分类', '贴近当前行动人物的有限视角')
  await fillKnowledgeText(editor, '内容边界', '只迁移抽象叙事行为\n不模仿来源作者声纹')
  await fillKnowledgeText(editor, '视角距离', '贴近当前人物判断，但不替人物总结全部情绪和主题。')
  await fillKnowledgeText(editor, '句式节奏', '动作处短句落点，关系变化用中句承接，解释不连续堆叠。')
  await fillKnowledgeText(editor, '用词池', '具体动作与物件\n人物关系站位\n可见资源和代价')
  await fillKnowledgeText(editor, '对话行为', '每句对话携带目的、关系站位或信息差，不让角色轮流讲设定。')
  await fillKnowledgeText(editor, '描写行为', '描写必须改变人物判断或行动空间，不铺陈与当前冲突无关的气氛。')
  await fillKnowledgeText(editor, '情绪处理', '先写身体反应、动作选择和关系后果，再让读者自己得出情绪。')
  await fillKnowledgeText(editor, '自然网文表达', '句子语义完整、主谓清楚并落在当前场景，连续阅读不出现报告腔。')
  await fillKnowledgeText(editor, '句式变化', '短中长句按压力变化交替，避免连续碎句和为了酷感破坏自然语序。')
  await fillKnowledgeText(editor, '场景锚点规则', '每两到三段出现一次会改变判断的动作、物件或资源变化。')
  await fillKnowledgeText(editor, '对话压力规则', '每句对话至少推进目的、关系站位或信息差中的一项。')
  await fillKnowledgeText(editor, 'AI 痕迹失败特征', '连续机械碎句\n抽象心理替代动作\n角色同声解释设定')
  await fillKnowledgeText(editor, '可读性验收测试', '连续阅读两千字时能分清谁在行动、谁在害怕以及谁承担代价。')
  await fillKnowledgeText(editor, '禁止复制内容', '来源作者标志句\n来源作品节奏链\n来源人物口头禅')
  await fillKnowledgeText(editor, 'NLCL 路由层', 'distillation_index\nstyle\ndeai')
}

async function fillValidatorKnowledgeReview(editor: import('@playwright/test').Locator) {
  await fillKnowledgeText(editor, '平台', '番茄')
  await fillKnowledgeText(editor, '受众', '男频')
  await fillKnowledgeText(editor, '题材分类', '玄幻')
  await fillKnowledgeText(editor, '子题材', '升级流\n冒险')
  await fillKnowledgeText(editor, '问题症状', '抽象总结\n来源污染')
  await fillKnowledgeText(editor, '内容边界', '正文候选拒收闸门')
  await fillKnowledgeText(editor, '校验器名称', 'character_pressure_and_source_contamination_gate')
  await fillKnowledgeText(editor, '适用对象', '完整章节正文候选\n针对性正文改写候选')
  await editor.getByRole('combobox', { name: '执行层', exact: true }).selectOption('L6_REVIEW_ACCEPTANCE')
  await fillKnowledgeText(
    editor,
    '检查项',
    '人物选择是否通过动作、关系或资源变化产生可见代价。\n正文是否避开来源名称、设定壳、标志句和可识别场景顺序。',
  )
  await fillKnowledgeText(
    editor,
    '拒收条件',
    '连续使用抽象心理总结替代人物行动和关系后果。\n出现来源专名、标志表达或未经迁移的完整场景顺序。',
  )
  await fillKnowledgeText(editor, '重写路由', 'L4_DRAFT_EXECUTION\nL5_DEAI_STYLE')
  await fillKnowledgeText(editor, 'NLCL 路由层', 'distillation_index\nreview')

  for (let index = 0; index < 8; index += 1) {
    const deleteRule = editor.getByRole('button', { name: /删除执行规则/ }).first()
    if ((await deleteRule.count()) === 0) break
    await deleteRule.click()
  }

  await editor.getByRole('button', { name: '添加执行规则', exact: true }).click()
  const forbidRule = editor.getByRole('group', { name: '执行规则 1', exact: true })
  await forbidRule.getByRole('textbox', { name: '规则 ID', exact: true }).fill('forbid_source_terms')
  await forbidRule.getByRole('combobox', { name: '规则类型', exact: true }).selectOption('forbid_terms')
  await forbidRule.getByRole('textbox', { name: '匹配词项', exact: true }).fill('Archive Night\nsource shell')
  await forbidRule.getByRole('textbox', { name: '失败签名', exact: true }).fill('validator_source_term_leak')
  await forbidRule.getByRole('textbox', { name: '失败提示', exact: true }).fill('正文候选包含正式校验器明确禁止的来源词项。')

  await editor.getByRole('button', { name: '添加执行规则', exact: true }).click()
  const requireRule = editor.getByRole('group', { name: '执行规则 2', exact: true })
  await requireRule.getByRole('textbox', { name: '规则 ID', exact: true }).fill('require_visible_pressure')
  await requireRule.getByRole('combobox', { name: '规则类型', exact: true }).selectOption('require_any_term')
  await requireRule.getByRole('textbox', { name: '匹配词项', exact: true }).fill('代价\n压力\n选择')
  await requireRule.getByRole('textbox', { name: '失败签名', exact: true }).fill('validator_visible_pressure_missing')
  await requireRule.getByRole('textbox', { name: '失败提示', exact: true }).fill('正文候选缺少正式校验器要求的可见压力或代价词项。')
}

async function fillAndConfirmKnowledgeCandidate(
  page: import('@playwright/test').Page,
  cardType: KnowledgeCardType,
) {
  const candidateTabs = page.getByRole('tablist', { name: '知识候选', exact: true })
  const candidateTab = candidateTabs.getByRole('tab').filter({ hasText: cardType }).first()
  await candidateTab.click()
  const editor = page.locator('.knowledge-editor')
  await expect(editor).toBeVisible()
  await fillSharedKnowledgeReview(editor, cardType)
  if (cardType === 'work') await fillWorkKnowledgeReview(editor)
  if (cardType === 'voice') await fillVoiceKnowledgeReview(editor)
  if (cardType === 'validator') await fillValidatorKnowledgeReview(editor)
  await editor.getByRole('checkbox', { name: /我已逐项确认/ }).check()
  return { candidateTab, editor }
}

async function saveAndPromoteCurrentCandidate(
  editor: import('@playwright/test').Locator,
  candidateTab: import('@playwright/test').Locator,
) {
  await editor.getByRole('button', { name: '保存正式复核', exact: true }).click()
  const promote = editor.getByRole('button', { name: '晋升正式卡', exact: true })
  await expect(promote).toBeEnabled({ timeout: 20_000 })
  await promote.click()
  await expect(candidateTab).toContainText('待整批正式化', { timeout: 20_000 })
}

async function reviewAndPromoteKnowledgeCandidate(
  page: import('@playwright/test').Page,
  cardType: KnowledgeCardType,
) {
  const { candidateTab, editor } = await fillAndConfirmKnowledgeCandidate(page, cardType)
  await saveAndPromoteCurrentCandidate(editor, candidateTab)
}

test('O2-B corrective focused local source review formalizes and recovers', async ({ page, request }) => {
  test.setTimeout(300_000)

  const desktopRuntimeDeepPath = path.join(evidenceDir, 'o2b-corrective-runtime-deep-invocation-desktop-1440x900.png')
  const desktopReviewOverviewPath = path.join(evidenceDir, 'o2b-corrective-review-overview-desktop-1440x900.png')
  const desktopReviewActionPath = path.join(evidenceDir, 'o2b-corrective-review-action-desktop-1440x900.png')
  const mobileCandidateOverviewPath = path.join(evidenceDir, 'o2b-corrective-candidate-overview-mobile-390x844.png')
  const mobileReviewActionPath = path.join(evidenceDir, 'o2b-corrective-review-action-mobile-390x844.png')
  const desktopFormalizedPath = path.join(evidenceDir, 'o2b-corrective-formalized-desktop-1440x900.png')
  const mobileFormalizedPath = path.join(evidenceDir, 'o2b-corrective-formalized-mobile-390x844.png')
  const acceptancePath = path.join(evidenceDir, 'o2b-corrective-acceptance-20260723.json')
  await rm(acceptancePath, { force: true })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`, { maxRetries: 2 })
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json() as {
    projects: Array<{ project_id: string; root_path: string }>
  }).projects.find((item) => path.resolve(item.root_path) === path.resolve(sourceProjectRoot))
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id

  await page.getByRole('button', { name: '资料', exact: true }).click()
  await page.getByRole('button', { name: '本地 TXT / EPUB', exact: true }).click()
  await page.getByRole('textbox', { name: '本地 TXT / EPUB', exact: true }).fill(localSourcePath)
  const createImportResponse = page.waitForResponse((response) => {
    const requestRecord = response.request()
    return new URL(response.url()).pathname === '/api/v1/source-imports'
      && requestRecord.method() === 'POST'
  }, { timeout: 30_000 })
  await page.getByRole('button', { name: '校验并蒸馏', exact: true }).click()
  const createdImport = await createImportResponse
  expect(createdImport.status()).toBe(202)
  const sourceImportId = (await createdImport.json() as { import_id: string }).import_id

  await expect(page.getByText('蒸馏草案待复核').first()).toBeVisible({ timeout: 60_000 })
  const deepInvocation = page.getByRole('region', { name: 'Runtime 深蒸馏 Invocation', exact: true })
  await expect(deepInvocation).toContainText('仍是旧 Source Import Skill 草案')

  const prepareResponsePromise = page.waitForResponse((response) => {
    const requestRecord = response.request()
    return new URL(response.url()).pathname
      === `/api/v1/source-imports/${sourceImportId}/distillation-invocation/prepare`
      && requestRecord.method() === 'POST'
  }, { timeout: 30_000 })
  await deepInvocation.getByRole('button', { name: '准备 Runtime 深蒸馏', exact: true }).click()
  const prepareResponse = await prepareResponsePromise
  expect(prepareResponse.ok()).toBeTruthy()
  const preparePayload = await prepareResponse.json() as { snapshot: RuntimeDeepInvocationSnapshot }
  const invocationId = preparePayload.snapshot.invocation.invocation_id
  expect(invocationId).toMatch(runtimeInvocationPattern)
  await expect(deepInvocation).toContainText('已准备', { timeout: 30_000 })
  await expect(deepInvocation).toContainText(`invocation_id · ${invocationId}`)

  const runResponsePromise = page.waitForResponse((response) => {
    const requestRecord = response.request()
    return new URL(response.url()).pathname
      === `/api/v1/distillation/invocations/${invocationId}/run`
      && requestRecord.method() === 'POST'
  }, { timeout: 60_000 })
  await deepInvocation.getByRole('button', { name: '运行 Provider 深蒸馏', exact: true }).click()
  const runResponse = await runResponsePromise
  expect(runResponse.ok()).toBeTruthy()
  const runSnapshot = await runResponse.json() as RuntimeDeepInvocationSnapshot
  const initialRuntimeDeep = assertRuntimeDeepSnapshot(runSnapshot, invocationId)
  await expect(deepInvocation).toContainText(/候选已持久化|已成功/, { timeout: 30_000 })
  await expect(deepInvocation).toContainText(`${initialRuntimeDeep.provider_turn_count} turn`)
  await expect(deepInvocation).toContainText('1/1/1')
  await expect(deepInvocation).toContainText('candidate_output_receipt_id')
  await deepInvocation.scrollIntoViewIfNeeded()
  await page.screenshot({ path: desktopRuntimeDeepPath, fullPage: false })

  const providerLastRequestResponse = await request.get(`${providerUrl}/last-request`)
  expect(providerLastRequestResponse.ok()).toBeTruthy()
  const providerLastRequest = await providerLastRequestResponse.json() as { tool_names: string[] }
  expect([...providerLastRequest.tool_names].sort()).toEqual([...distillationToolNames].sort())

  await page.getByRole('button', { name: '复核候选', exact: true }).click()
  const candidateTabs = page.getByRole('tablist', { name: '知识候选', exact: true })
  await expect(candidateTabs.getByRole('tab')).toHaveCount(3, { timeout: 30_000 })

  const { candidateTab: workTab, editor } = await fillAndConfirmKnowledgeCandidate(page, 'work')
  await page.locator('.knowledge-panel').evaluate((element) => { element.scrollTop = 0 })
  await expect(page.getByRole('list', { name: '资料到写作上下文流程' })).toBeVisible()
  await expect(page.getByRole('region', { name: '三类知识候选' })).toBeVisible()
  await expect(editor.getByRole('textbox', { name: 'card_id', exact: true })).toBeVisible()
  const desktopOverviewWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopReviewOverviewPath, fullPage: false })

  await scrollKnowledgePanelTo(page, '.knowledge-scores', 'center')
  await expect(editor.locator('.knowledge-scores')).toBeVisible()
  await expect(editor.locator('.knowledge-review-confirmation')).toBeVisible()
  await expect(editor.getByRole('button', { name: '保存正式复核', exact: true })).toBeEnabled()
  const desktopActionWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopReviewActionPath, fullPage: false })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  await scrollKnowledgePanelTo(page, '.knowledge-candidate-board', 'start')
  await expect(page.getByRole('region', { name: '三类知识候选' })).toBeVisible()
  await expect(candidateTabs.getByRole('tab').filter({ hasText: 'work' }).first()).toBeVisible()
  const mobileOverviewWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileCandidateOverviewPath, fullPage: false })

  await scrollKnowledgePanelTo(page, '.knowledge-scores', 'center')
  await expect(editor.locator('.knowledge-review-confirmation')).toBeVisible()
  await expect(editor.getByRole('button', { name: '保存正式复核', exact: true })).toBeVisible()
  const mobileActionWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileReviewActionPath, fullPage: false })

  await page.setViewportSize({ width: 1440, height: 900 })
  await saveAndPromoteCurrentCandidate(editor, workTab)
  await reviewAndPromoteKnowledgeCandidate(page, 'voice')
  await reviewAndPromoteKnowledgeCandidate(page, 'validator')
  await expect(page.getByRole('status', { name: '候选状态汇总' })).toContainText('promoted 3')
  await page.getByRole('button', { name: '完成本批复核', exact: true }).click()
  const finalizedStatus = page.getByRole('status', { name: '资料批次已完成' })
  await expect(finalizedStatus).toContainText('三类知识已正式化', { timeout: 30_000 })
  await finalizedStatus.scrollIntoViewIfNeeded()
  const desktopFormalizedWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopFormalizedPath, fullPage: false })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  await finalizedStatus.scrollIntoViewIfNeeded()
  await expect(finalizedStatus.locator('code')).toBeVisible()
  const mobileFormalizedWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileFormalizedPath, fullPage: false })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.reload()
  await page.getByRole('button', { name: '资料', exact: true }).click()
  await expect(page.getByText('正式蒸馏已验证入库').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: '查看知识批次', exact: true }).click()
  const recoveredStatus = page.getByRole('status', { name: '资料批次已完成' })
  await expect(recoveredStatus).toContainText('三类知识已正式化', { timeout: 30_000 })
  const recoveryReceiptId = (await recoveredStatus.locator('span').first().innerText()).trim()
  const recoveryReceiptHash = (await recoveredStatus.locator('code').first().innerText()).trim()
  expect(recoveryReceiptId).toMatch(/^knowledge-formalization-/)
  expect(recoveryReceiptHash).toMatch(/^[a-f0-9]{64}$/)

  const sourceReceiptResponse = await request.get(
    `${runtimeUrl}/api/v1/source-imports/${encodeURIComponent(sourceImportId)}/execution-receipt`,
  )
  const candidatesResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/candidates?source_import_id=${encodeURIComponent(sourceImportId)}`,
  )
  const formalizationResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/source-imports/${encodeURIComponent(sourceImportId)}/formalization`,
  )
  const finalRuntimeSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/distillation/invocations/${encodeURIComponent(invocationId)}`,
  )
  expect(sourceReceiptResponse.ok()).toBeTruthy()
  expect(candidatesResponse.ok()).toBeTruthy()
  expect(formalizationResponse.ok()).toBeTruthy()
  expect(finalRuntimeSnapshotResponse.ok()).toBeTruthy()
  const sourceReceipt = await sourceReceiptResponse.json()
  const candidates = await candidatesResponse.json() as {
    candidate_set_hash: string
    candidates: Array<{
      candidate_id: string
      source_card_index: number
      source_draft_hash: string
      card_type: KnowledgeCardType
      status: string
      runtime_distillation_invocation_id?: string | null
    }>
  }
  const formalization = await formalizationResponse.json() as {
    status: string
    receipt: {
      schema: string
      receipt_hash: string
      formalization_receipt_id: string
    } | null
  }
  const finalRuntimeSnapshot = await finalRuntimeSnapshotResponse.json() as RuntimeDeepInvocationSnapshot
  const runtimeDeep = assertRuntimeDeepSnapshot(finalRuntimeSnapshot, invocationId)
  const runtimeCandidateSetHash = finalRuntimeSnapshot.invocation.candidate_set_hash
  assertHexHash(runtimeCandidateSetHash, 'runtime candidate set hash')
  expect(candidates.candidates).toHaveLength(3)
  const canonicalCandidates = [...candidates.candidates].sort(
    (left, right) => left.source_card_index - right.source_card_index,
  )
  expect(canonicalCandidates.map((candidate) => `${candidate.source_card_index}/${candidate.card_type}`)).toEqual([
    '0/work',
    '1/voice',
    '2/validator',
  ])
  expect(canonicalCandidates.map((candidate) => candidate.runtime_distillation_invocation_id)).toEqual([
    invocationId,
    invocationId,
    invocationId,
  ])
  expect(canonicalCandidates.map((candidate) => candidate.source_draft_hash)).toEqual([
    runtimeCandidateSetHash,
    runtimeCandidateSetHash,
    runtimeCandidateSetHash,
  ])
  expect(candidates.candidates.every((candidate) => candidate.status === 'promoted')).toBe(true)
  expect(new Set(candidates.candidates.map((candidate) => candidate.card_type))).toEqual(
    new Set<KnowledgeCardType>(['work', 'voice', 'validator']),
  )
  expect(formalization.status).toBe('verified')
  expect(formalization.receipt).toBeTruthy()
  expect(formalization.receipt?.schema).toBe('zhimeng.knowledge-formalization-receipt.v2')
  expect(recoveryReceiptId).toBe(formalization.receipt?.formalization_receipt_id)
  expect(recoveryReceiptHash).toBe(formalization.receipt?.receipt_hash)

  await writeFile(
    acceptancePath,
    JSON.stringify({
      schema: 'zhimeng.o2b-corrective-acceptance.v2',
      source_import_id: sourceImportId,
      project_id: projectId,
      local_url: page.url(),
      screenshots: {
        desktop_runtime_deep_invocation: desktopRuntimeDeepPath,
        desktop_review_overview: desktopReviewOverviewPath,
        desktop_review_action: desktopReviewActionPath,
        mobile_candidate_overview: mobileCandidateOverviewPath,
        mobile_review_action: mobileReviewActionPath,
        desktop_formalized: desktopFormalizedPath,
        mobile_formalized: mobileFormalizedPath,
      },
      dom_widths: {
        desktop_review_overview: desktopOverviewWidths,
        desktop_review_action: desktopActionWidths,
        mobile_candidate_overview: mobileOverviewWidths,
        mobile_review_action: mobileActionWidths,
        desktop_formalized: desktopFormalizedWidths,
        mobile_formalized: mobileFormalizedWidths,
      },
      source_receipt: {
        status: sourceReceipt.lineage_status,
        receipt_hash: sourceReceipt.receipt?.receipt_hash,
      },
      runtime_deep: runtimeDeep,
      candidates: {
        count: candidates.candidates.length,
        canonical: canonicalCandidates.map((candidate) => ({
          candidate_id: candidate.candidate_id,
          source_card_index: candidate.source_card_index,
          card_type: candidate.card_type,
          status: candidate.status,
          runtime_distillation_invocation_id: candidate.runtime_distillation_invocation_id,
          source_draft_hash: candidate.source_draft_hash,
        })),
        types: canonicalCandidates.map((candidate) => candidate.card_type),
        statuses: canonicalCandidates.map((candidate) => candidate.status),
        runtime_distillation_invocation_ids: canonicalCandidates
          .map((candidate) => candidate.runtime_distillation_invocation_id),
        candidate_set_hash: candidates.candidate_set_hash,
      },
      formalization: {
        status: formalization.status,
        receipt_schema: formalization.receipt?.schema,
        receipt_id: formalization.receipt?.formalization_receipt_id,
        receipt_hash: formalization.receipt?.receipt_hash,
      },
      reload_recovery: {
        visible: true,
        receipt_id: recoveryReceiptId,
        receipt_hash: recoveryReceiptHash,
      },
      o2b_only: true,
      created_at: new Date().toISOString(),
    }, null, 2),
    'utf-8',
  )
})
