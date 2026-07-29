import { expect, test } from '@playwright/test'
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const evidenceDir = path.resolve(
  process.env.PLAYWRIGHT_EVIDENCE_DIR ?? path.resolve(process.cwd(), 'test-results/evidence'),
)
const runtimeUrl = process.env.PLAYWRIGHT_RUNTIME_URL
  ?? `http://127.0.0.1:${process.env.PLAYWRIGHT_RUNTIME_PORT ?? '8789'}`
const sourceProjectRoot = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/minimal-novel',
)
const localSourcePath = path.join(
  repoRoot,
  'services/writing-runtime/tests/fixtures/local-source/archive-night-sample.txt',
)
const sha256Pattern = /^[0-9a-f]{64}$/
const chainPathSuffix = '/adjacent-skill-chains/novel-distillation-kb-to-context'
const candidateLine = '候选正文句，只供作者审阅。'

interface IsolatedProjectFixture {
  rootPath: string
  prosePath: string
  sourceProsePath: string
  originalProseBytes: Buffer
}

const adjacentChainTest = test.extend<{ isolatedProject: IsolatedProjectFixture }>({
  isolatedProject: async ({ page }, provide) => {
    const sandboxParent = path.join(path.dirname(sourceProjectRoot), 'test-results')
    await mkdir(sandboxParent, { recursive: true })
    const sandboxRoot = await mkdtemp(path.join(sandboxParent, 'adjacent-chain-'))
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

      // Redirect project inspection to this test's copy so Gate/Apply/Rollback
      // never mutates the shared source fixture.
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

type KnowledgeCardType = 'work' | 'voice' | 'validator'

interface KnowledgeCandidate {
  candidate_id: string
  card_id: string
  card_type: KnowledgeCardType
  status: string
  revision: number
  content_hash: string
  source_draft_hash: string
}

interface AdjacentChainStep {
  step_id: string
  ordinal: number
  title: string
  skill_id: string
  capability: string
  status: string
  child_id: string | null
  result_hash: string | null
  error_code: string | null
  error_message: string | null
}

interface AdjacentChainRecord {
  schema: 'zhimeng.adjacent-skill-chain-run.v1'
  chain_id: string
  chain_key: 'novel-distillation-kb-to-context'
  status: string
  project: { project_id: string; revision: number; fingerprint: string }
  route: { profile: string; stage: string; chapter_id: string; task_intent: string }
  steps: [AdjacentChainStep, AdjacentChainStep]
  approval: {
    approved: boolean
    approved_by: 'author' | null
    approved_at: string | null
    risk_acknowledged: boolean
  }
  permissions: {
    read: string[]
    write: string[]
    network: false
    secrets: string[]
  }
  formal_draft_allowed: false
  source_import_id: string | null
  skill_handoff_id: string | null
  context_handoff_id: string | null
  context_pack_id: string | null
  context_pack_hash: string | null
  route_receipt_id: string | null
  route_receipt_hash: string | null
  current_step_ordinal: number
  error_code: string | null
  error_message: string | null
}

test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true })
})

function validationLadder() {
  return {
    internal_consistency: {
      status: 'pass',
      evidence: ['内部一致性已由作者逐项复核。'],
    },
    predictive_validation: {
      status: 'pass',
      evidence: ['预测验证已由相邻章节压力样本复核。'],
    },
    blind_sample_validation: {
      status: 'pass',
      evidence: ['盲样验证确认机制脱离来源表达后仍可迁移。'],
    },
    extreme_scenario_stress_test: {
      status: 'pass',
      evidence: ['极端压力测试确认失败边界与拒收条件可执行。'],
    },
  }
}

function baseCard(cardId: string) {
  return {
    card_id: cardId,
    card_type: 'work',
    title: '长篇升级循环与读者承诺',
    source: {
      name: '本地导入测试来源',
      author: '未知',
      scope: '完整本地样本与跨章节重复结构复核',
      evidence_quality: 'medium',
      notes: '作者确认只保留抽象机制，不复制来源表达。',
    },
    taxonomy: {
      genre: ['玄幻'],
      scene_function: ['阶段兑现'],
      emotion: ['压迫', '释放'],
      craft: ['升级循环'],
    },
    tags: ['玄幻', '升级', '兑现'],
    keywords: ['阶段兑现', '升级循环'],
    applicable_tasks: ['chapter_brief', 'draft_scene', 'review'],
    summary: '通过可见压力、有限选择、付出代价和阶段兑现组成可变形的长篇推进循环，并让回报真实改变人物处境。',
    reader_promise: '持续升级的危机、选择、代价与阶段兑现，并在每次回报后留下更高层债务。',
    reader_effect: {
      author_intent: '让读者明确感到每次选择都在推进力量与关系。',
      observed_reader_effect: '读者在小兑现后仍会追问下一次更高代价的选择。',
      effect_evidence: ['跨章节样本中，小兑现后留下了更高层级的新债务。'],
      effect_failure_boundary: '连续两章只有升级说明而没有选择和代价时会变成流水账。',
      misread_risk: '只复制升级名词会丢失压力、选择与代价之间的因果。',
      when_it_turns_bad: '回报不改变人物处境，或新债务与旧选择没有关系时。',
    },
    type_payload: {
      premise_pattern: '主角在资源受限时以承担代价换取新的行动空间。',
      core_reader_promise: '每次选择都会兑现眼前收益，同时生成更高一级且可追溯的义务。',
      opening_contract: '开场先展示可见损失，再给出只能冒险验证的有限机会。',
      progression_map: ['压力外显后做选择', '付出代价后小兑现', '兑现带出更大债务'],
      recurring_scene_loops: ['资源受限 -> 风险试探 -> 小兑现 -> 更高压力'],
      transfer_limits: ['不得复制来源人物', '不得复制来源设定壳', '不得复刻场景顺序'],
    },
    mechanics: [{
      name: '代价绑定的阶段兑现',
      use_when: '章节需要同时推进力量、关系与下一阶段压力时。',
      rule: '先让角色做不可撤销的选择，再用小兑现证明选择有效并抬高后续代价。',
      failure_mode: '先给奖励再补代价，会让选择失去重量并削弱追读。',
    }],
    validation_tests: [
      '本章兑现是否改变角色下一步可做的选择，而不只是增加数值。',
      '新增债务是否能追溯到本章选择和代价，而不是凭空追加危机。',
    ],
    validation_ladder: validationLadder(),
    scores: {
      evidence: 0.78,
      specificity: 0.82,
      generativity: 0.84,
      retrieval: 0.76,
      safety_risk: 0.12,
    },
    retrieval: {
      preferred_queries: ['玄幻 升级 阶段兑现'],
      negative_queries: ['纯日常 无升级'],
    },
    nlcl_route_hints: {
      recommended_profiles: ['chapter', 'draft_assist', 'review'],
      nlcl_layers: ['distillation_index'],
      route_terms: ['玄幻', '升级', '阶段兑现'],
      hard_filters: { genre: ['玄幻'] },
      negative_routes: ['纯日常'],
      safety_summary: '只使用抽象推进机制，不使用来源人物和设定壳。',
      feedback_default_layer: 'L3_DISTILLATION_RETRIEVAL',
    },
    safety: {
      copyright_boundary: '只保留可迁移机制，不保存来源表达和可识别场景壳。',
      do_not_reuse: ['来源人物', '来源设定壳', '来源场景顺序'],
      source_entity_names: [],
      source_setting_shells: [],
      signature_phrases: [],
      contamination_risks: ['source_shell', 'entity_leakage'],
    },
    draft_candidate: false,
    requires_human_distillation: false,
  }
}

function reviewedCard(cardType: KnowledgeCardType, cardId: string): Record<string, unknown> {
  const base = baseCard(cardId)
  if (cardType === 'work') return base
  if (cardType === 'voice') {
    return {
      ...base,
      card_type: 'voice',
      title: '贴近人物压力的长篇叙事声音规则',
      summary: '保持贴近人物判断的有限视角，以短中长句交替承接动作、关系和代价，并用现场变化替代抽象总结。',
      reader_promise: '读者能顺畅读懂人物正在做什么、为什么犹豫以及选择会付出什么代价。',
      taxonomy: {
        platform: ['番茄'],
        audience: ['男频'],
        genre: ['玄幻'],
        subgenre: ['升级流'],
        style: ['现场压力', '自然网文'],
        pov: ['贴近主角的有限视角'],
        content_boundary: ['只迁移抽象叙事行为'],
      },
      tags: ['番茄', '男频', '玄幻', '升级流', '有限视角', '自然网文'],
      keywords: ['人物压力', '自然叙事'],
      applicable_tasks: ['draft_scene', 'rewrite', 'review'],
      type_payload: {
        pov_distance: '贴近当前行动人物的判断，但不替角色总结全部情绪。',
        sentence_rhythm: '动作处用短句落点，关系变化用中句承接，信息解释不连续堆叠。',
        diction_pool: ['具体动作与物件', '人物关系站位', '可见资源和代价'],
        dialogue_behavior: '每句对话携带目的、关系站位或信息差。',
        description_behavior: '描写必须改变人物判断或行动空间。',
        emotion_handling: '先写身体反应、动作选择和关系后果。',
        human_web_prose: '句子语义完整、主谓清楚、落在当前场景。',
        sentence_variety: '短中长句按压力变化交替，禁止连续碎句。',
        scene_anchor_rules: ['每两到三段出现一次会改变判断的动作、物件或资源变化。'],
        dialogue_pressure_rules: ['每句对话至少推进目的、关系站位或信息差中的一项。'],
        anti_ai_failure_signatures: ['连续机械碎句', '抽象心理替代动作', '角色同声解释设定'],
        readability_acceptance_tests: ['连续阅读两千字时能分清谁在行动、谁在害怕以及谁承担代价。'],
        do_not_copy: ['来源作者标志句', '来源作品节奏链', '来源人物口头禅'],
      },
      nlcl_route_hints: {
        recommended_profiles: ['chapter', 'draft_assist', 'review'],
        nlcl_layers: ['distillation_index', 'style', 'deai'],
        route_terms: ['玄幻', '人物压力', '自然叙事'],
        hard_filters: { genre: ['玄幻'] },
        negative_routes: ['作者声纹模仿', '来源句式复刻'],
        safety_summary: '只迁移抽象叙事行为，不复制来源作者的声纹和表达。',
        feedback_default_layer: 'L5_DEAI_STYLE',
      },
    }
  }
  return {
    ...base,
    card_type: 'validator',
    title: '人物压力与来源污染的正文拒收闸门',
    summary: '在正文候选进入作者审阅前检查人物压力是否落到动作和代价，并阻断来源名称、设定壳、标志句或场景顺序。',
    reader_promise: '只有动作、关系和代价可追溯且没有来源污染的正文候选才能进入作者审阅。',
    taxonomy: {
      platform: ['番茄'],
      audience: ['男频'],
      genre: ['玄幻'],
      subgenre: ['升级流'],
      problem_symptom: ['抽象总结', '来源污染'],
      content_boundary: ['正文候选拒收闸门'],
      craft: ['人物压力'],
    },
    tags: ['番茄', '男频', '玄幻', '升级流', '正文审阅', '来源污染'],
    keywords: ['人物压力', '来源污染'],
    applicable_tasks: ['review', 'rewrite'],
    type_payload: {
      validator_name: 'character_pressure_and_source_contamination_gate',
      applies_to: ['完整章节正文候选', '针对性正文改写候选'],
      layer_id: 'L6_REVIEW_ACCEPTANCE',
      checks: [
        '人物选择是否通过动作、关系或资源变化产生可见代价。',
        '正文是否避开来源名称、设定壳、标志句和可识别场景顺序。',
      ],
      reject_conditions: [
        '连续使用抽象心理总结替代人物行动和关系后果。',
        '出现来源专名、标志表达或未经迁移的完整场景顺序。',
      ],
      execution_rules: [
        {
          rule_id: 'forbid_source_terms',
          kind: 'forbid_terms',
          terms: ['Archive Night', 'source shell'],
          case_sensitive: false,
          failure_signature: 'validator_source_term_leak',
          message: '正文候选包含正式 validator 明确禁止的来源词项。',
        },
        {
          rule_id: 'require_visible_pressure',
          kind: 'require_any_term',
          terms: ['代价', '压力', '选择'],
          case_sensitive: false,
          failure_signature: 'validator_visible_pressure_missing',
          message: '正文候选缺少正式 validator 要求的可见压力或代价词项。',
        },
      ],
      rewrite_routes: ['L4_DRAFT_EXECUTION', 'L5_DEAI_STYLE'],
    },
    nlcl_route_hints: {
      recommended_profiles: ['chapter', 'review', 'rewrite'],
      nlcl_layers: ['distillation_index', 'review'],
      route_terms: ['玄幻', '人物压力', '来源污染'],
      hard_filters: { genre: ['玄幻'] },
      negative_routes: ['只做文案建议', '跳过拒收条件'],
      safety_summary: '发现来源污染或人物压力失真时必须拒收并返回指定层重写。',
      feedback_default_layer: 'L6_REVIEW_ACCEPTANCE',
    },
  }
}

async function waitForSourceImport(
  request: import('@playwright/test').APIRequestContext,
  sourceImportId: string,
) {
  const deadline = Date.now() + 60_000
  while (Date.now() < deadline) {
    const response = await request.get(`${runtimeUrl}/api/v1/source-imports/${sourceImportId}`)
    expect(response.ok()).toBeTruthy()
    const job = await response.json() as { status: string; error?: string }
    if (job.status === 'awaiting_review') return job
    if (['failed', 'cancelled'].includes(job.status)) {
      throw new Error(`source import ${job.status}: ${job.error ?? 'unknown error'}`)
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error('source import did not reach awaiting_review')
}

async function assertDocumentWidth(page: import('@playwright/test').Page, maxWidth: number) {
  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document).toBeLessThanOrEqual(maxWidth)
  expect(widths.body).toBeLessThanOrEqual(maxWidth)
  return widths
}

async function scrollAdjacentChainTop(
  page: import('@playwright/test').Page,
  panel: import('@playwright/test').Locator,
) {
  await page.evaluate(() => {
    window.scrollTo(0, 0)
    for (const selector of ['.inspector', '.inspector-body', '.knowledge-panel', '.knowledge-workflow-stack']) {
      document.querySelectorAll(selector).forEach((element) => {
        ;(element as HTMLElement).scrollTop = 0
      })
    }
  })
  // Always re-resolve to avoid detached nodes after reload / mobile remount.
  const live = page.getByRole('region', { name: '相邻 Skill 连续链', exact: true })
  await expect(live).toBeVisible({ timeout: 30_000 })
  await live.scrollIntoViewIfNeeded()
  await expect(live.getByText('O2-D → O2-E 相邻连续链', { exact: true })).toBeVisible()
  await expect(live.getByText('O2-D', { exact: true }).first()).toBeVisible()
  return live
}


async function openKnowledgeAdjacentChain(page: import('@playwright/test').Page) {
  // Context Pack auto-open can steal the inspector tab after reload; keep re-asserting.
  await expect(async () => {
    const knowledgeTab = page.getByRole('tab', { name: '知识', exact: true })
    await knowledgeTab.click({ timeout: 5_000 })
    await expect(knowledgeTab).toHaveAttribute('aria-selected', 'true', { timeout: 2_000 })
    await expect(page.getByRole('region', { name: '相邻 Skill 连续链', exact: true })).toBeVisible({ timeout: 2_000 })
  }).toPass({ timeout: 45_000 })
  return page.getByRole('region', { name: '相邻 Skill 连续链', exact: true })
}

async function assertAdjacentChainSucceededVisible(page: import('@playwright/test').Page) {
  await expect(async () => {
    const knowledgeTab = page.getByRole('tab', { name: '知识', exact: true })
    await knowledgeTab.click({ timeout: 5_000 })
    await expect(knowledgeTab).toHaveAttribute('aria-selected', 'true', { timeout: 2_000 })
    const panel = page.getByRole('region', { name: '相邻 Skill 连续链', exact: true })
    await expect(panel).toBeVisible({ timeout: 2_000 })
    await expect(panel.getByRole('status', { name: '相邻链已完成', exact: true })).toContainText('Context Pack 已装配', { timeout: 2_000 })
    await expect(panel.getByText('连续交接完成', { exact: true }).first()).toBeVisible({ timeout: 2_000 })
  }).toPass({ timeout: 45_000 })
  return page.getByRole('region', { name: '相邻 Skill 连续链', exact: true })
}

function assertSucceededChain(record: AdjacentChainRecord, sourceImportId: string) {
  expect(record.schema).toBe('zhimeng.adjacent-skill-chain-run.v1')
  expect(record.chain_key).toBe('novel-distillation-kb-to-context')
  expect(record.status).toBe('succeeded')
  expect(record.source_import_id).toBe(sourceImportId)
  expect(record.formal_draft_allowed).toBe(false)
  expect(record.current_step_ordinal).toBe(2)
  expect(record.approval).toMatchObject({
    approved: true,
    approved_by: 'author',
    risk_acknowledged: true,
  })
  expect(record.permissions).toEqual({
    read: ['bound_project_snapshot', 'run_staging'],
    write: ['run_staging', 'runtime_context_store'],
    network: false,
    secrets: [],
  })
  expect(record.steps.map((step) => step.step_id)).toEqual([
    'o2d-kb-handoff',
    'o2e-context-handoff',
  ])
  expect(record.steps.every((step) => step.status === 'succeeded')).toBe(true)
  expect(record.skill_handoff_id).toMatch(/^skill-handoff-[0-9a-f]{32}$/)
  expect(record.context_handoff_id).toMatch(/^context-handoff-[0-9a-f]{32}$/)
  expect(record.context_pack_id).toMatch(/^context-pack-o2e-/)
  expect(record.context_pack_hash).toMatch(sha256Pattern)
  expect(record.route_receipt_id).toMatch(/^nlcl-route-/)
  expect(record.route_receipt_hash).toMatch(sha256Pattern)
  expect(record.steps[0].child_id).toBe(record.skill_handoff_id)
  expect(record.steps[1].child_id).toBe(record.context_handoff_id)
}

adjacentChainTest('adjacent chain prepares, single-approves, and runs O2-D then O2-E from the knowledge tab', async ({
  page,
  request,
  isolatedProject,
}) => {
  adjacentChainTest.setTimeout(420_000)

  const desktopPreparedPath = path.join(evidenceDir, 'adjacent-chain-prepared-desktop-1440x900.png')
  const mobileApprovedPath = path.join(evidenceDir, 'adjacent-chain-approved-mobile-390x844.png')
  const desktopSucceededPath = path.join(evidenceDir, 'adjacent-chain-succeeded-desktop-1440x900.png')
  const desktopRecoveredPath = path.join(evidenceDir, 'adjacent-chain-recovered-desktop-1440x900.png')
  const mobileRecoveredPath = path.join(evidenceDir, 'adjacent-chain-recovered-mobile-390x844.png')
  const desktopContextReviewPath = path.join(evidenceDir, 'adjacent-chain-context-review-desktop-1440x900.png')
  const desktopWritePrePath = path.join(evidenceDir, 'adjacent-chain-write-pre-desktop-1440x900.png')
  const desktopCandidatePath = path.join(evidenceDir, 'adjacent-chain-candidate-desktop-1440x900.png')
  const desktopChangeSetPath = path.join(evidenceDir, 'adjacent-chain-changeset-diff-desktop-1440x900.png')
  const desktopAppliedPath = path.join(evidenceDir, 'adjacent-chain-applied-desktop-1440x900.png')
  const desktopRolledBackPath = path.join(evidenceDir, 'adjacent-chain-rolled-back-desktop-1440x900.png')
  const acceptancePath = path.join(evidenceDir, 'adjacent-chain-acceptance-20260725.json')
  await Promise.all([
    desktopPreparedPath,
    mobileApprovedPath,
    desktopSucceededPath,
    desktopRecoveredPath,
    mobileRecoveredPath,
    desktopContextReviewPath,
    desktopWritePrePath,
    desktopCandidatePath,
    desktopChangeSetPath,
    desktopAppliedPath,
    desktopRolledBackPath,
    acceptancePath,
  ].map((target) => rm(target, { force: true })))

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 10_000 })

  const projectsResponse = await request.get(`${runtimeUrl}/api/v1/projects`, { maxRetries: 2 })
  expect(projectsResponse.ok()).toBeTruthy()
  const project = (await projectsResponse.json() as {
    projects: Array<{ project_id: string; root_path: string }>
  }).projects.find((item) => path.resolve(item.root_path) === path.resolve(isolatedProject.rootPath))
  expect(project).toBeTruthy()
  const projectId = (project as { project_id: string }).project_id
  expect(path.resolve((project as { root_path: string }).root_path)).toBe(
    path.resolve(isolatedProject.rootPath),
  )

  const beforeSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(beforeSnapshotResponse.ok()).toBeTruthy()
  const beforeSnapshot = await beforeSnapshotResponse.json() as {
    project: { project_id: string; revision: number; fingerprint: string }
    files: Array<{ file_id: string; relative_path: string }>
  }
  const prose = beforeSnapshot.files.find((file) => file.relative_path.endsWith('CH001_正文.md'))
  expect(prose).toBeTruthy()
  const proseId = (prose as { file_id: string }).file_id
  const beforeContentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(beforeContentResponse.ok()).toBeTruthy()
  const beforeContent = (await beforeContentResponse.json() as { content: string }).content
  expect(beforeContent).not.toContain(candidateLine)
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(isolatedProject.originalProseBytes)

  const localInspectResponse = await request.post(`${runtimeUrl}/api/v1/source-imports/local/inspect`, {
    data: { source_path: localSourcePath },
  })
  expect(localInspectResponse.ok()).toBeTruthy()
  const sourceCandidate = await localInspectResponse.json() as {
    source_path: string
    source_sha256: string
    source_size_bytes: number
    source_format: string
  }
  expect(sourceCandidate.source_sha256).toMatch(sha256Pattern)
  expect(sourceCandidate.source_format).toBe('txt')

  const sourceCreateResponse = await request.post(`${runtimeUrl}/api/v1/source-imports`, {
    data: {
      provider: 'local_file',
      query: sourceCandidate.source_path,
      candidate: sourceCandidate,
    },
  })
  expect(sourceCreateResponse.status()).toBe(202)
  const sourceImportId = (await sourceCreateResponse.json() as { import_id: string }).import_id
  expect(sourceImportId).toMatch(/^source-import-[0-9a-f]{32}$/)
  await waitForSourceImport(request, sourceImportId)

  const sourceReceiptResponse = await request.get(
    `${runtimeUrl}/api/v1/source-imports/${encodeURIComponent(sourceImportId)}/execution-receipt`,
  )
  expect(sourceReceiptResponse.ok()).toBeTruthy()
  const sourceReceiptRecord = await sourceReceiptResponse.json() as {
    lineage_status: string
    receipt: {
      receipt_id: string
      receipt_hash: string
      cards_draft_hash: string
      card_count: number
    }
  }
  expect(sourceReceiptRecord.lineage_status).toBe('verified')
  expect(sourceReceiptRecord.receipt.receipt_hash).toMatch(sha256Pattern)
  expect(sourceReceiptRecord.receipt.cards_draft_hash).toMatch(sha256Pattern)
  expect(sourceReceiptRecord.receipt.card_count).toBe(3)

  const knowledgeBase = `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/knowledge`
  const importCandidatesResponse = await request.post(
    `${knowledgeBase}/source-imports/${encodeURIComponent(sourceImportId)}/candidates`,
  )
  expect(importCandidatesResponse.status()).toBe(201)
  const importedCandidates = (await importCandidatesResponse.json() as {
    candidates: KnowledgeCandidate[]
  }).candidates
  expect(importedCandidates).toHaveLength(3)
  expect(importedCandidates.map((candidate) => candidate.card_type).sort()).toEqual(['validator', 'voice', 'work'])

  for (const candidate of importedCandidates) {
    const updateResponse = await request.put(
      `${knowledgeBase}/candidates/${encodeURIComponent(candidate.candidate_id)}`,
      {
        data: {
          expected_revision: candidate.revision,
          expected_content_hash: candidate.content_hash,
          card: reviewedCard(candidate.card_type, candidate.card_id),
        },
      },
    )
    expect(updateResponse.ok()).toBeTruthy()
    const reviewed = await updateResponse.json() as KnowledgeCandidate & { issues: unknown[] }
    expect(reviewed.issues).toEqual([])

    const promoteResponse = await request.post(
      `${knowledgeBase}/candidates/${encodeURIComponent(candidate.candidate_id)}/promote`,
      {
        data: {
          expected_revision: reviewed.revision,
          expected_content_hash: reviewed.content_hash,
          replace_active: false,
        },
      },
    )
    expect(promoteResponse.ok()).toBeTruthy()
  }

  const terminalCandidatesResponse = await request.get(
    `${knowledgeBase}/candidates?source_import_id=${encodeURIComponent(sourceImportId)}`,
  )
  expect(terminalCandidatesResponse.ok()).toBeTruthy()
  const terminalCandidates = await terminalCandidatesResponse.json() as {
    candidate_set_hash: string
    candidates: KnowledgeCandidate[]
  }
  expect(terminalCandidates.candidate_set_hash).toMatch(sha256Pattern)
  expect(terminalCandidates.candidates.every((candidate) => candidate.status === 'promoted')).toBe(true)

  const formalizeResponse = await request.post(
    `${knowledgeBase}/source-imports/${encodeURIComponent(sourceImportId)}/formalization/finalize`,
    {
      data: {
        expected_source_receipt_hash: sourceReceiptRecord.receipt.receipt_hash,
        expected_draft_hash: sourceReceiptRecord.receipt.cards_draft_hash,
        expected_candidate_set_hash: terminalCandidates.candidate_set_hash,
      },
    },
  )
  expect(formalizeResponse.ok()).toBeTruthy()
  const formalization = await formalizeResponse.json() as {
    schema: string
    formalization_receipt_id: string
    receipt_hash: string
    decisions: Array<{ decision: string }>
  }
  expect(formalization.schema).toBe('zhimeng.knowledge-formalization-receipt.v2')
  expect(formalization.formalization_receipt_id).toMatch(/^knowledge-formalization-[0-9a-f]{32}$/)
  expect(formalization.receipt_hash).toMatch(sha256Pattern)
  expect(formalization.decisions.map((decision) => decision.decision)).toEqual(['promoted', 'promoted', 'promoted'])

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 30_000 })
  const chainPanel = await openKnowledgeAdjacentChain(page)
  await scrollAdjacentChainTop(page, chainPanel)
  await expect(chainPanel).toContainText('等待正式知识批次')

  const prepareResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/projects/${projectId}${chainPathSuffix}/prepare`
  ), { timeout: 30_000 })
  await chainPanel.getByRole('button', { name: '准备连续链', exact: true }).click()
  const prepareResponse = await prepareResponsePromise
  expect(prepareResponse.ok()).toBeTruthy()
  const preparedPayload = await prepareResponse.json() as {
    record: AdjacentChainRecord
    not_claiming: string[]
  }
  const prepared = preparedPayload.record
  expect(prepared.status).toBe('prepared')
  expect(prepared.chain_key).toBe('novel-distillation-kb-to-context')
  expect(prepared.source_import_id).toBe(sourceImportId)
  expect(prepared.formal_draft_allowed).toBe(false)
  expect(prepared.steps.map((step) => step.step_id)).toEqual([
    'o2d-kb-handoff',
    'o2e-context-handoff',
  ])
  expect(prepared.steps[0].status).toBe('prepared')
  expect(prepared.steps[1].status).toBe('pending')
  expect(preparedPayload.not_claiming).toEqual([
    'four_skills_generic_exactly_once',
    'creation_suite_drafting',
    'reader_gate',
    'local_v1_exe',
  ])
  await expect(chainPanel).toContainText('相邻链已准备', { timeout: 30_000 })
  await expect(chainPanel.getByText('O2-D distillation -> KB handoff', { exact: true })).toBeVisible()
  await expect(chainPanel.getByText('O2-E KB index -> Context Pack', { exact: true })).toBeVisible()
  await scrollAdjacentChainTop(page, chainPanel)
  const desktopPreparedWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopPreparedPath, fullPage: false })

  const approveResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/projects/${projectId}${chainPathSuffix}/approve`
  ), { timeout: 30_000 })
  await chainPanel.getByRole('button', { name: '批准一次', exact: true }).click()
  const approveResponse = await approveResponsePromise
  expect(approveResponse.ok()).toBeTruthy()
  const approved = (await approveResponse.json() as { record: AdjacentChainRecord }).record
  expect(approved.status).toBe('approved')
  expect(approved.approval).toMatchObject({
    approved: true,
    approved_by: 'author',
    risk_acknowledged: true,
  })
  expect(approved.steps[0].status).toBe('approved')
  await expect(chainPanel).toContainText('已批准，等待连续运行', { timeout: 30_000 })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  await scrollAdjacentChainTop(page, chainPanel)
  await expect(chainPanel.getByRole('button', { name: '连续运行 O2-D→O2-E', exact: true })).toBeVisible()
  const mobileApprovedWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileApprovedPath, fullPage: false })

  const runResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/v1/projects/${projectId}${chainPathSuffix}/run`
  ), { timeout: 180_000 })
  await chainPanel.getByRole('button', { name: '连续运行 O2-D→O2-E', exact: true }).click()
  const runResponse = await runResponsePromise
  expect(runResponse.ok()).toBeTruthy()
  const succeeded = (await runResponse.json() as { record: AdjacentChainRecord }).record
  assertSucceededChain(succeeded, sourceImportId)
  await expect(chainPanel).toContainText('连续交接完成', { timeout: 30_000 })
  await expect(chainPanel.getByRole('status', { name: '相邻链已完成', exact: true })).toContainText('Context Pack 已装配')
  // Pack identity is verified via API; UI only needs stable success copy.

  await page.setViewportSize({ width: 1440, height: 900 })
  await scrollAdjacentChainTop(page, chainPanel)
  const result = chainPanel.locator('.skill-handoff-result')
  await result.evaluate((element) => element.scrollIntoView({ block: 'start', inline: 'nearest' }))
  await expect(result).toBeVisible()
  const desktopSucceededWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopSucceededPath, fullPage: false })

  const o2dResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/skill-handoffs/novel-distillation-kb`,
  )
  const o2eResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/context-handoffs/novel-kb-assemble`,
  )
  const chainRecoveredResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}${chainPathSuffix}`,
  )
  expect(o2dResponse.ok()).toBeTruthy()
  expect(o2eResponse.ok()).toBeTruthy()
  expect(chainRecoveredResponse.ok()).toBeTruthy()
  const o2d = await o2dResponse.json() as {
    record: {
      status: string
      handoff_id: string
      skill_receipt_id: string | null
      skill_receipt_hash: string | null
      formal_draft_allowed: boolean
      result: { status?: string; counts?: Record<KnowledgeCardType, number> } | null
    }
  }
  const o2e = await o2eResponse.json() as {
    record: {
      status: string
      context_handoff_id: string
      context_pack_id: string | null
      context_pack_hash: string | null
      route_receipt_id: string | null
      route_receipt_hash: string | null
      formal_draft_allowed: boolean
    }
  }
  const recoveredApi = (await chainRecoveredResponse.json() as { record: AdjacentChainRecord }).record
  expect(o2d.record.status).toBe('succeeded')
  expect(o2d.record.handoff_id).toBe(succeeded.skill_handoff_id)
  expect(o2d.record.formal_draft_allowed).toBe(false)
  expect(o2d.record.result).toMatchObject({
    status: 'ready_for_downstream',
    counts: { work: 1, voice: 1, validator: 1 },
  })
  expect(o2e.record.status).toBe('succeeded')
  expect(o2e.record.context_handoff_id).toBe(succeeded.context_handoff_id)
  expect(o2e.record.context_pack_id).toBe(succeeded.context_pack_id)
  expect(o2e.record.context_pack_hash).toBe(succeeded.context_pack_hash)
  expect(o2e.record.route_receipt_id).toBe(succeeded.route_receipt_id)
  expect(o2e.record.route_receipt_hash).toBe(succeeded.route_receipt_hash)
  expect(o2e.record.formal_draft_allowed).toBe(false)
  assertSucceededChain(recoveredApi, sourceImportId)
  expect(recoveredApi.chain_id).toBe(succeeded.chain_id)

  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 30_000 })
  let recoveredPanel = await assertAdjacentChainSucceededVisible(page)
  recoveredPanel = await scrollAdjacentChainTop(page, recoveredPanel)
  // Re-assert once more right before capture, because Context Pack auto-open may steal the tab.
  recoveredPanel = await assertAdjacentChainSucceededVisible(page)
  const desktopRecoveredWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopRecoveredPath, fullPage: false })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  recoveredPanel = await assertAdjacentChainSucceededVisible(page)
  recoveredPanel = await scrollAdjacentChainTop(page, recoveredPanel)
  recoveredPanel = await assertAdjacentChainSucceededVisible(page)
  const recoveredResult = recoveredPanel.locator('.skill-handoff-result')
  await expect(recoveredResult).toBeVisible()
  const mobileRecoveredWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileRecoveredPath, fullPage: false })


  // Author-controlled bridge: open the O2-E Context Pack for review without claiming formal draft.
  await page.setViewportSize({ width: 1440, height: 900 })
  let bridgePanel = await assertAdjacentChainSucceededVisible(page)
  const openContextPromise = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false
    const pathname = new URL(response.url()).pathname
    return pathname.startsWith('/api/v1/agent/context-packs/')
      && !pathname.endsWith('/nlcl-route')
      && !pathname.includes('/prewrite-operation-package')
      && !pathname.includes('/executions/')
  }, { timeout: 30_000 })
  await bridgePanel.getByRole('button', { name: '打开 Context 审查', exact: true }).click()
  const openContextResponse = await openContextPromise
  expect(openContextResponse.ok()).toBeTruthy()
  const openedPack = await openContextResponse.json() as {
    pack: {
      context_pack_id: string
      pack_hash: string
      formal_draft_allowed?: boolean
      active_chapter_id?: string | null
      objective?: string
    }
    consumed_by_run_id?: string | null
  }
  expect(openedPack.pack.context_pack_id).toBe(succeeded.context_pack_id)
  expect(openedPack.pack.pack_hash).toBe(succeeded.context_pack_hash)
  expect(openedPack.pack.formal_draft_allowed ?? false).toBe(false)
  expect(openedPack.consumed_by_run_id ?? null).toBeNull()
  await expect(page.getByRole('tab', { name: 'Context', exact: true })).toHaveAttribute('aria-selected', 'true', { timeout: 30_000 })
  const contextPanel = page.locator('.context-pack-panel')
  await expect(contextPanel).toBeVisible({ timeout: 30_000 })
  await expect(contextPanel).toContainText('Context Pack', { timeout: 30_000 })
  await expect(contextPanel).toContainText(/未写回|不写正式正文|formal_draft_allowed|正文尚未写回|只生成/)
  await expect(contextPanel.getByLabel('写前链就绪')).toContainText('写前链就绪 · 可确认运行', { timeout: 30_000 })
  await expect(contextPanel.getByLabel('写前链就绪')).toContainText('项目审计 → 写前门禁 → 创作交接')
  await expect(contextPanel.getByLabel('写前链就绪')).toContainText('formal_draft_allowed=false')
  await expect(contextPanel.getByText(/novel-creation-suite\.project-audit/)).toBeVisible()
  await expect(contextPanel.getByText(/novel-creation-suite\.prewrite-gate/)).toBeVisible()
  await expect(contextPanel.getByText(/novel-creation-suite\.creation-handoff/)).toBeVisible()
  // Author-review mode produces a ChangeSet Diff candidate after write-pre.
  await contextPanel.getByRole('radio', { name: '正文候选', exact: true }).click()
  await expect(contextPanel.getByRole('radio', { name: '正文候选', exact: true })).toHaveAttribute('aria-checked', 'true')
  const confirmButton = contextPanel.getByRole('button', { name: '确认并生成候选', exact: true })
  await expect(confirmButton).toBeEnabled({ timeout: 30_000 })
  const desktopContextReviewWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopContextReviewPath, fullPage: false })

  const createRunPromise = page.waitForResponse((response) => {
    if (response.request().method() !== 'POST') return false
    const pathname = new URL(response.url()).pathname
    return /\/api\/v1\/agent\/sessions\/[^/]+\/runs$/.test(pathname)
  }, { timeout: 30_000 })
  await confirmButton.click()
  const createRunResponse = await createRunPromise
  if (!(createRunResponse.ok() || createRunResponse.status() === 202)) {
    let bodyText = ''
    try {
      bodyText = await createRunResponse.text()
    } catch {
      bodyText = '<unreadable body>'
    }
    throw new Error(
      `create-run failed: status=${createRunResponse.status()} body=${bodyText}`,
    )
  }
  const createdRun = await createRunResponse.json() as {
    run?: {
      run_id?: string
      status?: string
      context_pack_id?: string | null
    }
    run_id?: string
  }
  const runId = createdRun.run?.run_id ?? createdRun.run_id
  expect(runId).toMatch(/^run[-_]/)
  await expect(page.getByRole('tab', { name: 'Agent', exact: true })).toHaveAttribute('aria-selected', 'true', { timeout: 30_000 })

  // Managed write-pre chain: project-audit -> prewrite-gate -> creation-handoff.
  // Skills live in the open drawer content, not inside the tablist labeled 运行详情.
  await page.getByRole('tab', { name: /Skills/ }).click()
  const skillCards = page.locator('.skill-activity-card')
  try {
    await expect(skillCards).toHaveCount(3, { timeout: 90_000 })
  } catch (error) {
    const failedRunResponse = await request.get(
      `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(String(runId))}`,
    )
    const failedRunBody = await failedRunResponse.text()
    const failedActivitiesResponse = await request.get(
      `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(String(runId))}/activities`,
    )
    const failedActivitiesBody = await failedActivitiesResponse.text()
    throw new Error(
      `write-pre skills missing; run=${failedRunBody}; activities=${failedActivitiesBody}; original=${String(error)}`,
    )
  }
  await expect(skillCards.filter({ hasText: 'novel-creation-suite.project-audit' })).toHaveCount(1)
  await expect(skillCards.filter({ hasText: 'novel-creation-suite.prewrite-gate' })).toHaveCount(1)
  await expect(skillCards.filter({ hasText: 'novel-creation-suite.creation-handoff' })).toHaveCount(1)
  await expect(skillCards.locator('.skill-status')).toHaveText(['执行成功', '执行成功', '执行成功'])

  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  await expect(page.getByText(/NLCL 写前链|写前链执行中|创作交接|写前门禁|项目审计/).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/formal_draft_allowed=false|正式正文未授权|正文尚未写回|仅生成待审候选|不会自动写回正文|正文候选尚未写回/).first()).toBeVisible()
  const desktopWritePreWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopWritePrePath, fullPage: false })

  // Wait for managed write-pre + provider candidate to reach a terminal Run.
  let runRecord: { run_id: string; status: string } | null = null
  await expect.poll(async () => {
    const agentRunResponse = await request.get(`${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(String(runId))}`)
    if (!agentRunResponse.ok()) return `http-${agentRunResponse.status()}`
    const runPayload = await agentRunResponse.json() as {
      run: { run_id: string; status: string }
    }
    runRecord = runPayload.run
    return runPayload.run.status
  }, { timeout: 90_000 }).toMatch(/^(succeeded|failed|cancelled|interrupted)$/)
  expect(runRecord).not.toBeNull()
  expect(runRecord!.run_id).toBe(runId)
  expect(runRecord!.status).toBe('succeeded')

  const activitiesResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(String(runId))}/activities`,
  )
  expect(activitiesResponse.ok()).toBeTruthy()
  const activities = await activitiesResponse.json() as {
    activities: Array<{
      kind: string
      payload?: {
        skill_id?: string
        tool_name?: string
        status?: string
        proposal_id?: string
        output?: {
          audit?: { formal_draft_allowed?: boolean }
          prewrite_gate?: { formal_draft_allowed?: boolean }
          creation_handoff?: { formal_draft_allowed?: boolean }
          receipt?: { formal_draft_allowed?: boolean }
          accepted?: boolean
          formal_draft_allowed?: boolean
          candidate_kind?: string
        }
      }
    }>
  }
  const writePreSkillIds = activities.activities
    .map((activity) => activity.payload?.skill_id)
    .filter((skillId): skillId is string => typeof skillId === 'string')
  let writePreFormalDraftAllowed = false
  for (const activity of activities.activities) {
    const output = activity.payload?.output
    if (output?.audit?.formal_draft_allowed) writePreFormalDraftAllowed = true
    if (output?.prewrite_gate?.formal_draft_allowed) writePreFormalDraftAllowed = true
    if (output?.creation_handoff?.formal_draft_allowed) writePreFormalDraftAllowed = true
    if (output?.receipt?.formal_draft_allowed) writePreFormalDraftAllowed = true
  }
  expect(writePreFormalDraftAllowed).toBe(false)
  expect(writePreSkillIds.some((skillId) => skillId.includes('project-audit'))).toBeTruthy()
  expect(writePreSkillIds.some((skillId) => skillId.includes('prewrite-gate'))).toBeTruthy()
  expect(writePreSkillIds.some((skillId) => skillId.includes('creation-handoff'))).toBeTruthy()

  const toolNames = activities.activities
    .filter((activity) => activity.kind === 'tool_call')
    .map((activity) => activity.payload?.tool_name)
    .filter((name): name is string => typeof name === 'string')
  expect(toolNames).toEqual(expect.arrayContaining([
    'skill_execute',
    'skill_execute',
    'skill_execute',
    'change_set_propose',
    'candidate_output_submit',
  ]))
  expect(toolNames.filter((name) => name === 'skill_execute')).toHaveLength(3)
  expect(toolNames.filter((name) => name === 'change_set_propose')).toHaveLength(1)
  expect(toolNames.filter((name) => name === 'candidate_output_submit')).toHaveLength(1)

  const candidateResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/runs/${encodeURIComponent(String(runId))}/candidate-output`,
  )
  expect(candidateResponse.ok()).toBeTruthy()
  const candidateOutput = await candidateResponse.json() as {
    schema?: string
    candidate_output_id?: string
    kind?: string
    formal_draft_allowed?: boolean
    requires_author_review?: boolean
    handoff_status?: string
    content?: { title?: string; summary?: string; body?: string }
    references?: Array<{ reference_kind?: string; reference_id?: string }>
  }
  expect(candidateOutput.kind).toBe('change_proposal')
  expect(candidateOutput.formal_draft_allowed).toBe(false)
  expect(candidateOutput.requires_author_review ?? true).toBe(true)
  expect(candidateOutput.content?.body).toBeTruthy()

  // Author-visible candidate surface after O2 write-pre.
  await page.getByLabel('运行详情').getByRole('tab', { name: /候选/ }).click()
  const writerTrace = page.getByRole('article', { name: 'Writer 任务与候选结果' })
  await expect(writerTrace).toBeVisible({ timeout: 30_000 })
  await expect(writerTrace).toContainText(/正文候选任务|author_review_change_proposal|候选结果|Writer/)
  await expect(page.getByText(/正式正文未授权|正文候选尚未写回|formal_draft_allowed=false|待作者审阅/).first()).toBeVisible()
  await expect(page.locator('.candidate-output-card, .candidate-output-panel').first()).toBeVisible({ timeout: 30_000 })
  if (candidateOutput.content?.title) {
    await expect(page.getByText(candidateOutput.content.title).first()).toBeVisible()
  }
  const desktopCandidateWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopCandidatePath, fullPage: false })

  // Author-visible ChangeSet Diff from O2 managed path.
  await page.getByLabel('运行详情').getByRole('tab', { name: /Timeline/ }).click()
  const proposalActivity = page.locator('.timeline-change_proposal')
  await expect(proposalActivity).toHaveCount(1, { timeout: 30_000 })
  const proposal = activities.activities.find((activity) => activity.kind === 'change_proposal') as {
    kind: string
    payload?: { proposal_id?: string }
  } | undefined
  expect(proposal?.payload?.proposal_id).toBeTruthy()
  const changeSetId = String(proposal?.payload?.proposal_id)
  const changeSetResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}`,
  )
  expect(changeSetResponse.ok()).toBeTruthy()
  const changeSet = await changeSetResponse.json() as {
    change_set_id: string
    title?: string
    source_run_id?: string | null
    source_tool_call_id?: string | null
  }
  expect(changeSet.change_set_id).toBe(changeSetId)
  expect(changeSet.source_run_id).toBe(runId)

  await proposalActivity.getByRole('button', { name: '审阅改动', exact: true }).click()
  await expect(page.getByRole('tab', { name: '改动', exact: true })).toHaveAttribute('aria-selected', 'true', { timeout: 30_000 })
  const selectedChangeSet = page.locator('.changeset-row[aria-current="true"]')
  await expect(selectedChangeSet).toBeVisible({ timeout: 30_000 })
  await expect(selectedChangeSet).toContainText('正文候选改动')
  await expect(page.getByRole('heading', { name: '正文候选改动', exact: true })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('tab', { name: /Diff/ }).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('main').getByText(candidateLine, { exact: true })).toBeVisible({ timeout: 30_000 })
  const desktopChangeSetWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopChangeSetPath, fullPage: false })

  // Route execution receipts remain formal-draft-closed.
  for (const ordinal of [1, 2, 3]) {
    const executionResponse = await request.get(
      `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(openedPack.pack.context_pack_id)}/nlcl-route/executions/${ordinal}`,
    )
    expect(executionResponse.ok()).toBeTruthy()
    const execution = await executionResponse.json() as {
      status: string
      receipt?: { domain_status?: string | null } | null
    }
    expect(execution.status).toBe('succeeded')
  }

  // Project truth stays unchanged through O2 write-pre / Diff until author Gate+Apply.
  const preApplySnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(preApplySnapshotResponse.ok()).toBeTruthy()
  const preApplySnapshot = await preApplySnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(preApplySnapshot.project).toMatchObject({
    revision: beforeSnapshot.project.revision,
    fingerprint: beforeSnapshot.project.fingerprint,
  })
  const preApplyContentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(preApplyContentResponse.ok()).toBeTruthy()
  expect((await preApplyContentResponse.json() as { content: string }).content).toBe(beforeContent)
  expect(await readFile(isolatedProject.prosePath)).toEqual(isolatedProject.originalProseBytes)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(isolatedProject.originalProseBytes)

  // Author-controlled Gate -> Apply -> byte-exact Rollback on the isolated O2 ChangeSet.
  await page.getByRole('button', { name: /接受 .* Hunk 1/ }).click()
  const applyButton = page.getByRole('button', { name: '应用所选更改' })
  await expect(page.getByLabel('Gate 状态：未运行')).toBeVisible()
  await expect(applyButton).toBeDisabled()
  await expect(applyButton).toHaveAttribute('aria-describedby', 'changeset-gate-apply-note')

  await page.getByRole('button', { name: '运行 Gate' }).click()
  await expect(page.getByLabel('Gate 状态：通过')).toBeVisible({ timeout: 45_000 })
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
  expect(gate.receipt?.candidate_hash).toMatch(sha256Pattern)
  expect(gate.receipt?.quality_assessed).toBe(false)
  expect(gate.receipt?.formal_draft_allowed).toBe(false)

  const afterGateContentResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(proseId)}`,
  )
  expect(afterGateContentResponse.ok()).toBeTruthy()
  expect((await afterGateContentResponse.json() as { content: string }).content).toBe(beforeContent)
  const afterGateSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(afterGateSnapshotResponse.ok()).toBeTruthy()
  const afterGateSnapshot = await afterGateSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(afterGateSnapshot.project).toMatchObject({
    revision: beforeSnapshot.project.revision,
    fingerprint: beforeSnapshot.project.fingerprint,
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
    base_revision: beforeSnapshot.project.revision,
    result_revision: beforeSnapshot.project.revision + 1,
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
  expect(appliedBytes.toString('utf8')).toContain(candidateLine)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(isolatedProject.originalProseBytes)

  const appliedSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(appliedSnapshotResponse.ok()).toBeTruthy()
  const appliedSnapshot = await appliedSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
    files: Array<{ relative_path: string; sha256: string }>
  }
  const appliedProse = appliedSnapshot.files.find((file) => file.relative_path.endsWith('CH001_正文.md'))
  expect(appliedSnapshot.project).toMatchObject({
    revision: beforeSnapshot.project.revision + 1,
    fingerprint: applied.writeback?.result_fingerprint,
  })
  expect(applied.writeback?.files[0].after_sha256).toBe(appliedProse?.sha256)
  await expect(page.locator('.statusbar').getByText(
    `project rev ${beforeSnapshot.project.revision + 1}`,
    { exact: true },
  )).toBeVisible({ timeout: 30_000 })

  const draftTab = page.getByRole('tab', { name: '本稿', exact: true })
  const chapterEditor = page.getByLabel('章节正文编辑器')
  await expect(async () => {
    await draftTab.click()
    await expect(draftTab).toHaveAttribute('aria-selected', 'true')
    await expect(chapterEditor).toBeVisible()
  }).toPass({ timeout: 30_000 })
  await expect(chapterEditor).toContainText(candidateLine, { timeout: 30_000 })

  const evidenceAfterApplyResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}/writeback-evidence`,
  )
  expect(evidenceAfterApplyResponse.ok()).toBeTruthy()
  const evidenceAfterApply = await evidenceAfterApplyResponse.json() as {
    schema?: string
    receipt?: {
      receipt_id?: string
      outcome?: string
      formal_draft_allowed?: boolean
      quality_assessed?: boolean
      novel_state_synced?: boolean
      result_revision?: number
      context_pack_id?: string | null
      route_receipt_id?: string | null
    } | null
    history?: Array<{ outcome?: string }>
  }
  expect(evidenceAfterApply.schema).toBe('zhimeng.writeback-evidence-response.v1')
  expect(evidenceAfterApply.receipt?.outcome).toBe('applied')
  expect(evidenceAfterApply.receipt?.formal_draft_allowed).toBe(false)
  expect(evidenceAfterApply.receipt?.quality_assessed).toBe(false)
  expect(evidenceAfterApply.receipt?.novel_state_synced).toBe(false)
  expect(evidenceAfterApply.receipt?.result_revision).toBe(beforeSnapshot.project.revision + 1)
  expect(evidenceAfterApply.history).toHaveLength(1)
  const writebackEvidence = page.getByRole('region', { name: '写回证据', exact: true })
  await expect(writebackEvidence).toBeVisible({ timeout: 30_000 })
  await expect(writebackEvidence).toContainText('formal_draft_allowed=false')

  const desktopAppliedWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopAppliedPath, fullPage: false })

  await rollbackButton.click()
  await expect(page.getByText(`项目结果 revision ${beforeSnapshot.project.revision + 2}`)).toBeVisible({
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
    result_revision: beforeSnapshot.project.revision + 2,
    outcome: 'rolled_back',
  })
  expect(rolledBack.rollback?.files).toHaveLength(1)

  const rolledBackBytes = await readFile(isolatedProject.prosePath)
  expect(rolledBackBytes).toEqual(isolatedProject.originalProseBytes)
  expect(await readFile(isolatedProject.sourceProsePath)).toEqual(isolatedProject.originalProseBytes)

  const rolledBackSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(rolledBackSnapshotResponse.ok()).toBeTruthy()
  const rolledBackSnapshot = await rolledBackSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
    files: Array<{ relative_path: string; sha256: string }>
  }
  const rolledBackProse = rolledBackSnapshot.files.find((file) => file.relative_path.endsWith('CH001_正文.md'))
  expect(rolledBackSnapshot.project).toMatchObject({
    revision: beforeSnapshot.project.revision + 2,
    fingerprint: rolledBack.rollback?.result_fingerprint,
  })
  expect(rolledBack.rollback?.files[0].after_sha256).toBe(rolledBackProse?.sha256)
  await expect(page.locator('.statusbar').getByText(
    `project rev ${beforeSnapshot.project.revision + 2}`,
    { exact: true },
  )).toBeVisible({ timeout: 30_000 })
  await expect(async () => {
    await draftTab.click()
    await expect(draftTab).toHaveAttribute('aria-selected', 'true')
    await expect(chapterEditor).toBeVisible()
  }).toPass({ timeout: 30_000 })
  await expect(chapterEditor).toContainText('雨落在旧档案馆的玻璃窗上。')
  await expect(chapterEditor).not.toContainText(candidateLine, { timeout: 30_000 })

  const evidenceAfterRollbackResponse = await request.get(
    `${runtimeUrl}/api/v1/change-sets/${encodeURIComponent(changeSetId)}/writeback-evidence`,
  )
  expect(evidenceAfterRollbackResponse.ok()).toBeTruthy()
  const evidenceAfterRollback = await evidenceAfterRollbackResponse.json() as {
    receipt?: {
      outcome?: string
      result_revision?: number
      formal_draft_allowed?: boolean
      novel_state_synced?: boolean
      receipt_id?: string
    } | null
    history?: Array<{ outcome?: string }>
  }
  expect(evidenceAfterRollback.receipt?.outcome).toBe('rolled_back')
  expect(evidenceAfterRollback.receipt?.result_revision).toBe(beforeSnapshot.project.revision + 2)
  expect(evidenceAfterRollback.receipt?.formal_draft_allowed).toBe(false)
  expect(evidenceAfterRollback.receipt?.novel_state_synced).toBe(false)
  expect(evidenceAfterRollback.history?.map((item) => item.outcome)).toEqual([
    'applied',
    'rolled_back',
  ])
  await expect(page.getByRole('region', { name: '写回证据', exact: true })).toContainText('已回滚')

  const desktopRolledBackWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopRolledBackPath, fullPage: false })

  await writeFile(
    acceptancePath,
    JSON.stringify({
      schema: 'zhimeng.adjacent-chain-acceptance.v1',
      generated_at: new Date().toISOString(),
      selected_edge: 'novel-distillation -> novel-kb-manager.import-distillation-index -> assemble-context / Runtime Context Pack -> author confirm write-pre chain -> author-visible change_proposal Diff -> Gate -> Apply -> writeback evidence -> Rollback',
      adjacent_chain_slice_only: true,
      write_pre_chain_slice: true,
      author_candidate_slice: true,
      author_changeset_diff_slice: true,
      author_gate_apply_rollback_slice: true,
      author_writeback_evidence_slice: true,
      not_claiming: [
        'four_skills_generic_exactly_once',
        'creation_suite_drafting',
        'reader_gate',
        'local_v1_exe',
        'literary_quality_gate',
        'original_nlcl_writeback_semantics',
      ],
      local_url: page.url(),
      project: {
        project_id: projectId,
        root_path: isolatedProject.rootPath,
        base_revision: beforeSnapshot.project.revision,
        base_fingerprint: beforeSnapshot.project.fingerprint,
        final_revision: rolledBackSnapshot.project.revision,
        final_fingerprint: rolledBackSnapshot.project.fingerprint,
        source_fixture_unchanged: true,
        isolated_project: true,
      },
      source_import: {
        source_import_id: sourceImportId,
        provider: 'local_file',
        source_path: sourceCandidate.source_path,
        source_sha256: sourceCandidate.source_sha256,
        receipt_id: sourceReceiptRecord.receipt.receipt_id,
        receipt_hash: sourceReceiptRecord.receipt.receipt_hash,
        cards_draft_hash: sourceReceiptRecord.receipt.cards_draft_hash,
      },
      formalization: {
        schema: formalization.schema,
        receipt_id: formalization.formalization_receipt_id,
        receipt_hash: formalization.receipt_hash,
        candidate_types: terminalCandidates.candidates.map((candidate) => candidate.card_type),
        candidate_statuses: terminalCandidates.candidates.map((candidate) => candidate.status),
      },
      adjacent_chain: {
        chain_id: succeeded.chain_id,
        chain_key: succeeded.chain_key,
        status: succeeded.status,
        route: succeeded.route,
        steps: succeeded.steps,
        approval: succeeded.approval,
        permissions: succeeded.permissions,
        formal_draft_allowed: succeeded.formal_draft_allowed,
        skill_handoff_id: succeeded.skill_handoff_id,
        context_handoff_id: succeeded.context_handoff_id,
        context_pack_id: succeeded.context_pack_id,
        context_pack_hash: succeeded.context_pack_hash,
        route_receipt_id: succeeded.route_receipt_id,
        route_receipt_hash: succeeded.route_receipt_hash,
        current_step_ordinal: succeeded.current_step_ordinal,
      },
      child_handoffs: {
        o2d: {
          handoff_id: o2d.record.handoff_id,
          status: o2d.record.status,
          skill_receipt_id: o2d.record.skill_receipt_id,
          skill_receipt_hash: o2d.record.skill_receipt_hash,
          formal_draft_allowed: o2d.record.formal_draft_allowed,
          counts: o2d.record.result?.counts ?? null,
        },
        o2e: {
          context_handoff_id: o2e.record.context_handoff_id,
          status: o2e.record.status,
          context_pack_id: o2e.record.context_pack_id,
          context_pack_hash: o2e.record.context_pack_hash,
          route_receipt_id: o2e.record.route_receipt_id,
          route_receipt_hash: o2e.record.route_receipt_hash,
          formal_draft_allowed: o2e.record.formal_draft_allowed,
        },
      },
      reload_recovery: {
        chain_id: recoveredApi.chain_id,
        status: recoveredApi.status,
        context_pack_id: recoveredApi.context_pack_id,
        context_pack_hash: recoveredApi.context_pack_hash,
        route_receipt_id: recoveredApi.route_receipt_id,
        route_receipt_hash: recoveredApi.route_receipt_hash,
        matches_original: true,
      },
      screenshots: {
        desktop_prepared: desktopPreparedPath,
        mobile_approved: mobileApprovedPath,
        desktop_succeeded: desktopSucceededPath,
        desktop_recovered: desktopRecoveredPath,
        mobile_recovered: mobileRecoveredPath,
        desktop_context_review: desktopContextReviewPath,
        desktop_write_pre: desktopWritePrePath,
        desktop_candidate: desktopCandidatePath,
        desktop_changeset_diff: desktopChangeSetPath,
        desktop_applied: desktopAppliedPath,
        desktop_rolled_back: desktopRolledBackPath,
      },
      layout_widths: {
        desktop_prepared: desktopPreparedWidths,
        mobile_approved: mobileApprovedWidths,
        desktop_succeeded: desktopSucceededWidths,
        desktop_recovered: desktopRecoveredWidths,
        mobile_recovered: mobileRecoveredWidths,
        desktop_context_review: desktopContextReviewWidths,
        desktop_write_pre: desktopWritePreWidths,
        desktop_candidate: desktopCandidateWidths,
        desktop_changeset_diff: desktopChangeSetWidths,
        desktop_applied: desktopAppliedWidths,
        desktop_rolled_back: desktopRolledBackWidths,
      },
      context_review_bridge: {
        context_pack_id: openedPack.pack.context_pack_id,
        context_pack_hash: openedPack.pack.pack_hash,
        formal_draft_allowed: openedPack.pack.formal_draft_allowed ?? false,
        consumed_by_run_id: openedPack.consumed_by_run_id ?? null,
        author_controlled_open: true,
        write_pre_ready: true,
      },
      write_pre_chain: {
        run_id: runId,
        run_status: runRecord!.status,
        skill_ids: writePreSkillIds,
        formal_draft_allowed: writePreFormalDraftAllowed,
        chain: [
          'novel-creation-suite.project-audit',
          'novel-creation-suite.prewrite-gate',
          'novel-creation-suite.creation-handoff',
        ],
        project_truth_unchanged_until_author_apply: true,
      },
      author_candidate: {
        run_id: runId,
        run_status: runRecord!.status,
        writer_mode: 'author_review_change_proposal',
        candidate_output_id: candidateOutput.candidate_output_id ?? null,
        schema: candidateOutput.schema ?? null,
        kind: candidateOutput.kind ?? null,
        formal_draft_allowed: candidateOutput.formal_draft_allowed ?? false,
        requires_author_review: candidateOutput.requires_author_review ?? true,
        handoff_status: candidateOutput.handoff_status ?? null,
        title: candidateOutput.content?.title ?? null,
        project_truth_unchanged_until_author_apply: true,
      },
      author_changeset_diff: {
        change_set_id: changeSetId,
        title: changeSet.title ?? null,
        source_run_id: changeSet.source_run_id ?? null,
        source_tool_call_id: changeSet.source_tool_call_id ?? null,
        formal_draft_allowed: false,
        project_truth_unchanged_until_author_apply: true,
        author_opened_diff: true,
      },
      author_gate_apply_rollback: {
        change_set_id: changeSetId,
        gate_status: gate.receipt?.status ?? null,
        gate_candidate_hash: gate.receipt?.candidate_hash ?? null,
        gate_quality_assessed: gate.receipt?.quality_assessed ?? null,
        gate_formal_draft_allowed: gate.receipt?.formal_draft_allowed ?? null,
        applied_status: applied.status,
        applied_revision: applied.writeback?.result_revision ?? null,
        applied_fingerprint: applied.writeback?.result_fingerprint ?? null,
        applied_contains_candidate_line: true,
        rolled_back_status: rolledBack.status,
        rolled_back_revision: rolledBack.rollback?.result_revision ?? null,
        rolled_back_fingerprint: rolledBack.rollback?.result_fingerprint ?? null,
        byte_exact_rollback: true,
        source_fixture_unchanged: true,
        candidate_line: candidateLine,
      },
      author_writeback_evidence: {
        after_apply_receipt_id: evidenceAfterApply.receipt?.receipt_id ?? null,
        after_apply_outcome: evidenceAfterApply.receipt?.outcome ?? null,
        after_apply_result_revision: evidenceAfterApply.receipt?.result_revision ?? null,
        after_apply_context_pack_id: evidenceAfterApply.receipt?.context_pack_id ?? null,
        after_apply_route_receipt_id: evidenceAfterApply.receipt?.route_receipt_id ?? null,
        after_rollback_receipt_id: evidenceAfterRollback.receipt?.receipt_id ?? null,
        after_rollback_outcome: evidenceAfterRollback.receipt?.outcome ?? null,
        after_rollback_result_revision: evidenceAfterRollback.receipt?.result_revision ?? null,
        formal_draft_allowed: false,
        quality_assessed: false,
        novel_state_synced: false,
        history_outcomes: evidenceAfterRollback.history?.map((item) => item.outcome) ?? [],
        not_claiming_original_nlcl_writeback_semantics: true,
      },
    }, null, 2),
    'utf-8',
  )
})

