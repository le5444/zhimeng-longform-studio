import { expect, test } from '@playwright/test'
import { cp, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  distillationCoverageFixture as distillationCoverage,
  distillationHealthFixture as distillationHealth,
} from '../src/test/distillationFixture'
import {
  knowledgeCandidateFixture,
  knowledgeScopedCardListFixture,
} from '../src/test/knowledgeFixture'
import {
  sourceImportFormalizationFixture,
  sourceImportJobFixture,
  sourceImportReceiptFixture,
  tomatoCandidateFixture as sourceCandidate,
} from '../src/test/sourceImportFixture'
import { agentCandidateOutputFixture } from '../src/test/agentCandidateOutputFixture'
import { agentWorkflowStateFixture } from '../src/test/agentWorkflowFixture'
import {
  providerRecoveryDecisionFixture,
  providerRecoveryFixture,
} from '../src/test/providerRecoveryFixture'
import { canonicalJsonHash } from '../src/lib/canonicalJson'

const evidenceDir = path.resolve(
  process.env.PLAYWRIGHT_EVIDENCE_DIR ?? path.resolve(process.cwd(), 'test-results/evidence'),
)
const runtimeUrl = process.env.PLAYWRIGHT_RUNTIME_URL
  ?? `http://127.0.0.1:${process.env.PLAYWRIGHT_RUNTIME_PORT ?? '8789'}`
const providerUrl = process.env.PLAYWRIGHT_PROVIDER_URL
  ?? `http://127.0.0.1:${process.env.PLAYWRIGHT_PROVIDER_PORT ?? '18790'}`

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const sourceProjectRoot = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/minimal-novel',
)
const localSourcePath = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/local-source/archive-night-sample.txt',
)

interface IsolatedProjectFixture {
  rootPath: string
  prosePath: string
  sourceProsePath: string
  originalProseBytes: Buffer
}

const agentChangeSetTest = test.extend<{ isolatedProject: IsolatedProjectFixture }>({
  isolatedProject: async ({ page }, provide) => {
    const sandboxParent = path.join(path.dirname(sourceProjectRoot), 'test-results')
    await mkdir(sandboxParent, { recursive: true })
    const sandboxRoot = await mkdtemp(path.join(sandboxParent, 'workbench-o1-'))
    const rootPath = path.join(sandboxRoot, 'minimal-novel')
    const relativeProsePath = path.join('05_正文内容', '第一卷', 'CH001_正文.md')
    const prosePath = path.join(rootPath, relativeProsePath)
    const sourceProsePath = path.join(sourceProjectRoot, relativeProsePath)

    try {
      await cp(sourceProjectRoot, rootPath, {
        recursive: true,
        errorOnExist: true,
        preserveTimestamps: true,
      })
      const originalProseBytes = await readFile(prosePath)

      // Vite is configured with the read-only source fixture. Redirect project
      // inspection to this test's copy so every later bound Runtime operation,
      // including Agent tools and ChangeSet writeback, uses the isolated root.
      await page.route('**/api/v1/projects/inspect', async (route) => {
        if (route.request().method() !== 'POST') {
          await route.continue()
          return
        }
        await route.continue({
          postData: JSON.stringify({ root_path: rootPath }),
        })
      })

      await provide({
        rootPath,
        prosePath,
        sourceProsePath,
        originalProseBytes,
      })
    } finally {
      await rm(sandboxRoot, { recursive: true, force: true })
    }
  },
})

test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true })
})

interface ChangeSetFixture {
  title: string
  afterText: string
}

async function mockSourceImports(page: import('@playwright/test').Page) {
  const sourceImportId = sourceImportJobFixture().import_id
  const job = (status: 'downloading' | 'awaiting_review') => sourceImportJobFixture({
    importId: sourceImportId,
    candidate: sourceCandidate,
    status,
    percent: status === 'awaiting_review' ? 100 : 12,
    savedChapters: status === 'awaiting_review' ? 412 : 48,
    totalChapters: 412,
  })
  let created = false
  let listReadsAfterCreate = 0
  await page.route('**/api/v1/source-imports**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathName = url.pathname
    if (pathName.endsWith('/tomato/search') && request.method() === 'POST') {
      const alternate = { ...sourceCandidate, index: 2, book_id: '987654322', author: '闻舟工作室' }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema: 'zhimeng.tomato-search.v1',
          query: sourceCandidate.title,
          resolution: 'selection_required',
          candidates: [sourceCandidate, alternate],
          selected_candidate: null,
        }),
      })
      return
    }
    if (
      pathName === `/api/v1/source-imports/${sourceImportId}/execution-receipt`
      && request.method() === 'GET'
    ) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(sourceImportReceiptFixture({ importId: sourceImportId })),
      })
      return
    }
    if (pathName === '/api/v1/source-imports' && request.method() === 'POST') {
      created = true
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify(job('downloading')),
      })
      return
    }
    if (pathName === '/api/v1/source-imports' && request.method() === 'GET') {
      const imports = created
        ? [job(listReadsAfterCreate++ === 0 ? 'downloading' : 'awaiting_review')]
        : []
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ schema: 'zhimeng.source-import-list.v1', imports }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/v1/knowledge/source-imports/**', async (route) => {
    const request = route.request()
    const pathName = new URL(request.url()).pathname
    if (
      pathName === `/api/v1/knowledge/source-imports/${sourceImportId}/formalization`
      && request.method() === 'GET'
    ) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(sourceImportFormalizationFixture({
          importId: sourceImportId,
          status: 'awaiting_review',
        })),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
}

async function expectSourceLineageEvidence(page: import('@playwright/test').Page) {
  const evidence = page.getByLabel('执行与正式化收据')
  const receipt = sourceImportReceiptFixture()
  await expect(evidence).toBeVisible()
  await expect(
    evidence.locator('dt').filter({ hasText: 'lineage' }).locator('..').locator('dd'),
  ).toHaveText('verified')
  await expect(
    evidence.locator('dt').filter({ hasText: 'formalization' }).locator('..').locator('dd'),
  ).toHaveText('awaiting_review')
  await expect(evidence.getByText(`receipt_id · ${receipt.receipt?.receipt_id}`)).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
}

async function mockDistillationPreview(page: import('@playwright/test').Page) {
  await page.route('**/api/v1/distillation/**', async (route) => {
    const pathName = new URL(route.request().url()).pathname
    if (pathName.endsWith('/health')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(distillationHealth),
      })
      return
    }
    if (pathName.endsWith('/coverage') && route.request().method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(distillationCoverage),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
}

async function mockKnowledge(page: import('@playwright/test').Page) {
  await page.route('**/api/v1/projects/*/knowledge/**', async (route) => {
    const request = route.request()
    const pathName = new URL(request.url()).pathname
    if (pathName.endsWith('/knowledge/candidates') && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema: 'zhimeng.knowledge-candidate-list.v2',
          candidates: [knowledgeCandidateFixture],
          candidate_set_hash: 'b'.repeat(64),
        }),
      })
      return
    }
    if (pathName.endsWith('/knowledge/cards') && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(knowledgeScopedCardListFixture),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
}

async function createReviewChangeSet(
  request: import('@playwright/test').APIRequestContext,
  fixture: ChangeSetFixture,
) {
  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`, {
    maxRetries: 2,
  })
  expect(projectsResponse.ok()).toBeTruthy()
  const projects = await projectsResponse.json()
  const project = projects.projects.find((item: { display_name: string }) => (
    item.display_name === 'minimal-novel'
  ))
  expect(project).toBeTruthy()

  const snapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${project.project_id}/inspect`,
  )
  expect(snapshotResponse.ok()).toBeTruthy()
  const snapshot = await snapshotResponse.json()
  const prose = snapshot.files.find((item: { relative_path: string }) => (
    item.relative_path.endsWith('CH001_正文.md')
  ))
  expect(prose).toBeTruthy()
  const contentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${project.project_id}/files/${prose.file_id}`,
  )
  expect(contentResponse.ok()).toBeTruthy()
  const content = await contentResponse.json()
  const afterContent = content.content.replace(
    '雨落在旧档案馆的玻璃窗上。',
    fixture.afterText,
  )
  expect(afterContent).not.toBe(content.content)

  const createResponse = await request.post(
    `${runtimeUrl}/api/v1/projects/${project.project_id}/change-sets`,
    {
      data: {
        base_revision: snapshot.project.revision,
        base_fingerprint: snapshot.project.fingerprint,
        title: fixture.title,
        summary: '只创建并审阅候选改动，不写入测试项目。',
        origin: 'manual',
        changes: [{
          operation: 'update',
          file_id: prose.file_id,
          after_content: afterContent,
          rationale: '把开场的声音和压力提前到第一句。',
        }],
      },
    },
  )
  expect(createResponse.status()).toBe(201)
}

async function reviewAndConfirmContext(
  page: import('@playwright/test').Page,
  routeEvidencePath?: string,
  writerMode: 'repair_only' | 'author_review_change_proposal' = 'repair_only',
) {
  await page.getByRole('button', { name: '编译上下文' }).click()
  const panel = page.locator('.context-pack-panel')
  await expect(panel).toBeVisible({ timeout: 15_000 })
  await expect(panel.getByText('NLCL 路由收据')).toBeVisible()
  await expect(panel.getByText('尚未授权正式起稿')).toBeVisible()
  await expect(panel.getByText(/novel-creation-suite\.project-audit/)).toBeVisible()
  await expect(panel.getByText(/novel-creation-suite\.prewrite-gate/)).toBeVisible()
  await expect(panel.getByText(/novel-creation-suite\.creation-handoff/)).toBeVisible()
  await expect(panel.getByText('等待对应受管 Creation 调用')).toHaveCount(3)
  await expect(panel.getByText('写前运行包')).toBeVisible()
  await expect(panel.getByText('实际发送给模型的消息')).toBeVisible()
  const reviewedHash = await panel
    .locator('.context-hashes div')
    .filter({ hasText: 'provider_messages_hash' })
    .locator('code')
    .textContent()
  expect(reviewedHash).toMatch(/^[0-9a-f]{64}$/)
  if (routeEvidencePath) {
    await page.screenshot({ path: routeEvidencePath, fullPage: false })
  }
  if (writerMode === 'author_review_change_proposal') {
    await panel.getByRole('radio', { name: '正文候选' }).click()
    await expect(panel.getByText('生成正文改动候选，仍需作者审阅后写回')).toBeVisible()
  }
  await panel.getByRole('button', {
    name: writerMode === 'author_review_change_proposal' ? '确认并生成候选' : '确认并运行',
  }).click()
  return reviewedHash as string
}

type KnowledgeCardType = 'work' | 'voice' | 'validator'

async function fillKnowledgeText(
  editor: import('@playwright/test').Locator,
  label: string,
  value: string,
) {
  await editor.getByRole('textbox', { name: label, exact: true }).fill(value)
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

  for (const section of ['路由与安全边界', '可执行机制', '读者效果', '验证与评分']) {
    await editor.locator('summary').filter({ hasText: section }).click()
  }

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
  await fillKnowledgeText(editor, '效果证据', '三个连续章节都能从人物决定追踪到资源变化和下一次冲突。')
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
      `作者已记录 ${label} 的可复核样本，并确认规则在不复制来源表达时仍然成立。`,
    )
  }
  const scores: Array<[string, string]> = [
    ['证据', '0.78'],
    ['具体', '0.82'],
    ['生成', '0.84'],
    ['检索', '0.76'],
    ['风险', '0.12'],
  ]
  for (const [label, value] of scores) {
    await editor.getByRole('spinbutton', { name: label, exact: true }).fill(value)
  }
}

async function fillWorkKnowledgeReview(editor: import('@playwright/test').Locator) {
  await fillKnowledgeText(editor, '前提模式', '受限人物为获得新的行动空间，必须先接受一个不可逆代价。')
  await fillKnowledgeText(editor, '核心承诺', '每次选择都会兑现眼前收益，同时生成更高一级且可追溯的义务。')
  await fillKnowledgeText(editor, '开篇合同', '先展示一项可见损失，再给出机会有限且伴随风险的选择。')
  await fillKnowledgeText(editor, '成长路线', '把当前资源与关系压力外化\n迫使人物作出后果明确的选择\n兑现收益并生成更高一级压力')
  await fillKnowledgeText(editor, '循环场景', '资源受限 -> 风险试探 -> 阶段兑现 -> 新义务压回人物')
  await fillKnowledgeText(editor, '迁移边界', '不得复制来源人物名称\n不得复制来源世界设定壳\n不得复制来源完整场景顺序')
}

async function fillVoiceKnowledgeReview(editor: import('@playwright/test').Locator) {
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

async function reviewAndPromoteKnowledgeCandidate(
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
  await editor.getByRole('button', { name: '保存正式复核', exact: true }).click()
  const promote = editor.getByRole('button', { name: '晋升正式卡', exact: true })
  await expect(promote).toBeEnabled({ timeout: 20_000 })
  await promote.click()
  await expect(candidateTab).toContainText('待整批正式化', { timeout: 20_000 })
}

test('desktop workbench renders a real read-only project snapshot', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')

  await expect(page).toHaveTitle('织梦作者工作台')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('雨落在旧档案馆的玻璃窗上。')).toBeVisible()
  await expect(page.getByText('project rev 1')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Agent', exact: true })).toBeEnabled()
  await expect(page.locator('.ProseMirror')).toHaveAttribute('contenteditable', 'false')
  await expect(page.getByLabel('作品卷章树')).toBeVisible()
  await expect(page.getByLabel('章节正文编辑器')).toBeVisible()
  await expect(page.getByLabel('写作检查器')).toBeVisible()

  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    height: window.innerHeight,
    scrollHeight: document.documentElement.scrollHeight,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)
  expect(viewport.scrollHeight).toBeLessThanOrEqual(viewport.height)

})

test('Agent streams early, replays after refresh, and renders Markdown safely', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const composer = page.getByLabel('给 Writing Agent 的指令')
  await expect(composer).toBeEnabled({ timeout: 10_000 })
  const completedReplies = page.locator('.message-agent').filter({
    hasText: '第二段保留黄铜钥匙。',
  })
  const completedReplyCount = await completedReplies.count()
  await composer.fill('请收紧开头，但保留黄铜钥匙。')
  const reviewedHash = await reviewAndConfirmContext(
    page,
    path.join(evidenceDir, 'nlcl-route-review-desktop-1440x900.png'),
  )

  const streamingReply = page.locator('.message-streaming')
  await expect(streamingReply).toContainText('第一段先收紧。', { timeout: 15_000 })
  await expect(streamingReply).not.toContainText('第二段保留黄铜钥匙。')
  await page.reload()

  await expect(completedReplies).toHaveCount(completedReplyCount + 1, { timeout: 15_000 })
  const latestReply = completedReplies.last()
  await expect(latestReply).toContainText('第一段先收紧。')
  await expect(latestReply).toContainText('第二段保留黄铜钥匙。')
  await expect(latestReply.getByText('安全代码块')).toBeVisible()
  await expect(page.locator('.conversation script')).toHaveCount(0)
  expect(await page.evaluate(() => (window as typeof window & { __agentXss?: boolean }).__agentXss)).toBeUndefined()
  await expect(composer).toBeEnabled({ timeout: 30_000 })

  const providerReceipt = await request.get(`${providerUrl}/last-request`)
  expect(providerReceipt.ok()).toBeTruthy()
  const providerRequest = await providerReceipt.json() as {
    messages: Array<Record<string, unknown>>
    provider_messages_hash: string
  }

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`)
  const projectId = (await projectsResponse.json()).projects[0].project_id as string
  const sessionsResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
  )
  const sessionId = (await sessionsResponse.json()).sessions[0].session_id as string
  const runsResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
  )
  const runs = (await runsResponse.json()).runs as Array<{
    objective: string
    ordinal: number
    run_id: string
  }>
  const run = runs
    .filter((candidate) => candidate.objective === '请收紧开头，但保留黄铜钥匙。')
    .sort((left, right) => right.ordinal - left.ordinal)[0]
  expect(run).toBeTruthy()
  const packResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(run.run_id)}/context-pack`,
  )
  const runPack = await packResponse.json()
  expect(runPack.consumed_by_run_id).toBe(run.run_id)
  expect(runPack.pack.provider_messages_hash).toBe(reviewedHash)
  expect(
    providerRequest.messages.slice(0, runPack.pack.exact_provider_messages.length),
  ).toEqual(runPack.pack.exact_provider_messages)
  expect(providerRequest.provider_messages_hash).not.toBe(reviewedHash)
  type WorkflowToolResult = {
    status?: string
    output?: {
      domain_status?: string
      audit?: { health?: string }
      prewrite_gate?: { gate_status?: string; formal_draft_allowed?: boolean }
      creation_handoff?: { handoff_status?: string; formal_draft_allowed?: boolean }
    }
  }
  const workflowToolResults = providerRequest.messages.flatMap((message) => {
    if (message.role !== 'tool') return []
    try {
      return [JSON.parse(String(message.content ?? '{}')) as WorkflowToolResult]
    } catch {
      return []
    }
  })
  const automaticAudit = workflowToolResults.find((result) => result.output?.audit)
  if (!automaticAudit) throw new Error('Missing managed project-audit ToolResult')
  expect(automaticAudit.status).toBe('succeeded')
  expect(automaticAudit.output?.domain_status).toBe('blocked')
  expect(automaticAudit.output?.audit?.health).toBe('blocked')
  const automaticPrewrite = workflowToolResults.find((result) => result.output?.prewrite_gate)
  if (!automaticPrewrite) throw new Error('Missing managed prewrite-gate ToolResult')
  expect(automaticPrewrite.status).toBe('succeeded')
  expect(automaticPrewrite.output?.prewrite_gate?.gate_status).toBe('blocked')
  expect(automaticPrewrite.output?.prewrite_gate?.formal_draft_allowed).toBe(false)
  const automaticHandoff = workflowToolResults.find((result) => result.output?.creation_handoff)
  if (!automaticHandoff) throw new Error('Missing managed creation-handoff ToolResult')
  expect(automaticHandoff.status).toBe('succeeded')
  expect(automaticHandoff.output?.domain_status).toBe('blocked')
  expect(automaticHandoff.output?.creation_handoff?.handoff_status).toBe('blocked')
  expect(automaticHandoff.output?.creation_handoff?.formal_draft_allowed).toBe(false)
  const activitiesResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(run.run_id)}/activities`,
  )
  const receipt = (await activitiesResponse.json()).activities.find(
    (activity: { kind: string; payload: { artifact_type?: string } }) => (
      activity.kind === 'artifact' && activity.payload.artifact_type === 'context_injection_receipt'
    ),
  )
  expect(receipt.payload.content_hash).toBe(reviewedHash)

  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)
  await page.getByRole('tab', { name: /Timeline/ }).click()
  await expect(page.locator('.timeline-table')).toBeVisible()
  await expect(page.getByText('运行状态').first()).toBeVisible()
  await page.getByRole('button', { name: '收起运行抽屉' }).click()
  await page.screenshot({
    path: path.join(evidenceDir, 'agent-stream-recovery-desktop-1440x900.png'),
    fullPage: false,
  })
})

test('paused durable workflow exposes explicit Resume on desktop and mobile', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`)
  expect(projectsResponse.ok()).toBeTruthy()
  const projects = await projectsResponse.json() as {
    projects: Array<{ project_id: string }>
  }
  const projectItem = projects.projects[0]
  expect(projectItem).toBeTruthy()
  const inspectionResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectItem.project_id)}/inspect`,
  )
  expect(inspectionResponse.ok()).toBeTruthy()
  const inspection = await inspectionResponse.json() as {
    project: { project_id: string; fingerprint: string; revision: number }
  }
  const project = inspection.project

  const runId = 'run-playwright-workflow-0001'
  const sessionId = 'session-playwright-workflow-0001'
  const now = '2026-07-21T10:00:00Z'
  const projectRevision = {
    project_id: project.project_id,
    revision: project.revision,
    fingerprint: project.fingerprint,
    observed_at: now,
  }
  const workflow = agentWorkflowStateFixture({
    runId,
    projectId: project.project_id,
    runStatus: 'paused',
  })
  const run = {
    schema: 'zhimeng.execution-run.v1',
    run_id: runId,
    session_id: sessionId,
    ordinal: 1,
    status: 'paused',
    objective: '检查设定后继续本章写作',
    created_at: now,
    updated_at: now,
    started_at: now,
    completed_at: null,
    executor: {},
    capabilities: {},
    project_revision: projectRevision,
    correlation_id: 'correlation-playwright-workflow-0001',
    causation_id: null,
    last_activity_sequence: 2,
    checkpoint_id: null,
    error: null,
  }
  const session = {
    schema: 'zhimeng.agent-session.v1',
    session_id: sessionId,
    project_revision: projectRevision,
    title: 'M3i 恢复验证',
    status: 'active',
    revision: 1,
    executor: {},
    capabilities: {},
    correlation_id: 'correlation-playwright-workflow-0001',
    last_run_ordinal: 1,
    last_message_ordinal: 0,
    created_at: now,
    updated_at: now,
  }
  const pausedActivity = {
    schema: 'zhimeng.activity-item.v1',
    activity_id: 'activity-playwright-workflow-0002',
    session_id: sessionId,
    run_id: runId,
    sequence: 2,
    created_at: now,
    project_revision: projectRevision,
    correlation_id: 'correlation-playwright-workflow-0001',
    causation_id: null,
    kind: 'run_state',
    payload: {
      status: 'paused',
      previous_status: 'running',
      detail: 'Runtime restarted before Provider dispatch',
    },
  }

  await page.route('**/api/v1/projects/*/agent/sessions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ sessions: [session] }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ messages: [], last_ordinal: 0 }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/runs*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ runs: [run] }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/context-packs*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema: 'zhimeng.context-pack-list.v1',
        session_id: sessionId,
        context_packs: [],
      }),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/activities*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema: 'zhimeng.agent-activity-list.v1',
        run_id: runId,
        activities: [pausedActivity],
        last_sequence: 2,
      }),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/workflow`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(workflow),
    })
  })

  await page.reload()
  await expect(page.getByText('工作流已暂停')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('button', { name: '继续工作流' })).toBeVisible()
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  await expect(page.getByText('已完成 1/3 步')).toBeVisible()
  await page.screenshot({
    path: path.join(evidenceDir, 'durable-workflow-resume-desktop-1440x900.png'),
    fullPage: false,
  })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByLabel('移动端工作区').getByRole('tab', { name: 'Agent' }).click()
  await expect(page.getByText('工作流已暂停')).toBeVisible()
  await expect(page.getByRole('button', { name: '继续工作流' })).toBeVisible()
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  await page.screenshot({
    path: path.join(evidenceDir, 'durable-workflow-resume-mobile-390x844.png'),
    fullPage: false,
  })
})

test('unknown Provider result requires one CAS decision without replaying Provider', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`)
  expect(projectsResponse.ok()).toBeTruthy()
  const projects = await projectsResponse.json() as {
    projects: Array<{ project_id: string }>
  }
  const projectItem = projects.projects[0]
  expect(projectItem).toBeTruthy()
  const inspectionResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectItem.project_id)}/inspect`,
  )
  expect(inspectionResponse.ok()).toBeTruthy()
  const inspection = await inspectionResponse.json() as {
    project: { project_id: string; fingerprint: string; revision: number }
  }
  const project = inspection.project

  const runId = 'run-0123456789abcdef0123456789'
  const sessionId = 'session-provider-recovery-playwright-0001'
  const now = '2026-07-22T03:00:00Z'
  const projectRevision = {
    project_id: project.project_id,
    revision: project.revision,
    fingerprint: project.fingerprint,
    observed_at: now,
  }
  const session = {
    schema: 'zhimeng.agent-session.v1',
    session_id: sessionId,
    project_revision: projectRevision,
    title: 'Provider 恢复验收',
    status: 'active',
    revision: 1,
    executor: {},
    capabilities: {},
    correlation_id: 'correlation-provider-recovery-playwright-0001',
    last_run_ordinal: 1,
    last_message_ordinal: 0,
    created_at: now,
    updated_at: now,
  }
  const run = {
    schema: 'zhimeng.execution-run.v1',
    run_id: runId,
    session_id: sessionId,
    ordinal: 1,
    status: 'interrupted',
    objective: '检查写前门禁后给出修复候选',
    created_at: now,
    updated_at: now,
    started_at: now,
    completed_at: now,
    executor: {},
    capabilities: {},
    project_revision: projectRevision,
    correlation_id: 'correlation-provider-recovery-playwright-0001',
    causation_id: null,
    last_activity_sequence: 1,
    checkpoint_id: null,
    error: null,
  }
  const interruptedActivity = {
    schema: 'zhimeng.activity-item.v1',
    activity_id: 'activity-provider-recovery-playwright-0001',
    session_id: sessionId,
    run_id: runId,
    sequence: 1,
    created_at: now,
    project_revision: projectRevision,
    correlation_id: 'correlation-provider-recovery-playwright-0001',
    causation_id: null,
    kind: 'run_state',
    payload: {
      status: 'interrupted',
      previous_status: 'running',
      detail: 'Runtime stopped after Provider dispatch began',
    },
  }
  const workflow = agentWorkflowStateFixture({
    runId,
    projectId: project.project_id,
    runStatus: 'interrupted',
    providerBoundary: true,
  })
  const baseCandidate = await agentCandidateOutputFixture()
  const { envelope_hash: _baseEnvelopeHash, ...baseCandidateMaterial } = baseCandidate
  const candidateMaterial = {
    ...baseCandidateMaterial,
    project_id: project.project_id,
    project_revision: project.revision,
    project_fingerprint: project.fingerprint,
  }
  const candidate = {
    ...candidateMaterial,
    envelope_hash: await canonicalJsonHash(candidateMaterial),
  }
  const basePendingRecovery = await providerRecoveryFixture()
  const { dossier_hash: _baseDossierHash, ...baseDossierMaterial } = basePendingRecovery.dossier
  const dossierMaterial = {
    ...baseDossierMaterial,
    project_id: project.project_id,
    project_revision: project.revision,
    project_fingerprint: project.fingerprint,
    candidate_output_id: candidate.candidate_output_id,
    candidate_output_hash: candidate.envelope_hash,
  }
  const dossier = {
    ...dossierMaterial,
    dossier_hash: await canonicalJsonHash(dossierMaterial),
  }
  const pendingRecovery = {
    ...basePendingRecovery,
    dossier,
  }
  const decidedRecovery = {
    ...pendingRecovery,
    decision: await providerRecoveryDecisionFixture(dossier, 'abandon_and_recompile'),
  }
  const providerBeforeResponse = await request.get(`${providerUrl}/last-request`)
  expect(providerBeforeResponse.ok()).toBeTruthy()
  const providerBefore = await providerBeforeResponse.json()
  let decisionPostCount = 0
  let decisionRequest: unknown = null

  await page.route('**/api/v1/projects/*/agent/sessions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ sessions: [session] }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/messages*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ messages: [], last_ordinal: 0 }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/runs*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        runs: [{ ...run, status: decisionPostCount > 0 ? 'failed' : 'interrupted' }],
      }),
    })
  })
  await page.route(`**/api/v1/agent/sessions/${sessionId}/context-packs*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema: 'zhimeng.context-pack-list.v1',
        session_id: sessionId,
        context_packs: [],
      }),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/activities*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema: 'zhimeng.agent-activity-list.v1',
        run_id: runId,
        activities: [interruptedActivity],
        last_sequence: 1,
      }),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/workflow`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...workflow,
        run_status: decisionPostCount > 0 ? 'failed' : 'interrupted',
      }),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/candidate-output`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(candidate),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/provider-recovery/decision`, async (route) => {
    decisionPostCount += 1
    decisionRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(decidedRecovery),
    })
  })
  await page.route(`**/api/v1/agent/runs/${runId}/provider-recovery`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(decisionPostCount > 0 ? decidedRecovery : pendingRecovery),
    })
  })

  await page.reload()
  const recoveryPanel = page.getByLabel('Provider 恢复').first()
  await expect(recoveryPanel).toBeVisible({ timeout: 10_000 })
  await expect(recoveryPanel).toContainText('远端结果')
  await expect(recoveryPanel).toContainText('未知')
  await expect(recoveryPanel).toContainText('unknown')
  await expect(recoveryPanel).toContainText('候选状态')
  await expect(recoveryPanel).toContainText('已持久化')
  await expect(recoveryPanel).toContainText('persisted')
  await expect(recoveryPanel).toContainText('Provider 重放')
  await expect(recoveryPanel).toContainText('0 次')
  await expect(recoveryPanel).toContainText('provider_replay_count=0')

  const recoveryActions = recoveryPanel.getByLabel('Provider 恢复操作')
  await expect(recoveryActions.getByRole('button')).toHaveCount(2)
  await expect(recoveryActions.getByRole('button', { name: /采纳已保存候选/ })).toBeEnabled()
  const abandonButton = recoveryActions.getByRole('button', { name: /放弃未知结果/ })
  await expect(abandonButton).toBeEnabled()
  await abandonButton.click()

  await expect.poll(() => decisionPostCount).toBe(1)
  expect(decisionRequest).toEqual({
    schema: 'zhimeng.provider-recovery-decision-request.v1',
    expected_dossier_revision: dossier.dossier_revision,
    expected_dossier_hash: dossier.dossier_hash,
    action: 'abandon_and_recompile',
  })
  await expect(recoveryPanel).toContainText('已放弃未知结果')
  await expect(recoveryPanel.getByLabel('Provider 恢复操作')).toHaveCount(0)
  await expect(recoveryPanel.getByRole('button', { name: /重新编译新 Run/ })).toBeVisible()

  const providerAfterResponse = await request.get(`${providerUrl}/last-request`)
  expect(providerAfterResponse.ok()).toBeTruthy()
  expect(await providerAfterResponse.json()).toEqual(providerBefore)
  expect(decisionPostCount).toBe(1)
  await page.screenshot({
    path: path.join(evidenceDir, 'provider-recovery-decision-desktop-1440x900.png'),
    fullPage: false,
  })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByLabel('移动端工作区').getByRole('tab', { name: 'Agent' }).click()
  await expect(page.getByLabel('Provider 恢复').first()).toContainText('已放弃未知结果')
  await expect(page.getByRole('button', { name: /重新编译新 Run/ }).first()).toBeVisible()
  const mobileDimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    page: document.documentElement.scrollWidth,
  }))
  expect(mobileDimensions.page).toBeLessThanOrEqual(mobileDimensions.viewport)
  await page.screenshot({
    path: path.join(evidenceDir, 'provider-recovery-decision-mobile-390x844.png'),
    fullPage: false,
  })
})

test('mobile Agent conversation remains usable without page overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await page.getByLabel('移动端工作区').getByRole('tab', { name: 'Agent' }).click()

  const composer = page.getByLabel('给 Writing Agent 的指令')
  await expect(composer).toBeEnabled({ timeout: 10_000 })
  const mobileReplies = page.locator('.message-agent').filter({
    hasText: '移动端完整结果已保存。',
  })
  const mobileReplyCount = await mobileReplies.count()
  await composer.fill('移动端验证：检查窄屏对话与代码块。')
  await reviewAndConfirmContext(
    page,
    path.join(evidenceDir, 'nlcl-route-review-mobile-390x844.png'),
  )
  await expect(mobileReplies).toHaveCount(mobileReplyCount + 1, { timeout: 30_000 })
  const latestMobileReply = mobileReplies.last()
  await expect(latestMobileReply).toContainText('移动端首段已到达。')
  await expect(latestMobileReply.getByText('移动安全代码块')).toBeVisible()

  await page.getByRole('button', { name: '查看执行收据' }).click()
  const mobileExecution = page.locator('.nlcl-execution-state')
  await expect(mobileExecution).toHaveCount(3, { timeout: 30_000 })
  await expect(mobileExecution.first()).toContainText('执行成功')
  await expect(mobileExecution.filter({ hasText: '领域状态：blocked' })).toHaveCount(3)
  await page.screenshot({
    path: path.join(evidenceDir, 'nlcl-route-execution-mobile-390x844.png'),
    fullPage: false,
  })
  await page.locator('#inspector-tab-agent').click()

  const dimensions = await page.evaluate(() => {
    const conversation = document.querySelector('.conversation')
    return {
      viewport: window.innerWidth,
      page: document.documentElement.scrollWidth,
      conversationClient: conversation?.clientWidth ?? 0,
      conversationScroll: conversation?.scrollWidth ?? 0,
    }
  })
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport)
  expect(dimensions.conversationScroll).toBeLessThanOrEqual(dimensions.conversationClient)
  await page.getByRole('tab', { name: /Timeline/ }).click()
  await expect(page.locator('.timeline-table')).toBeVisible()
  const drawerDimensions = await page.locator('.drawer-content').evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }))
  expect(drawerDimensions.scroll).toBeLessThanOrEqual(drawerDimensions.client)
  await page.getByRole('button', { name: '收起运行抽屉' }).click()
  await page.screenshot({
    path: path.join(evidenceDir, 'agent-stream-recovery-mobile-390x844.png'),
    fullPage: false,
  })
})

test('mobile workbench switches panes without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')

  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByLabel('章节正文编辑器').getByText('雨落在旧档案馆的玻璃窗上。', { exact: true })).toBeVisible()
  await expect(page.getByLabel('章节正文编辑器')).toBeVisible()
  const editorViewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(editorViewport.scrollWidth).toBeLessThanOrEqual(editorViewport.width)

  await page.getByRole('tab', { name: '章节' }).click()
  await expect(page.getByLabel('作品卷章树')).toBeVisible()
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await expect(page.getByLabel('写作检查器')).toBeVisible()
  await expect(page.locator('.runtime-notice').getByText('Runtime 已就绪')).toBeVisible({ timeout: 10_000 })
})

test('compact tablet viewport uses pane switching instead of clipping inspector', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')

  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  const inspector = page.getByLabel('写作检查器')
  await expect(inspector).toBeVisible()
  const box = await inspector.boundingBox()
  expect(box).not.toBeNull()
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(800)
})

test('desktop reviews a real Runtime ChangeSet without writing the fixture', async ({ page, request }) => {
  const title = '桌面验收：收紧档案馆开场'
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  await createReviewChangeSet(request, {
    title,
    afterText: '雨点砸在旧档案馆的玻璃窗上。',
  })

  await page.getByRole('tab', { name: '改动', exact: true }).click()
  const row = page.locator('.changeset-row').filter({ hasText: title })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.getByText(/雨点砸在旧档案馆的玻璃窗上/)).toBeVisible()
  await page.getByRole('button', { name: /接受 .* Hunk 1/ }).click()
  await expect(page.getByText('可应用').first()).toBeVisible()
  await expect(page.getByRole('button', { name: '应用所选更改' })).toBeEnabled()

  await page.screenshot({
    path: path.join(evidenceDir, 'changeset-review-desktop-1440x900.png'),
    fullPage: false,
  })
})

test('mobile ChangeSet review stays within the inspector viewport', async ({ page, request }) => {
  const title = '移动验收：收紧档案馆开场'
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  await createReviewChangeSet(request, {
    title,
    afterText: '急雨敲在旧档案馆的玻璃窗上。',
  })

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: '改动', exact: true }).click()
  const row = page.locator('.changeset-row').filter({ hasText: title })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.getByText(/急雨敲在旧档案馆的玻璃窗上/)).toBeVisible()
  await page.getByRole('button', { name: /接受 .* Hunk 1/ }).click()
  await expect(page.getByRole('button', { name: '应用所选更改' })).toBeVisible()
  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)

  await page.screenshot({
    path: path.join(evidenceDir, 'changeset-review-mobile-390x844.png'),
    fullPage: false,
  })
})

agentChangeSetTest('Agent ChangeSet completes Gate, Apply, refresh, and byte-exact Rollback on an isolated project', async ({
  page,
  request,
  isolatedProject,
}) => {
  agentChangeSetTest.setTimeout(120_000)
  const objective = '工具链验证：读取第一章，只生成候选改动，不要写回项目。'
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`, {
    maxRetries: 2,
  })
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json()).projects.find(
    (candidate: { display_name: string }) => candidate.display_name === 'minimal-novel',
  ) as { project_id: string; root_path: string } | undefined
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id
  expect(path.resolve((project as { root_path: string }).root_path)).toBe(
    path.resolve(isolatedProject.rootPath),
  )

  const snapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(snapshotResponse.ok()).toBeTruthy()
  const snapshot = await snapshotResponse.json()
  const beforeProject = snapshot.project as { revision: number; fingerprint: string }
  const prose = snapshot.files.find(
    (file: { relative_path: string }) => file.relative_path.endsWith('CH001_正文.md'),
  ) as { file_id: string } | undefined
  expect(prose).toBeTruthy()
  const proseId = (prose as { file_id: string }).file_id
  const beforeResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(beforeResponse.ok()).toBeTruthy()
  const beforeContent = (await beforeResponse.json()).content as string
  expect(beforeContent).not.toContain('工具链验证句')
  await expect(page.locator('.ProseMirror')).not.toContainText('工具链验证句')

  const composer = page.getByLabel('给 Writing Agent 的指令')
  await expect(composer).toBeEnabled({ timeout: 10_000 })
  await composer.fill(objective)
  await reviewAndConfirmContext(page, undefined, 'author_review_change_proposal')

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: /Tools/ }).click()
  const toolCards = page.locator('.tool-activity-card')
  await expect(toolCards).toHaveCount(7, { timeout: 45_000 })
  await expect(composer).toBeEnabled({ timeout: 30_000 })
  await expect(toolCards.filter({ hasText: 'skill_execute' })).toHaveCount(3)
  await expect(toolCards.filter({ hasText: 'project_list_files' })).toHaveCount(1)
  await expect(toolCards.filter({ hasText: 'project_read_file' })).toHaveCount(1)
  await expect(toolCards.filter({ hasText: 'change_set_propose' })).toHaveCount(1)
  await expect(toolCards.filter({ hasText: 'candidate_output_submit' })).toHaveCount(1)
  await expect(toolCards.locator('.tool-status')).toHaveText([
    '执行成功',
    '执行成功',
    '执行成功',
    '执行成功',
    '执行成功',
    '执行成功',
    '执行成功',
  ])

  await page.getByLabel('运行详情').getByRole('tab', { name: /候选/ }).click()
  const writerTrace = page.getByRole('article', { name: 'Writer 任务与候选结果' })
  await expect(writerTrace).toContainText('正文候选任务')
  await expect(writerTrace).toContainText('author_review_change_proposal')
  await expect(writerTrace).toContainText('候选结果')
  await expect(writerTrace).toContainText('writer-candidate-result-')
  await expect(page.getByText('正文候选尚未写回')).toBeVisible()
  await expect(page.getByText(/writes_project_truth=false/)).toBeVisible()

  await page.getByRole('tab', { name: /Timeline/ }).click()
  await expect(page.locator('.timeline-tool_call')).toHaveCount(7)
  await expect(page.locator('.timeline-tool_result')).toHaveCount(7)
  const proposalActivity = page.locator('.timeline-change_proposal')
  await expect(proposalActivity).toHaveCount(1)
  await expect(proposalActivity).toContainText('工具链验证候选')

  const sessionsResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
  )
  expect(sessionsResponse.ok()).toBeTruthy()
  const sessionId = (await sessionsResponse.json()).sessions[0].session_id as string
  const runsResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
  )
  expect(runsResponse.ok()).toBeTruthy()
  const runs = (await runsResponse.json()).runs as Array<{
    objective: string
    ordinal: number
    run_id: string
  }>
  const run = runs
    .filter((candidate) => candidate.objective === objective)
    .sort((left, right) => right.ordinal - left.ordinal)[0]
  expect(run).toBeTruthy()
  const runId = (run as { run_id: string }).run_id
  const candidateResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/candidate-output`,
  )
  expect(candidateResponse.ok()).toBeTruthy()
  const candidateOutput = await candidateResponse.json() as {
    run_id: string
    formal_draft_allowed: boolean
    requires_author_review: boolean
    content: { body: string }
  }
  expect(candidateOutput).toMatchObject({
    run_id: runId,
    formal_draft_allowed: false,
    requires_author_review: true,
  })
  expect(candidateOutput.content.body).toBe('候选改动已生成，请审阅。')
  const activitiesResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/activities`,
  )
  expect(activitiesResponse.ok()).toBeTruthy()
  const activities = (await activitiesResponse.json()).activities as Array<{
    kind: string
    payload: { proposal_id?: string; tool_call_id?: string; tool_name?: string }
  }>
  expect(
    activities.filter((activity) => activity.kind === 'tool_call').map(
      (activity) => activity.payload.tool_name,
    ),
  ).toEqual([
    'skill_execute',
    'skill_execute',
    'skill_execute',
    'project_list_files',
    'project_read_file',
    'change_set_propose',
    'candidate_output_submit',
  ])
  const proposal = activities.find((activity) => activity.kind === 'change_proposal')
  expect(proposal?.payload.proposal_id).toBeTruthy()
  const changeSetId = proposal?.payload.proposal_id as string
  const changeSetResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}`,
  )
  expect(changeSetResponse.ok()).toBeTruthy()
  const changeSet = await changeSetResponse.json() as {
    change_set_id: string
    title: string
    source_run_id: string | null
    source_tool_call_id: string | null
  }
  expect(changeSet.change_set_id).toBe(changeSetId)
  expect(changeSet.title).toBe('工具链验证候选')
  expect(changeSet.source_run_id).toBe(runId)
  expect(changeSet.source_tool_call_id).toBeTruthy()

  const afterResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(afterResponse.ok()).toBeTruthy()
  expect((await afterResponse.json()).content).toBe(beforeContent)
  await expect(page.locator('.ProseMirror')).not.toContainText('工具链验证句')

  await proposalActivity.getByRole('button', { name: '审阅改动' }).click()
  await expect(page.getByRole('tab', { name: '改动', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('heading', { name: '工具链验证候选', exact: true })).toBeVisible()
  const selectedChangeSet = page.locator('.changeset-row[aria-current="true"]')
  await expect(selectedChangeSet).toContainText('工具链验证候选')
  const provenance = page.getByLabel('ChangeSet 来源')
  await expect(provenance.getByTitle(runId)).toBeVisible()
  await expect(provenance.getByTitle(changeSet.source_tool_call_id as string)).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Diff 1', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('main').getByText('工具链验证句。', { exact: true })).toBeVisible()

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tab', { name: '正文', exact: true }).click()
  const mobileEditor = page.locator('[data-mobile-pane="editor"]')
  await expect(mobileEditor).toBeVisible()
  await expect(mobileEditor.getByRole('tab', { name: 'Diff 1', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(mobileEditor.getByText('工具链验证句。', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: path.join(evidenceDir, 'agent-changeset-diff-mobile-390x844.png'),
    fullPage: false,
  })

  await mobileEditor.getByRole('tab', { name: /Agent 候选/ }).click()
  await expect(mobileEditor.getByText(/formal_draft_allowed=false/)).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: path.join(evidenceDir, 'agent-candidate-mobile-390x844.png'),
    fullPage: false,
  })
  await mobileEditor.getByRole('tab', { name: /Diff/ }).click()
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.getByRole('button', { name: /接受 .* Hunk 1/ }).click()
  const applyButton = page.getByRole('button', { name: '应用所选更改' })
  await expect(page.getByLabel('Gate 状态：未运行')).toBeVisible()
  await expect(applyButton).toBeDisabled()
  await expect(applyButton).toHaveAttribute('aria-describedby', 'changeset-gate-apply-note')

  await page.getByRole('button', { name: '运行 Gate' }).click()
  await expect(page.getByLabel('Gate 状态：通过')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/仅证明交付合同，不代表文学质量通过/)).toBeVisible()
  await expect(applyButton).toBeEnabled()

  const gateResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}/post-draft-gate`,
  )
  expect(gateResponse.ok()).toBeTruthy()
  const gate = await gateResponse.json() as {
    change_set_id: string
    review_revision: number
    receipt: {
      status: string
      candidate_hash: string
      quality_assessed: boolean
      formal_draft_allowed: boolean
    } | null
  }
  expect(gate.change_set_id).toBe(changeSetId)
  expect(gate.receipt?.status).toBe('passed')
  expect(gate.receipt?.candidate_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(gate.receipt?.quality_assessed).toBe(false)
  expect(gate.receipt?.formal_draft_allowed).toBe(false)

  const afterGateResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(afterGateResponse.ok()).toBeTruthy()
  expect((await afterGateResponse.json()).content).toBe(beforeContent)

  const afterGateSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(afterGateSnapshotResponse.ok()).toBeTruthy()
  const afterGateProject = (await afterGateSnapshotResponse.json()).project as {
    revision: number
    fingerprint: string
  }
  expect(afterGateProject).toMatchObject({
    revision: beforeProject.revision,
    fingerprint: beforeProject.fingerprint,
  })

  await applyButton.click()
  const rollbackButton = page.getByRole('button', { name: '回滚 ChangeSet' })
  await expect(rollbackButton).toBeVisible({ timeout: 30_000 })

  const appliedResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}`,
  )
  expect(appliedResponse.ok()).toBeTruthy()
  const applied = await appliedResponse.json() as {
    status: string
    review_revision: number
    writeback: {
      schema: string
      base_revision: number
      result_revision: number
      result_fingerprint: string
      outcome: string
      files: Array<{
        relative_path: string
        after_sha256: string | null
        accepted_hunk_ids: string[]
        rejected_hunk_ids: string[]
        backup_ref: string | null
      }>
    } | null
  }
  expect(applied.status).toBe('applied')
  expect(applied.writeback).toMatchObject({
    schema: 'zhimeng.writeback-manifest.v1',
    base_revision: beforeProject.revision,
    result_revision: beforeProject.revision + 1,
    outcome: 'applied',
  })
  expect(applied.writeback?.files).toHaveLength(1)
  expect(applied.writeback?.files[0]).toMatchObject({
    relative_path: expect.stringMatching(/CH001_正文\.md$/),
    accepted_hunk_ids: [expect.any(String)],
    rejected_hunk_ids: [],
    backup_ref: expect.any(String),
  })

  const appliedBytes = await readFile(isolatedProject.prosePath)
  expect(appliedBytes.equals(isolatedProject.originalProseBytes)).toBe(false)
  expect(appliedBytes.toString('utf8')).toContain('工具链验证句。')

  const appliedSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(appliedSnapshotResponse.ok()).toBeTruthy()
  const appliedSnapshot = await appliedSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
    files: Array<{ relative_path: string; sha256: string }>
  }
  const appliedProse = appliedSnapshot.files.find((file) => (
    file.relative_path.endsWith('CH001_正文.md')
  ))
  expect(appliedSnapshot.project).toMatchObject({
    revision: beforeProject.revision + 1,
    fingerprint: applied.writeback?.result_fingerprint,
  })
  expect(applied.writeback?.files[0].after_sha256).toBe(appliedProse?.sha256)
  await expect(page.locator('.statusbar').getByText(
    `project rev ${beforeProject.revision + 1}`,
    { exact: true },
  )).toBeVisible({ timeout: 30_000 })
  const draftTab = page.getByRole('tab', { name: '本稿', exact: true })
  const chapterEditor = page.getByLabel('章节正文编辑器')
  await expect(async () => {
    await draftTab.click()
    await expect(draftTab).toHaveAttribute('aria-selected', 'true')
    await expect(chapterEditor).toBeVisible()
  }).toPass({ timeout: 30_000 })
  await expect(chapterEditor).toContainText('工具链验证句。', {
    timeout: 30_000,
  })

  await page.screenshot({
    path: path.join(evidenceDir, 'agent-changeset-applied-desktop-1440x900.png'),
    fullPage: false,
  })

  await rollbackButton.click()
  await expect(page.getByText(`项目结果 revision ${beforeProject.revision + 2}`)).toBeVisible({
    timeout: 30_000,
  })

  const rolledBackResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}`,
  )
  expect(rolledBackResponse.ok()).toBeTruthy()
  const rolledBack = await rolledBackResponse.json() as {
    status: string
    rollback: {
      schema: string
      result_revision: number
      result_fingerprint: string
      outcome: string
      files: Array<{ relative_path: string; after_sha256: string | null }>
    } | null
  }
  expect(rolledBack.status).toBe('rolled_back')
  expect(rolledBack.rollback).toMatchObject({
    schema: 'zhimeng.writeback-manifest.v1',
    result_revision: beforeProject.revision + 2,
    outcome: 'rolled_back',
  })
  expect(rolledBack.rollback?.files).toHaveLength(1)

  const rolledBackBytes = await readFile(isolatedProject.prosePath)
  expect(rolledBackBytes).toEqual(isolatedProject.originalProseBytes)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(
    isolatedProject.originalProseBytes,
  )

  const rolledBackSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(rolledBackSnapshotResponse.ok()).toBeTruthy()
  const rolledBackSnapshot = await rolledBackSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
    files: Array<{ relative_path: string; sha256: string }>
  }
  const rolledBackProse = rolledBackSnapshot.files.find((file) => (
    file.relative_path.endsWith('CH001_正文.md')
  ))
  expect(rolledBackSnapshot.project).toMatchObject({
    revision: beforeProject.revision + 2,
    fingerprint: rolledBack.rollback?.result_fingerprint,
  })
  expect(rolledBack.rollback?.files[0].after_sha256).toBe(rolledBackProse?.sha256)
  await expect(page.locator('.statusbar').getByText(
    `project rev ${beforeProject.revision + 2}`,
    { exact: true },
  )).toBeVisible({ timeout: 30_000 })
  await expect(async () => {
    await draftTab.click()
    await expect(draftTab).toHaveAttribute('aria-selected', 'true')
    await expect(chapterEditor).toBeVisible()
  }).toPass({ timeout: 30_000 })
  await expect(chapterEditor).toContainText('雨落在旧档案馆的玻璃窗上。')
  await expect(chapterEditor).not.toContainText('工具链验证句。', {
    timeout: 30_000,
  })

  await page.screenshot({
    path: path.join(evidenceDir, 'agent-changeset-rolled-back-desktop-1440x900.png'),
    fullPage: false,
  })
})

test('managed Skill receipt and artifacts survive a Workbench refresh', async ({ page, request }) => {
  const objective = '受管 Skill 恢复验收：只读审计当前项目，不要写回正式项目。'
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`)
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json()).projects.find(
    (candidate: { display_name: string }) => candidate.display_name === 'minimal-novel',
  ) as { project_id: string } | undefined
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id
  const beforeResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(beforeResponse.ok()).toBeTruthy()
  const before = await beforeResponse.json() as {
    project: { revision: number; fingerprint: string }
  }

  const composer = page.getByLabel('给 Writing Agent 的指令')
  await expect(composer).toBeEnabled({ timeout: 10_000 })
  const auditReplies = page.locator('.message-agent').filter({
    hasText: '受管 Creation Suite 审计已完成。',
  })
  const auditReplyCount = await auditReplies.count()
  await composer.fill(objective)
  await reviewAndConfirmContext(page)
  await expect(auditReplies).toHaveCount(auditReplyCount + 1, {
    timeout: 30_000,
  })
  await expect(auditReplies.last()).toContainText('执行状态：succeeded；领域状态：blocked')
  await expect(composer).toBeEnabled({ timeout: 30_000 })

  await page.reload()
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await expect(auditReplies.last()).toContainText('执行状态：succeeded；领域状态：blocked', {
    timeout: 10_000,
  })

  await page.getByRole('button', { name: '查看执行收据' }).click()
  const routeExecution = page.locator('.nlcl-execution-state')
  await expect(routeExecution).toHaveCount(3)
  await expect(routeExecution.first()).toContainText('执行成功')
  await expect(routeExecution.filter({ hasText: '领域状态：blocked' })).toHaveCount(3)
  await expect(page.getByText('prewrite_package_hash')).toBeVisible()
  await expect(page.getByText('nlcl_execution_1_hash')).toBeVisible()
  await expect(page.getByText('nlcl_execution_2_hash')).toBeVisible()
  await expect(page.getByText('nlcl_execution_3_hash')).toBeVisible()
  await page.screenshot({
    path: path.join(evidenceDir, 'nlcl-route-execution-desktop-1440x900.png'),
    fullPage: false,
  })

  await page.getByRole('tab', { name: /Skills/ }).click()
  const skillCard = page.locator('.skill-activity-card')
  await expect(skillCard).toHaveCount(3)
  await expect(skillCard.filter({ hasText: 'novel-creation-suite.project-audit' })).toHaveCount(1)
  await expect(skillCard.filter({ hasText: 'novel-creation-suite.prewrite-gate' })).toHaveCount(1)
  await expect(skillCard.filter({ hasText: 'novel-creation-suite.creation-handoff' })).toHaveCount(1)
  await expect(skillCard.locator('.skill-status')).toHaveText(['执行成功', '执行成功', '执行成功'])
  const auditSkillCard = skillCard.filter({ hasText: 'novel-creation-suite.project-audit' })
  const executionState = auditSkillCard.locator('.skill-status-grid > div').filter({
    hasText: '执行状态',
  })
  await expect(executionState).toContainText('succeeded')
  const domainState = auditSkillCard.locator('.skill-status-grid > div').filter({
    hasText: '领域状态',
  })
  await expect(domainState).toContainText('阻塞')
  await expect(domainState).toContainText('blocked')
  await expect(auditSkillCard).toContainText('zhimeng.skill-execution-receipt.v1')
  await expect(auditSkillCard).toContainText('project_audit')
  await expect(skillCard.filter({ hasText: 'novel-creation-suite.prewrite-gate' })).toContainText('prewrite_gate')
  await expect(skillCard.filter({ hasText: 'novel-creation-suite.creation-handoff' })).toContainText('creation_handoff')

  await page.getByRole('tab', { name: /Artifacts/ }).click()
  const artifacts = page.locator('.artifact-item')
  await expect(artifacts.filter({ hasText: 'skill-execution-receipt.json' })).toHaveCount(3)
  await expect(artifacts.filter({ hasText: 'project-audit.json' })).toHaveCount(1)
  await expect(artifacts.filter({ hasText: 'prewrite-gate.json' })).toHaveCount(1)
  await expect(artifacts.filter({ hasText: 'creation-handoff.json' })).toHaveCount(1)
  await expect(artifacts.filter({ hasText: 'skill_execution_receipt' })).toHaveCount(3)
  await expect(artifacts.filter({ hasText: 'project_audit' })).toHaveCount(1)
  await expect(artifacts.filter({ hasText: 'prewrite_gate' })).toHaveCount(1)
  await expect(artifacts.filter({ hasText: 'creation_handoff' })).toHaveCount(1)

  const sessionsResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
  )
  expect(sessionsResponse.ok()).toBeTruthy()
  const sessionId = (await sessionsResponse.json()).sessions[0].session_id as string
  const runsResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
  )
  expect(runsResponse.ok()).toBeTruthy()
  const runs = (await runsResponse.json()).runs as Array<{
    objective: string
    ordinal: number
    run_id: string
  }>
  const run = runs
    .filter((candidate) => candidate.objective === objective)
    .sort((left, right) => right.ordinal - left.ordinal)[0]
  expect(run).toBeTruthy()
  const runId = (run as { run_id: string }).run_id
  const activitiesResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/activities`,
  )
  expect(activitiesResponse.ok()).toBeTruthy()
  const activities = (await activitiesResponse.json()).activities as Array<{
    kind: string
    payload: Record<string, unknown>
  }>
  expect(
    activities
      .filter((activity) => activity.kind === 'tool_call')
      .map((activity) => activity.payload.tool_name),
  ).toEqual([
    'skill_execute',
    'skill_execute',
    'skill_execute',
    'candidate_output_submit',
  ])
  const toolCall = activities.find((activity) => (
    activity.kind === 'tool_call' && activity.payload.tool_name === 'skill_execute'
  ))
  expect(toolCall).toBeTruthy()
  expect(toolCall?.payload.arguments).toEqual({
    skill_id: 'novel-creation-suite.project-audit',
    input: { schema: 'zhimeng.creation-project-audit-request.v1' },
  })
  const skillCall = activities.find((activity) => (
    activity.kind === 'skill_call'
    && activity.payload.skill_id === 'novel-creation-suite.project-audit'
  ))
  expect(skillCall).toBeTruthy()
  const skillPayload = skillCall?.payload as {
    status: string
    output: {
      receipt: {
        execution_status: string
        domain_status: string
        exit_code: number
        artifacts: Array<{ artifact_type: string }>
      }
    }
  }
  expect(skillPayload.status).toBe('succeeded')
  expect(skillPayload.output.receipt.execution_status).toBe('succeeded')
  expect(skillPayload.output.receipt.domain_status).toBe('blocked')
  expect(skillPayload.output.receipt.exit_code).toBe(1)
  expect(skillPayload.output.receipt.artifacts.map((artifact) => artifact.artifact_type)).toEqual([
    'project_audit',
  ])
  expect(
    activities
      .filter((activity) => activity.kind === 'artifact')
      .map((activity) => activity.payload.artifact_type),
  ).toEqual(expect.arrayContaining(['skill_execution_receipt', 'project_audit']))

  const prewriteToolCall = activities.find((activity) => (
    activity.kind === 'tool_call'
    && activity.payload.tool_name === 'skill_execute'
    && (activity.payload.arguments as { skill_id?: string })?.skill_id
      === 'novel-creation-suite.prewrite-gate'
  ))
  expect(prewriteToolCall).toBeTruthy()
  const prewriteArguments = prewriteToolCall?.payload.arguments as {
    input: {
      project_audit_execution_receipt_hash: string
      project_audit_skill_receipt_hash: string
      project_audit_tool_result_hash: string
    }
  }
  expect(prewriteArguments.input.project_audit_execution_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(prewriteArguments.input.project_audit_skill_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(prewriteArguments.input.project_audit_tool_result_hash).toMatch(/^[0-9a-f]{64}$/)
  const prewriteSkillCall = activities.find((activity) => (
    activity.kind === 'skill_call'
    && activity.payload.skill_id === 'novel-creation-suite.prewrite-gate'
  ))
  if (!prewriteSkillCall) {
    throw new Error('Missing prewrite gate skill call activity')
  }
  expect(
    (prewriteSkillCall.payload.output as { receipt: { artifacts: Array<{ artifact_type: string }> } }).receipt.artifacts,
  ).toEqual(expect.arrayContaining([
    expect.objectContaining({ artifact_type: 'prewrite_gate' }),
  ]))

  const handoffToolCall = activities.find((activity) => (
    activity.kind === 'tool_call'
    && activity.payload.tool_name === 'skill_execute'
    && (activity.payload.arguments as { skill_id?: string })?.skill_id
      === 'novel-creation-suite.creation-handoff'
  ))
  expect(handoffToolCall).toBeTruthy()
  const handoffArguments = handoffToolCall?.payload.arguments as {
    input: {
      prewrite_gate_execution_receipt_hash: string
      prewrite_gate_skill_receipt_hash: string
      prewrite_gate_tool_result_hash: string
      prewrite_gate_formal_draft_allowed: boolean
    }
  }
  expect(handoffArguments.input.prewrite_gate_execution_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(handoffArguments.input.prewrite_gate_skill_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(handoffArguments.input.prewrite_gate_tool_result_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(handoffArguments.input.prewrite_gate_formal_draft_allowed).toBe(false)
  const handoffSkillCall = activities.find((activity) => (
    activity.kind === 'skill_call'
    && activity.payload.skill_id === 'novel-creation-suite.creation-handoff'
  ))
  if (!handoffSkillCall) {
    throw new Error('Missing creation handoff skill call activity')
  }
  expect(
    (handoffSkillCall.payload.output as { receipt: { artifacts: Array<{ artifact_type: string }> } }).receipt.artifacts,
  ).toEqual(expect.arrayContaining([
    expect.objectContaining({ artifact_type: 'creation_handoff' }),
  ]))

  const packResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/context-pack`,
  )
  expect(packResponse.ok()).toBeTruthy()
  const contextPackId = (await packResponse.json()).pack.context_pack_id as string
  const routeExecutionResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`
      + '/nlcl-route/executions/1',
  )
  expect(routeExecutionResponse.ok()).toBeTruthy()
  const routeReceipt = await routeExecutionResponse.json()
  expect(routeReceipt.status).toBe('succeeded')
  expect(routeReceipt.receipt.domain_status).toBe('blocked')
  expect(routeReceipt.receipt.run_id).toBe(runId)
  expect(routeReceipt.receipt.receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  const prewriteExecutionResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`
      + '/nlcl-route/executions/2',
  )
  expect(prewriteExecutionResponse.ok()).toBeTruthy()
  const prewriteRouteReceipt = await prewriteExecutionResponse.json()
  expect(prewriteRouteReceipt.status).toBe('succeeded')
  expect(prewriteRouteReceipt.receipt.domain_status).toBe('blocked')
  expect(prewriteRouteReceipt.receipt.run_id).toBe(runId)
  const handoffExecutionResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`
      + '/nlcl-route/executions/3',
  )
  expect(handoffExecutionResponse.ok()).toBeTruthy()
  const handoffRouteReceipt = await handoffExecutionResponse.json()
  expect(handoffRouteReceipt.status).toBe('succeeded')
  expect(handoffRouteReceipt.receipt.domain_status).toBe('blocked')
  expect(handoffRouteReceipt.receipt.run_id).toBe(runId)

  const afterResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(afterResponse.ok()).toBeTruthy()
  const after = await afterResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(after.project).toEqual(before.project)
})

test('desktop imports a same-name Tomato result to an auditable draft', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockSourceImports(page)
  await page.goto('/')
  await expect(page.getByText('Runtime 就绪').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('tab', { name: '资料', exact: true }).click()
  await page.getByRole('searchbox', { name: '番茄小说' }).fill(sourceCandidate.title)
  await page.getByRole('button', { name: '下载并蒸馏' }).click()
  await expect(page.getByText('选择同名作品')).toBeVisible()
  await page.getByRole('button', {
    name: `选择《${sourceCandidate.title}》，作者 ${sourceCandidate.author}`,
    exact: true,
  }).click()

  await expect(page.getByText('蒸馏草案待复核').first()).toBeVisible({ timeout: 6_000 })
  await expect(page.getByText('8 张候选卡，尚未进入正式知识库')).toBeVisible()
  await expectSourceLineageEvidence(page)
  await expect(page.getByText('已正式入库')).toHaveCount(0)
  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)

  await page.screenshot({
    path: path.join(evidenceDir, 'tomato-import-desktop-1440x900.png'),
    fullPage: false,
  })
})

test('mobile Tomato import remains usable without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockSourceImports(page)
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: '资料', exact: true }).click()
  await page.getByRole('searchbox', { name: '番茄小说' }).fill(sourceCandidate.title)
  await page.getByRole('button', { name: '下载并蒸馏' }).click()
  await page.getByRole('button', {
    name: `选择《${sourceCandidate.title}》，作者 ${sourceCandidate.author}`,
    exact: true,
  }).click()
  await expect(page.getByText('蒸馏草案待复核').first()).toBeVisible({ timeout: 6_000 })
  await expectSourceLineageEvidence(page)

  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)
  const inspector = page.getByLabel('写作检查器')
  const box = await inspector.boundingBox()
  expect(box).not.toBeNull()
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390)

  await page.screenshot({
    path: path.join(evidenceDir, 'tomato-import-mobile-390x844.png'),
    fullPage: false,
  })
})

test('desktop Workbench imports a real local TXT through Runtime and remains usable on mobile', async ({ page }) => {
  const sourceHash = createHash('sha256').update(await readFile(localSourcePath)).digest('hex')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('Runtime 就绪').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('button', { name: '资料' }).click()
  await page.getByRole('button', { name: '本地 TXT / EPUB', exact: true }).click()
  await page.getByRole('textbox', { name: '本地 TXT / EPUB' }).fill(localSourcePath)
  await page.getByRole('button', { name: '校验并蒸馏' }).click()

  await expect(page.getByText('本地导入任务已创建')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('蒸馏草案待复核').first()).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('完整源包')).toBeVisible()
  await expect(page.getByTitle(sourceHash).first()).toBeVisible()
  await expect(page.getByText('3 张候选卡，尚未进入正式知识库')).toBeVisible()
  const receiptEvidence = page.getByRole('region', { name: '执行与正式化收据' })
  await expect(
    receiptEvidence.locator('dt').filter({ hasText: 'lineage' }).locator('..').locator('dd'),
  ).toHaveText('verified')
  await expect(
    receiptEvidence.locator('dt').filter({ hasText: 'formalization' }).locator('..').locator('dd'),
  ).toHaveText('awaiting_review')

  const runtimeJobs = await page.request.get(`${runtimeUrl}/api/v1/source-imports`)
  expect(runtimeJobs.ok()).toBeTruthy()
  const localJob = (await runtimeJobs.json() as { imports: Array<{ provider: string; status: string; candidate: { source_sha256?: string } }> }).imports
    .find((item) => item.provider === 'local_file')
  expect(localJob?.status).toBe('awaiting_review')
  expect(localJob?.candidate.source_sha256).toBe(sourceHash)

  await page.screenshot({
    path: path.join(evidenceDir, 'local-txt-import-desktop-1440x900.png'),
    fullPage: false,
  })

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByText('蒸馏草案待复核').first()).toBeVisible()
  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)
  await page.screenshot({
    path: path.join(evidenceDir, 'local-txt-import-mobile-390x844.png'),
    fullPage: false,
  })
})

agentChangeSetTest('Workbench-only local TXT reaches formal knowledge, gated ChangeSet, Apply, and byte-exact Rollback', async ({
  page,
  request,
  isolatedProject,
}) => {
  agentChangeSetTest.setTimeout(300_000)
  const objective = '使用本批已正式化知识，为当前章节生成可供作者逐项审阅的正文修改建议。'
  const candidateLine = '他必须在天亮前作出选择，并承担由此而来的代价。'
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('Runtime 就绪').first()).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 15_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`, { maxRetries: 2 })
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json() as {
    projects: Array<{ project_id: string; root_path: string }>
  }).projects.find((item) => path.resolve(item.root_path) === path.resolve(isolatedProject.rootPath))
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id

  const beforeSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(beforeSnapshotResponse.ok()).toBeTruthy()
  const beforeSnapshot = await beforeSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
    files: Array<{ file_id: string; relative_path: string }>
  }
  const prose = beforeSnapshot.files.find((file) => file.relative_path.endsWith('CH001_正文.md'))
  expect(prose).toBeTruthy()
  const proseId = (prose as { file_id: string }).file_id
  const beforeContentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(beforeContentResponse.ok()).toBeTruthy()
  const beforeContent = (await beforeContentResponse.json()).content as string
  expect(beforeContent).not.toContain(candidateLine)
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)

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
  await page.getByRole('button', { name: '复核候选', exact: true }).click()
  const candidateTabs = page.getByRole('tablist', { name: '知识候选', exact: true })
  await expect(candidateTabs.getByRole('tab')).toHaveCount(3, { timeout: 30_000 })

  await reviewAndPromoteKnowledgeCandidate(page, 'work')
  await reviewAndPromoteKnowledgeCandidate(page, 'voice')
  await reviewAndPromoteKnowledgeCandidate(page, 'validator')
  await expect(page.getByRole('status', { name: '候选状态汇总' })).toContainText('promoted 3')
  await page.getByRole('button', { name: '完成本批复核', exact: true }).click()
  await expect(page.getByRole('status', { name: '资料批次已完成' })).toContainText(
    'completed · 本批复核已完成',
    { timeout: 30_000 },
  )
  await expect(page.getByRole('button', { name: '使用这批知识开始写作', exact: true })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440)
  await page.screenshot({
    path: path.join(evidenceDir, 'workbench-only-local-knowledge-desktop-1440x900.png'),
    fullPage: false,
  })

  const sourceReceiptResponse = await request.get(
    `${runtimeUrl}/api/v1/source-imports/${encodeURIComponent(sourceImportId)}/execution-receipt`,
  )
  const candidatesResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/candidates?source_import_id=${encodeURIComponent(sourceImportId)}`,
  )
  const formalizationResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/source-imports/${encodeURIComponent(sourceImportId)}/formalization`,
  )
  const cardsResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/cards`,
  )
  expect(sourceReceiptResponse.ok()).toBeTruthy()
  expect(candidatesResponse.ok()).toBeTruthy()
  expect(formalizationResponse.ok()).toBeTruthy()
  expect(cardsResponse.ok()).toBeTruthy()
  const sourceReceiptRecord = await sourceReceiptResponse.json() as {
    source_import_id: string
    receipt: {
      receipt_hash: string
      source_packet_fingerprint: string
      draft_fingerprint: string
      cards_draft_hash: string
    }
  }
  const candidateList = await candidatesResponse.json() as {
    candidate_set_hash: string
    candidates: Array<{
      candidate_id: string
      card_type: KnowledgeCardType
      source_import_id: string
      status: string
      origin: {
        source_receipt_hash: string
        source_packet_fingerprint: string
        draft_fingerprint: string
      }
    }>
  }
  const formalization = await formalizationResponse.json() as {
    status: string
    receipt: {
      formalization_receipt_id: string
      receipt_hash: string
      source_import_id: string
      source_receipt_hash: string
      source_packet_fingerprint: string
      draft_fingerprint: string
      cards_draft_hash: string
      candidate_set_hash: string
      scope: { scope_type: string; project_binding_id: string | null }
    }
  }
  const scopedCards = await cardsResponse.json() as {
    fingerprint: string
    cards: Array<{
      active_version: {
        card: { card_type: KnowledgeCardType }
        lineage: { source_import_id: string; candidate_origin: { source_receipt_hash: string } }
        formalization: { formalization_receipt_id: string; formalization_receipt_hash: string }
        formalization_status: string
      }
    }>
  }
  expect(sourceReceiptRecord.source_import_id).toBe(sourceImportId)
  expect(candidateList.candidates).toHaveLength(3)
  expect(new Set(candidateList.candidates.map((item) => item.card_type))).toEqual(
    new Set<KnowledgeCardType>(['work', 'voice', 'validator']),
  )
  for (const candidate of candidateList.candidates) {
    expect(candidate).toMatchObject({ source_import_id: sourceImportId, status: 'promoted' })
    expect(candidate.origin).toMatchObject({
      source_receipt_hash: sourceReceiptRecord.receipt.receipt_hash,
      source_packet_fingerprint: sourceReceiptRecord.receipt.source_packet_fingerprint,
      draft_fingerprint: sourceReceiptRecord.receipt.draft_fingerprint,
    })
  }
  expect(formalization.status).toBe('verified')
  expect(formalization.receipt).toMatchObject({
    source_import_id: sourceImportId,
    source_receipt_hash: sourceReceiptRecord.receipt.receipt_hash,
    source_packet_fingerprint: sourceReceiptRecord.receipt.source_packet_fingerprint,
    draft_fingerprint: sourceReceiptRecord.receipt.draft_fingerprint,
    cards_draft_hash: sourceReceiptRecord.receipt.cards_draft_hash,
    candidate_set_hash: candidateList.candidate_set_hash,
    scope: { scope_type: 'project', project_binding_id: projectId },
  })
  const batchCards = scopedCards.cards.filter(
    (card) => card.active_version.lineage.source_import_id === sourceImportId,
  )
  expect(batchCards).toHaveLength(3)
  for (const card of batchCards) {
    expect(card.active_version).toMatchObject({ formalization_status: 'verified' })
    expect(card.active_version.lineage.candidate_origin.source_receipt_hash).toBe(
      sourceReceiptRecord.receipt.receipt_hash,
    )
    expect(card.active_version.formalization).toEqual({
      formalization_receipt_id: formalization.receipt.formalization_receipt_id,
      formalization_receipt_hash: formalization.receipt.receipt_hash,
    })
  }

  await page.getByRole('button', { name: '使用这批知识开始写作', exact: true }).click()
  const contextPanel = page.locator('.context-pack-panel')
  await expect(contextPanel).toBeVisible({ timeout: 30_000 })
  await expect(contextPanel.getByText('NLCL 路由收据')).toBeVisible()
  await expect(contextPanel.getByRole('radio', { name: '正文候选', exact: true })).toHaveAttribute(
    'aria-checked',
    'true',
  )
  await expect(contextPanel.getByText('生成正文改动候选，仍需作者审阅后写回')).toBeVisible()
  await contextPanel.getByRole('button', { name: '确认并生成候选', exact: true }).click()

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: /Tools/ }).click()
  const toolCards = page.locator('.tool-activity-card')
  await expect(toolCards).toHaveCount(7, { timeout: 60_000 })
  await expect(toolCards.filter({ hasText: 'change_set_propose' })).toHaveCount(1)
  await expect(toolCards.filter({ hasText: 'candidate_output_submit' })).toHaveCount(1)
  await expect(page.getByLabel('给 Writing Agent 的指令')).toBeEnabled({ timeout: 30_000 })
  await page.getByLabel('运行详情').getByRole('tab', { name: /候选/ }).click()
  await expect(page.getByRole('article', { name: 'Writer 任务与候选结果' })).toContainText(
    '候选结果',
    { timeout: 30_000 },
  )
  await expect(page.getByRole('article', { name: 'Writer 任务与候选结果' })).toContainText(
    'writer-candidate-result-',
  )
  const candidateCard = page.locator('.candidate-output-card')
  await expect(candidateCard.getByText(/unreported · \d+ turns?/)).toBeVisible({ timeout: 30_000 })
  await expect(candidateCard.getByText(/Provider 身份收据 · \d+/)).toBeVisible()
  await expect(candidateCard.getByText(/turn 1 · unreported/)).toBeVisible()
  await expect(candidateCard.getByText(/playwright-scripted-model → 未报告/)).toHaveCount(4)
  await expect(candidateCard.getByText(/invalid_evidence/)).toHaveCount(0)

  const sessionsResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
  )
  expect(sessionsResponse.ok()).toBeTruthy()
  const sessionId = (await sessionsResponse.json() as {
    sessions: Array<{ session_id: string }>
  }).sessions[0].session_id
  const runsResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
  )
  expect(runsResponse.ok()).toBeTruthy()
  const run = (await runsResponse.json() as {
    runs: Array<{ run_id: string; objective: string; ordinal: number }>
  }).runs
    .filter((item) => item.objective === objective)
    .sort((left, right) => right.ordinal - left.ordinal)[0]
  expect(run).toBeTruthy()
  const runId = run.run_id
  const candidateResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/candidate-output`,
  )
  const providerReceiptsResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/provider-turn-receipts`,
  )
  const writerTaskResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/writer-task`,
  )
  const writerResultResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/writer-candidate-result`,
  )
  expect(candidateResponse.ok()).toBeTruthy()
  expect(providerReceiptsResponse.ok()).toBeTruthy()
  expect(writerTaskResponse.ok()).toBeTruthy()
  expect(writerResultResponse.ok()).toBeTruthy()
  const candidateEnvelope = await candidateResponse.json() as {
    schema: string
    candidate_output_id: string
    envelope_hash: string
    writer_task_id: string
    writer_task_hash: string
    instruction_materialization_id: string
    instruction_materialization_hash: string
    instruction_dispatch_receipt_id: string
    instruction_dispatch_receipt_hash: string
    provider_turn_receipts: Array<{
      turn_ordinal: number
      receipt_id: string
      receipt_hash: string
    }>
    provider_turn_receipts_hash: string
    provider_identity_status: string
  }
  const providerReceipts = await providerReceiptsResponse.json() as {
    requested_model: string | null
    provider_turn_receipts: Array<{
      turn_ordinal: number
      receipt_id: string
      receipt_hash: string
      requested_model: string | null
      reported_model: string | null
      model_identity_status: string
    }>
    provider_turn_receipts_hash: string
    provider_identity_status: string
  }
  expect(candidateEnvelope).toMatchObject({
    schema: 'zhimeng.agent-candidate-output.v3',
    provider_identity_status: 'unreported',
  })
  expect(providerReceipts).toMatchObject({
    requested_model: 'playwright-scripted-model',
    provider_identity_status: 'unreported',
    provider_turn_receipts_hash: candidateEnvelope.provider_turn_receipts_hash,
  })
  expect(providerReceipts.provider_turn_receipts).toHaveLength(4)
  expect(providerReceipts.provider_turn_receipts.map((receipt) => ({
    turn_ordinal: receipt.turn_ordinal,
    receipt_id: receipt.receipt_id,
    receipt_hash: receipt.receipt_hash,
  }))).toEqual(candidateEnvelope.provider_turn_receipts)
  for (const receipt of providerReceipts.provider_turn_receipts) {
    expect(receipt).toMatchObject({
      requested_model: 'playwright-scripted-model',
      reported_model: null,
      model_identity_status: 'unreported',
    })
  }
  const writerTask = await writerTaskResponse.json() as {
    writer_task_id: string
    task_hash: string
    context_pack_id: string
    context_pack_hash: string
    project_id: string
    project_revision: number
    project_fingerprint: string
    knowledge_fingerprint: string
    selected_card_ids: string[]
    formal_draft_allowed: boolean
    writes_project_truth: boolean
    requires_author_review: boolean
  }
  const writerResult = await writerResultResponse.json() as {
    writer_task_id: string
    writer_task_hash: string
    candidate_output_id: string
    candidate_output_hash: string
    change_set_id: string
    project_id: string
    project_fingerprint: string
    formal_draft_allowed: boolean
    writes_project_truth: boolean
    requires_author_review: boolean
  }
  expect(writerTask).toMatchObject({
    project_id: projectId,
    project_revision: beforeSnapshot.project.revision,
    project_fingerprint: beforeSnapshot.project.fingerprint,
    knowledge_fingerprint: scopedCards.fingerprint,
    formal_draft_allowed: false,
    writes_project_truth: false,
    requires_author_review: true,
  })
  expect(writerTask.selected_card_ids).toHaveLength(3)
  expect(candidateEnvelope).toMatchObject({
    writer_task_id: writerTask.writer_task_id,
    writer_task_hash: writerTask.task_hash,
  })
  expect(candidateEnvelope.instruction_materialization_id).toMatch(
    /^creation-instruction-materialization-[0-9a-f]{32}$/,
  )
  expect(candidateEnvelope.instruction_materialization_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(candidateEnvelope.instruction_dispatch_receipt_id).toMatch(
    /^creation-instruction-dispatch-[0-9a-f]{32}$/,
  )
  expect(candidateEnvelope.instruction_dispatch_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(writerResult).toMatchObject({
    project_id: projectId,
    project_fingerprint: beforeSnapshot.project.fingerprint,
    writer_task_id: writerTask.writer_task_id,
    writer_task_hash: writerTask.task_hash,
    candidate_output_id: candidateEnvelope.candidate_output_id,
    candidate_output_hash: candidateEnvelope.envelope_hash,
    formal_draft_allowed: false,
    writes_project_truth: false,
    requires_author_review: true,
  })
  const contextPackResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(writerTask.context_pack_id)}`,
  )
  const routeResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(writerTask.context_pack_id)}/nlcl-route`,
  )
  expect(contextPackResponse.ok()).toBeTruthy()
  expect(routeResponse.ok()).toBeTruthy()
  const contextPackRecord = await contextPackResponse.json() as {
    schema: 'zhimeng.context-pack-record.v1'
    pack: {
      pack_hash: string
      project_id: string
      project_revision: number
      project_fingerprint: string
      knowledge_fingerprint: string
    }
    consumed_by_run_id: string | null
  }
  const route = await routeResponse.json() as {
    source: string
    knowledge_fingerprint: string
    selected_cards: Array<{
      card_id: string
      card_type: KnowledgeCardType
      knowledge_provenance: {
        source_import_id: string
        formalization_receipt_id: string
        formalization_receipt_hash: string
      }
    }>
  }
  expect(contextPackRecord).toMatchObject({
    schema: 'zhimeng.context-pack-record.v1',
    consumed_by_run_id: runId,
  })
  expect(contextPackRecord.pack).toMatchObject({
    pack_hash: writerTask.context_pack_hash,
    project_id: projectId,
    project_revision: beforeSnapshot.project.revision,
    project_fingerprint: beforeSnapshot.project.fingerprint,
    knowledge_fingerprint: scopedCards.fingerprint,
  })
  expect(route).toMatchObject({ source: 'native_knowledge', knowledge_fingerprint: scopedCards.fingerprint })
  expect(route.selected_cards).toHaveLength(3)
  expect(new Set(route.selected_cards.map((item) => item.card_type))).toEqual(
    new Set<KnowledgeCardType>(['work', 'voice', 'validator']),
  )
  for (const selectedCard of route.selected_cards) {
    expect(selectedCard.knowledge_provenance).toMatchObject({
      source_import_id: sourceImportId,
      formalization_receipt_id: formalization.receipt.formalization_receipt_id,
      formalization_receipt_hash: formalization.receipt.receipt_hash,
    })
  }

  const preApprovalSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(preApprovalSnapshotResponse.ok()).toBeTruthy()
  expect((await preApprovalSnapshotResponse.json()).project).toEqual(beforeSnapshot.project)
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)
  const preApprovalContentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(preApprovalContentResponse.ok()).toBeTruthy()
  expect((await preApprovalContentResponse.json()).content).toBe(beforeContent)

  await page.getByRole('tab', { name: /Timeline/ }).click()
  const proposalActivity = page.locator('.timeline-change_proposal')
  await expect(proposalActivity).toHaveCount(1)
  await expect(proposalActivity).toContainText('本批知识正文候选')
  await proposalActivity.getByRole('button', { name: '审阅改动', exact: true }).click()
  await expect(page.getByRole('heading', { name: '本批知识正文候选', exact: true })).toBeVisible()
  await expect(page.getByRole('main').getByText(candidateLine, { exact: true })).toBeVisible()

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tab', { name: '正文', exact: true }).click()
  const mobileEditor = page.locator('[data-mobile-pane="editor"]')
  await expect(mobileEditor.getByText(candidateLine, { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: path.join(evidenceDir, 'workbench-only-local-changeset-mobile-390x844.png'),
    fullPage: false,
  })
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.getByRole('button', { name: /接受 .* Hunk 1/ }).click()
  const applyButton = page.getByRole('button', { name: '应用所选更改', exact: true })
  await expect(page.getByLabel('Gate 状态：未运行')).toBeVisible()
  await expect(applyButton).toBeDisabled()
  await page.getByRole('button', { name: '运行 Gate', exact: true }).click()
  await expect(page.getByLabel('Gate 状态：通过')).toBeVisible({ timeout: 45_000 })
  await expect(applyButton).toBeEnabled()

  const gateResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(writerResult.change_set_id)}/post-draft-gate`,
  )
  expect(gateResponse.ok()).toBeTruthy()
  const gate = await gateResponse.json() as {
    receipt: {
      status: string
      project_id: string
      base_revision: number
      base_fingerprint: string
      context_pack_id: string
      context_pack_hash: string
      knowledge_fingerprint: string
      knowledge_validator_count: number
      knowledge_validator_card_ids: string[]
      quality_assessed: boolean
      formal_draft_allowed: boolean
      result: {
        quality_assessed: boolean
        formal_draft_allowed: boolean
        failure_signatures: string[]
        knowledge_validator_results: Array<{
          card_id: string
          status: string
          configuration_errors: string[]
          failure_signatures: string[]
          rule_results: Array<{ rule_id: string; status: string }>
        }>
      }
    }
  }
  expect(gate.receipt).toMatchObject({
    status: 'passed',
    project_id: projectId,
    base_revision: beforeSnapshot.project.revision,
    base_fingerprint: beforeSnapshot.project.fingerprint,
    context_pack_id: writerTask.context_pack_id,
    context_pack_hash: writerTask.context_pack_hash,
    knowledge_fingerprint: scopedCards.fingerprint,
    knowledge_validator_count: 1,
    quality_assessed: false,
    formal_draft_allowed: false,
  })
  expect(gate.receipt.knowledge_validator_card_ids).toHaveLength(1)
  expect(gate.receipt.result).toMatchObject({
    quality_assessed: false,
    formal_draft_allowed: false,
    failure_signatures: [],
  })
  expect(gate.receipt.result.knowledge_validator_results).toHaveLength(1)
  const validatorResult = gate.receipt.result.knowledge_validator_results[0]
  expect(validatorResult).toMatchObject({
    status: 'passed',
    configuration_errors: [],
    failure_signatures: [],
  })
  expect(validatorResult.card_id).toBe(gate.receipt.knowledge_validator_card_ids[0])
  expect(validatorResult.rule_results).toEqual(expect.arrayContaining([
    expect.objectContaining({ rule_id: 'forbid_source_terms', status: 'passed' }),
    expect.objectContaining({ rule_id: 'require_visible_pressure', status: 'passed' }),
  ]))
  const afterGateSnapshot = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(afterGateSnapshot.ok()).toBeTruthy()
  expect((await afterGateSnapshot.json()).project).toEqual(beforeSnapshot.project)
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440)
  await page.screenshot({
    path: path.join(evidenceDir, 'workbench-only-formal-validator-gate-desktop-1440x900.png'),
    fullPage: false,
  })

  await applyButton.click()
  const rollbackButton = page.getByRole('button', { name: '回滚 ChangeSet', exact: true })
  await expect(rollbackButton).toBeVisible({ timeout: 30_000 })
  const appliedBytes = await readFile(isolatedProject.prosePath)
  expect(appliedBytes.equals(isolatedProject.originalProseBytes)).toBe(false)
  expect(appliedBytes.toString('utf8')).toContain(candidateLine)
  const appliedSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(appliedSnapshotResponse.ok()).toBeTruthy()
  const appliedSnapshot = await appliedSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(appliedSnapshot.project.revision).toBe(beforeSnapshot.project.revision + 1)
  expect(appliedSnapshot.project.fingerprint).not.toBe(beforeSnapshot.project.fingerprint)

  await rollbackButton.click()
  await expect(page.getByText(`项目结果 revision ${beforeSnapshot.project.revision + 2}`)).toBeVisible({
    timeout: 30_000,
  })
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(isolatedProject.originalProseBytes)
  const rolledBackSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(rolledBackSnapshotResponse.ok()).toBeTruthy()
  const rolledBackSnapshot = await rolledBackSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(rolledBackSnapshot.project.revision).toBe(beforeSnapshot.project.revision + 2)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await expect(page.getByText(
    `项目结果 revision ${beforeSnapshot.project.revision + 2}`,
    { exact: true },
  )).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  const inspector = page.getByLabel('写作检查器')
  const inspectorBox = await inspector.boundingBox()
  expect(inspectorBox).not.toBeNull()
  expect((inspectorBox?.x ?? 0) + (inspectorBox?.width ?? 0)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: path.join(evidenceDir, 'workbench-only-local-rollback-mobile-390x844.png'),
    fullPage: false,
  })
})

agentChangeSetTest('project KB rebuilds through the real Runtime and survives refresh without overflow', async ({
  page,
  request,
}) => {
  agentChangeSetTest.setTimeout(90_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  let rebuildRequests = 0
  page.on('request', (outgoing) => {
    const url = new URL(outgoing.url())
    if (
      outgoing.method() === 'POST'
      && url.pathname.endsWith('/knowledge/kb-state/rebuild')
    ) rebuildRequests += 1
  })
  const inspectionResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/projects/inspect'
  ))
  await page.goto('/')
  const inspection = await (await inspectionResponse).json() as {
    project: { project_id: string }
  }
  const projectId = inspection.project.project_id
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('button', { name: '知识库' }).click()
  const panel = page.locator('.knowledge-panel')
  const statusPanel = panel.getByLabel('项目知识快照')
  await expect(statusPanel.getByText('项目知识快照尚未生成')).toBeVisible({
    timeout: 15_000,
  })
  const rebuild = statusPanel.getByRole('button', { name: '重建快照' })
  await expect(rebuild).toBeEnabled()
  const rebuildResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/knowledge/kb-state/rebuild')
  ))
  await rebuild.click()
  expect((await rebuildResponse).ok()).toBeTruthy()
  await expect(statusPanel.getByText('项目知识快照已同步')).toBeVisible({
    timeout: 20_000,
  })
  expect(rebuildRequests).toBe(1)

  const runtimeState = await request.get(
    `${runtimeUrl}/api/v1/projects/${projectId}/knowledge/kb-state`,
  )
  expect(runtimeState.ok()).toBeTruthy()
  const current = await runtimeState.json() as {
    project_id: string
    status: string
    runtime_relative_root: string
    reasons: string[]
    manifest: {
      project: { project_id: string }
      source_of_truth: {
        project_files: string
        runtime_sqlite: string
        derived: boolean
        rebuildable: boolean
      }
    } | null
  }
  expect(current.project_id).toBe(projectId)
  expect(current.status).toBe('current')
  expect(current.reasons).toEqual([])
  expect(current.runtime_relative_root).toBe(`project-kb/${projectId}.kb`)
  expect(current.manifest?.project.project_id).toBe(projectId)
  expect(current.manifest?.source_of_truth).toMatchObject({
    project_files: 'work_truth',
    runtime_sqlite: 'run_truth',
    derived: true,
    rebuildable: true,
  })
  await page.screenshot({
    path: path.join(evidenceDir, 'project-kb-current-desktop-1440x900.png'),
    fullPage: false,
  })

  await page.reload()
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: '知识库' }).click()
  await expect(page.getByLabel('项目知识快照').getByText('项目知识快照已同步')).toBeVisible({
    timeout: 15_000,
  })
  expect(rebuildRequests).toBe(1)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByLabel('移动端工作区').getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: '知识', exact: true }).click()
  const compactPanel = page.locator('.knowledge-panel')
  await expect(compactPanel.getByText('项目知识快照已同步')).toBeVisible()
  const fit = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
  }))
  expect(fit.document).toBeLessThanOrEqual(fit.viewport)
  expect(await compactPanel.evaluate((element) => (
    element.scrollWidth <= element.clientWidth
  ))).toBeTruthy()
  const inspector = page.getByLabel('写作检查器')
  const inspectorBox = await inspector.boundingBox()
  expect(inspectorBox).not.toBeNull()
  expect((inspectorBox?.x ?? 0) + (inspectorBox?.width ?? 0)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: path.join(evidenceDir, 'project-kb-current-mobile-390x844.png'),
    fullPage: false,
  })
})

test('desktop reviews native knowledge candidates and active formal versions', async ({ page }) => {
  await mockKnowledge(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('Runtime 就绪').first()).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: '知识库' }).click()

  const panel = page.locator('.knowledge-panel')
  await expect(panel.getByText('原生知识库')).toBeVisible()
  await expect(panel.getByText('1 项未通过')).toBeVisible()
  await expect(panel.getByText('知识版本')).toBeVisible()
  await expect(panel.getByTitle(knowledgeScopedCardListFixture.fingerprint)).toHaveText('99999999')
  const desktopFits = await page.locator('.knowledge-panel').evaluate((element) => (
    element.scrollWidth <= element.clientWidth
  ))
  expect(desktopFits).toBeTruthy()
  await page.screenshot({
    path: path.join(evidenceDir, 'native-knowledge-review-desktop-1440x900.png'),
    fullPage: true,
  })
})

test('mobile native knowledge review stays inside the inspector pane', async ({ page }) => {
  await mockKnowledge(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: '知识' }).click()

  const panel = page.locator('.knowledge-panel')
  await expect(panel.getByText('原生知识库')).toBeVisible()
  await expect(panel.getByText('1 项未通过')).toBeVisible()
  const mobileFits = await page.evaluate(() => (
    document.documentElement.scrollWidth <= window.innerWidth
  ))
  expect(mobileFits).toBeTruthy()
  await page.screenshot({
    path: path.join(evidenceDir, 'native-knowledge-review-mobile-390x844.png'),
    fullPage: true,
  })
})

test('desktop renders contract-backed distillation coverage without claiming formal context', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockDistillationPreview(page)
  await page.goto('/')
  await expect(page.getByText('Runtime 就绪').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('tab', { name: 'Context', exact: true }).click()
  await expect(page.getByText('风险就绪 · 1,124 张卡')).toBeVisible()
  await page.getByRole('textbox', { name: '写作目标' }).fill(distillationCoverage.query)
  await page.getByRole('button', { name: '检查覆盖' }).click()

  await expect(page.getByText('只能作为风险预览，不进入正式写作')).toBeVisible()
  await expect(page.getByText('系统流公共评价类型合同')).toBeVisible()
  await expect(page.getByText('开篇兑现风险闸门')).toBeVisible()
  await expect(page.getByText(/已注入|正式上下文已装配/)).toHaveCount(0)
  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)

  await page.screenshot({
    path: path.join(evidenceDir, 'distillation-coverage-desktop-1440x900.png'),
    fullPage: false,
  })
})

test('mobile distillation coverage stays inside the inspector viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockDistillationPreview(page)
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await page.getByRole('tab', { name: 'Context', exact: true }).click()
  await page.getByRole('textbox', { name: '写作目标' }).fill(distillationCoverage.query)
  await page.getByRole('button', { name: '检查覆盖' }).click()
  await expect(page.getByText('只能作为风险预览，不进入正式写作')).toBeVisible()

  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width)
  const inspector = page.getByLabel('写作检查器')
  const box = await inspector.boundingBox()
  expect(box).not.toBeNull()
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390)
  const panelFits = await page.locator('.distillation-panel').evaluate((element) => (
    element.scrollWidth <= element.clientWidth
  ))
  const summaryFits = await page.locator('.coverage-summary-line').evaluate((element) => (
    element.scrollWidth <= element.clientWidth
  ))
  expect(panelFits).toBeTruthy()
  expect(summaryFits).toBeTruthy()

  await page.screenshot({
    path: path.join(evidenceDir, 'distillation-coverage-mobile-390x844.png'),
    fullPage: false,
  })
})

test('desktop lists managed Skill lifecycle and toggles project enable/disable with CAS', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`)
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json()).projects.find(
    (candidate: { display_name: string }) => candidate.display_name === 'minimal-novel',
  ) as { project_id: string } | undefined
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id

  const beforeResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/managed-skills`,
  )
  expect(beforeResponse.ok()).toBeTruthy()
  const before = await beforeResponse.json() as {
    activations: Array<{
      skill_id: string
      effective_state: string
      configured_state: string
      state_revision: number
      configured_version_id: string | null
      effective_version_id: string | null
    }>
  }
  const target = before.activations.find((item) => item.skill_id === 'novel-creation-suite.project-audit')
  expect(target).toBeTruthy()
  expect(target?.effective_state).toBe('enabled')

  await page.getByRole('tab', { name: '技能' }).click()
  const panel = page.getByRole('region', { name: '受管 Skill 生命周期' })
  await expect(panel).toBeVisible()
  const card = panel.locator('.managed-skill-card[data-skill-id="novel-creation-suite.project-audit"]')
  await expect(card).toBeVisible()
  await expect(card).toContainText('novel-creation-suite.project-audit')
  await expect(card).toHaveAttribute('data-effective-state', 'enabled')

  await card.getByRole('button', { name: '项目禁用' }).click()
  await expect(card).toHaveAttribute('data-effective-state', 'disabled', { timeout: 10_000 })
  await expect(card).toContainText('项目已禁用')

  const disabledResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/managed-skills`,
  )
  expect(disabledResponse.ok()).toBeTruthy()
  const disabled = (await disabledResponse.json()).activations.find(
    (item: { skill_id: string }) => item.skill_id === 'novel-creation-suite.project-audit',
  )
  expect(disabled.effective_state).toBe('disabled')
  expect(disabled.configured_state).toBe('disabled')
  expect(disabled.state_revision).toBeGreaterThan(target!.state_revision)

  await card.getByRole('button', { name: '项目启用' }).click()
  await expect(card).toHaveAttribute('data-effective-state', 'enabled', { timeout: 10_000 })
  await expect(card).toContainText('项目生效中')

  const enabledResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/managed-skills`,
  )
  expect(enabledResponse.ok()).toBeTruthy()
  const enabled = (await enabledResponse.json()).activations.find(
    (item: { skill_id: string }) => item.skill_id === 'novel-creation-suite.project-audit',
  )
  expect(enabled.effective_state).toBe('enabled')
  expect(enabled.configured_state).toBe('enabled')
  expect(enabled.effective_scope).toBe('project')

  await page.screenshot({
    path: path.join(evidenceDir, 'managed-skill-lifecycle-desktop-1440x900.png'),
    fullPage: false,
  })
})

