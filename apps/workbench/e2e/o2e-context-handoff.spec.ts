import { expect, test } from '@playwright/test'
import { createHash } from 'node:crypto'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
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

interface O2dRecord {
  handoff_id: string
  status: string
  project: { project_id: string; revision: number; fingerprint: string }
  workflow: {
    workflow_plan_id: string
    workflow_plan_hash: string
    checkpoint_id: string
    checkpoint_hash: string
  } | null
  upstream: {
    source_import_id: string
    formalization_receipt_id: string
    formalization_receipt_hash: string
    cards: Array<{ card_id: string; card_type: KnowledgeCardType; version_id: string; content_hash: string }>
  } | null
  activation: {
    managed_skill_id: string
    skill_version_id: string
    skill_version: string
    manifest_fingerprint: string
    package_fingerprint: string
  } | null
  approval: { approved: boolean; approved_by: string | null; risk_acknowledged: boolean }
  permissions: { read: string[]; write: string[]; network: boolean; secrets: string[] }
  formal_draft_allowed: boolean
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  artifact_hash: string | null
  output_artifact_path: string | null
  result: {
    status?: string
    downstream_target?: string
    distillation_index_hash?: string
    counts?: Record<KnowledgeCardType, number>
    entries?: Array<{ card_id: string; card_type: KnowledgeCardType; version_id: string; content_hash: string }>
  } | null
}

interface O2eRecord {
  context_handoff_id: string
  handoff_key: string
  status: string
  project: { project_id: string; revision: number; fingerprint: string }
  workflow: {
    workflow_plan_id: string
    workflow_plan_hash: string
    checkpoint_id: string
    checkpoint_hash: string
    tool_call_id: string
    skill_call_id: string
  } | null
  upstream: {
    source_handoff_id: string
    source_skill_receipt_id: string
    source_skill_receipt_hash: string
    source_result_hash: string
    distillation_index_hash: string
    distillation_index_artifact_path: string
    distillation_index_artifact_file_hash: string
    source_project_revision: number
    source_project_fingerprint: string
    upstream_activation_skill_version_id: string
    entries: Array<{
      card_id: string
      card_type: KnowledgeCardType
      version_id: string
      content_hash: string
      title: string
      route_terms: string[]
      tags: string[]
      safety_summary: string
    }>
    no_full_card_text_copied: boolean
  } | null
  downstream: { target_skill_id: string; capability: string; contract: string }
  route: {
    profile: string
    stage: string
    chapter_id: string
    task_intent: string
    route_terms: string[]
  }
  activation: {
    managed_skill_id: string
    skill_version_id: string
    skill_version: string
    manifest_fingerprint: string
    package_fingerprint: string
    effective_scope: string
  } | null
  approval: { approved: boolean; approved_by: string | null; risk_acknowledged: boolean }
  permissions: { read: string[]; write: string[]; network: boolean; secrets: string[] }
  constraints: Record<string, unknown>
  formal_draft_allowed: boolean
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  artifact_hash: string | null
  output_artifact_path: string | null
  context_pack_id: string | null
  context_pack_hash: string | null
  route_receipt_id: string | null
  route_receipt_hash: string | null
  result: {
    status?: string
    source_handoff_id?: string
    distillation_index_hash?: string
    downstream_target?: string
    context_pack_id?: string
    context_pack_hash?: string
    route_receipt_id?: string
    route_receipt_hash?: string
    route_health?: string
    route_evidence_status?: string
    context_health?: string
    readiness?: string
    profile?: string
    stage?: string
    chapter_id?: string
    route_terms?: string[]
    selected_entries?: Array<{ card_id: string; card_type: KnowledgeCardType; version_id: string; content_hash: string }>
    constraints?: Record<string, unknown>
  } | null
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

function sha256Bytes(value: Buffer | string) {
  return createHash('sha256').update(value).digest('hex')
}

function assertO2d(record: O2dRecord, sourceImportId: string) {
  expect(record.status).toBe('succeeded')
  expect(record.upstream?.source_import_id).toBe(sourceImportId)
  expect(record.upstream?.cards.map((card) => card.card_type)).toEqual(['work', 'voice', 'validator'])
  expect(record.activation?.managed_skill_id).toBe('novel-distillation.kb-handoff')
  expect(record.approval).toMatchObject({ approved: true, approved_by: 'author', risk_acknowledged: true })
  expect(record.permissions).toEqual({
    read: ['bound_project_snapshot'],
    write: ['run_staging'],
    network: false,
    secrets: [],
  })
  expect(record.formal_draft_allowed).toBe(false)
  expect(record.skill_receipt_id).toMatch(/^skill-receipt-[0-9a-f]{32}$/)
  expect(record.skill_receipt_hash).toMatch(sha256Pattern)
  expect(record.result_hash).toMatch(sha256Pattern)
  expect(record.artifact_hash).toMatch(sha256Pattern)
  expect(record.result).toMatchObject({
    status: 'ready_for_downstream',
    downstream_target: 'novel-kb-manager.import-distillation-index',
    counts: { work: 1, voice: 1, validator: 1 },
  })
  expect(record.result?.distillation_index_hash).toMatch(sha256Pattern)
}

function assertO2e(record: O2eRecord, o2d: O2dRecord) {
  expect(record.status).toBe('succeeded')
  expect(record.handoff_key).toBe('novel-kb-import-index-to-assemble-context')
  expect(record.upstream?.source_handoff_id).toBe(o2d.handoff_id)
  expect(record.upstream?.source_skill_receipt_id).toBe(o2d.skill_receipt_id)
  expect(record.upstream?.source_skill_receipt_hash).toBe(o2d.skill_receipt_hash)
  expect(record.upstream?.source_result_hash).toBe(o2d.result_hash)
  expect(record.upstream?.distillation_index_hash).toBe(o2d.result?.distillation_index_hash)
  expect(record.upstream?.entries.map((entry) => entry.card_type)).toEqual(['work', 'voice', 'validator'])
  expect(record.upstream?.no_full_card_text_copied).toBe(true)
  expect(record.downstream).toMatchObject({
    target_skill_id: 'novel-kb-manager',
    capability: 'assemble-context',
  })
  expect(record.activation).toMatchObject({
    managed_skill_id: 'novel-kb.context-handoff',
    skill_version: '2026.07.23-o2e.1',
    effective_scope: 'project',
  })
  expect(record.activation?.manifest_fingerprint).toMatch(sha256Pattern)
  expect(record.activation?.package_fingerprint).toMatch(sha256Pattern)
  expect(record.workflow?.workflow_plan_id).toMatch(/^workflow-plan-[0-9a-f]{32}$/)
  expect(record.workflow?.workflow_plan_hash).toMatch(sha256Pattern)
  expect(record.workflow?.checkpoint_id).toMatch(/^workflow-checkpoint-[0-9a-f]{32}$/)
  expect(record.workflow?.checkpoint_hash).toMatch(sha256Pattern)
  expect(record.approval).toMatchObject({ approved: true, approved_by: 'author', risk_acknowledged: true })
  expect(record.permissions).toEqual({
    read: ['bound_project_snapshot', 'run_staging'],
    write: ['run_staging', 'runtime_context_store'],
    network: false,
    secrets: [],
  })
  expect(record.formal_draft_allowed).toBe(false)
  expect(record.constraints).toMatchObject({
    no_full_card_text_copied: true,
    formal_draft_allowed: false,
    project_truth_not_written: true,
    route_health_ready_is_not_validated: true,
    writes_formal_prose: false,
  })
  expect(record.skill_receipt_id).toMatch(/^skill-receipt-[0-9a-f]{32}$/)
  expect(record.skill_receipt_hash).toMatch(sha256Pattern)
  expect(record.context_pack_id).toMatch(/^context-pack-o2e-/)
  expect(record.context_pack_hash).toMatch(sha256Pattern)
  expect(record.route_receipt_id).toMatch(/^nlcl-route-[0-9a-f]{32}$/)
  expect(record.route_receipt_hash).toMatch(sha256Pattern)
  expect(record.result).toMatchObject({
    status: 'context_ready',
    downstream_target: 'novel-kb-manager.assemble-context',
    route_health: 'ready',
    route_evidence_status: 'not_validated',
    profile: 'chapter',
    stage: 'draft',
    chapter_id: 'CH001',
  })
  expect(record.result?.selected_entries?.map((entry) => entry.card_type)).toEqual(['work', 'voice', 'validator'])
}

async function scrollContextPanelTop(
  page: import('@playwright/test').Page,
  panel: import('@playwright/test').Locator,
) {
  await page.evaluate(() => {
    window.scrollTo(0, 0)
    for (const selector of ['.inspector', '.inspector-body', '.knowledge-panel', '.knowledge-workflow-stack']) {
      document.querySelectorAll<HTMLElement>(selector).forEach((element) => {
        element.scrollTop = 0
      })
    }
  })
  await panel.scrollIntoViewIfNeeded()
  await expect(panel.getByText('O2-D distillation_index', { exact: true })).toBeInViewport()
  await expect(panel.getByText('novel-kb-manager', { exact: true })).toBeInViewport()
  await expect(panel.getByText('Runtime Context Pack', { exact: true })).toBeInViewport()
}

test('O2-E consumes the real O2-D managed index and persists a recoverable Context Pack', async ({ page, request }) => {
  test.setTimeout(360_000)

  const desktopPreparedPath = path.join(evidenceDir, 'o2e-context-handoff-prepared-desktop-1440x900.png')
  const mobileOverviewPath = path.join(evidenceDir, 'o2e-context-handoff-overview-mobile-390x844.png')
  const mobileApprovedActionPath = path.join(evidenceDir, 'o2e-context-handoff-approved-action-mobile-390x844.png')
  const desktopSucceededPath = path.join(evidenceDir, 'o2e-context-handoff-succeeded-desktop-1440x900.png')
  const desktopRecoveredPath = path.join(evidenceDir, 'o2e-context-handoff-recovered-desktop-1440x900.png')
  const mobileRecoveredPath = path.join(evidenceDir, 'o2e-context-handoff-recovered-mobile-390x844.png')
  const acceptancePath = path.join(evidenceDir, 'o2e-context-handoff-acceptance-20260723.json')
  await Promise.all([
    desktopPreparedPath,
    mobileOverviewPath,
    mobileApprovedActionPath,
    desktopSucceededPath,
    desktopRecoveredPath,
    mobileRecoveredPath,
    acceptancePath,
  ].map((target) => rm(target, { force: true })))

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

  const beforeSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(beforeSnapshotResponse.ok()).toBeTruthy()
  const beforeSnapshot = await beforeSnapshotResponse.json() as {
    project: { project_id: string; revision: number; fingerprint: string }
  }

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

  const o2dPrepareResponse = await request.post(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/skill-handoffs/novel-distillation-kb/prepare`,
    {
      data: {
        expected_project_revision: beforeSnapshot.project.revision,
        expected_project_fingerprint: beforeSnapshot.project.fingerprint,
        source_import_id: sourceImportId,
      },
    },
  )
  expect(o2dPrepareResponse.ok()).toBeTruthy()
  const o2dPrepared = (await o2dPrepareResponse.json() as { record: O2dRecord }).record
  expect(o2dPrepared.status).toBe('prepared')

  const o2dApproveResponse = await request.post(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/skill-handoffs/novel-distillation-kb/approve`,
    { data: { expected_handoff_id: o2dPrepared.handoff_id } },
  )
  expect(o2dApproveResponse.ok()).toBeTruthy()

  const o2dRunResponse = await request.post(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/skill-handoffs/novel-distillation-kb/run`,
    { data: { expected_handoff_id: o2dPrepared.handoff_id }, timeout: 90_000 },
  )
  expect(o2dRunResponse.ok()).toBeTruthy()
  const o2d = (await o2dRunResponse.json() as { record: O2dRecord }).record
  assertO2d(o2d, sourceImportId)

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('tab', { name: '知识', exact: true }).click()
  const contextPanel = page.getByRole('region', { name: '受控入口到 Context Pack', exact: true })
  await scrollContextPanelTop(page, contextPanel)
  await expect(contextPanel).toContainText('等待受控入口')

  const prepareResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname
      === `/api/v1/projects/${projectId}/context-handoffs/novel-kb-assemble/prepare`
  ))
  await contextPanel.getByRole('button', { name: '准备 Context Pack', exact: true }).click()
  const prepareResponse = await prepareResponsePromise
  expect(prepareResponse.ok()).toBeTruthy()
  const prepared = (await prepareResponse.json() as { record: O2eRecord }).record
  expect(prepared.status).toBe('prepared')
  expect(prepared.upstream?.source_handoff_id).toBe(o2d.handoff_id)
  expect(prepared.upstream?.distillation_index_hash).toBe(o2d.result?.distillation_index_hash)
  await expect(contextPanel).toContainText('Context Pack 待批准')
  await expect(contextPanel.getByText('作品机制入口', { exact: true })).toBeVisible()
  await expect(contextPanel.getByText('叙事声音入口', { exact: true })).toBeVisible()
  await expect(contextPanel.getByText('校验闸门入口', { exact: true })).toBeVisible()
  await scrollContextPanelTop(page, contextPanel)
  const desktopPreparedWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopPreparedPath, fullPage: false })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  await scrollContextPanelTop(page, contextPanel)
  await expect(contextPanel.getByText('Context Pack 待批准', { exact: true })).toBeInViewport()
  const mobileOverviewWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileOverviewPath, fullPage: false })

  const actions = contextPanel.locator('.context-handoff-actions')
  await actions.scrollIntoViewIfNeeded()
  await expect(actions.getByRole('button', { name: '批准', exact: true })).toBeInViewport()
  const approveResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname
      === `/api/v1/projects/${projectId}/context-handoffs/novel-kb-assemble/approve`
  ))
  await actions.getByRole('button', { name: '批准', exact: true }).click()
  const approveResponse = await approveResponsePromise
  expect(approveResponse.ok()).toBeTruthy()
  const approved = (await approveResponse.json() as { record: O2eRecord }).record
  expect(approved.status).toBe('approved')
  await expect(contextPanel).toContainText('已批准，等待装配')
  await actions.scrollIntoViewIfNeeded()
  await expect(actions.getByRole('button', { name: '运行 assemble-context', exact: true })).toBeInViewport()
  const mobileApprovedActionWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileApprovedActionPath, fullPage: false })

  const runResponsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname
      === `/api/v1/projects/${projectId}/context-handoffs/novel-kb-assemble/run`
  ), { timeout: 90_000 })
  await actions.getByRole('button', { name: '运行 assemble-context', exact: true }).click()
  const runResponse = await runResponsePromise
  expect(runResponse.ok()).toBeTruthy()
  const succeeded = (await runResponse.json() as { record: O2eRecord }).record
  assertO2e(succeeded, o2d)
  await expect(contextPanel).toContainText('Context Pack 已生成', { timeout: 30_000 })
  await expect(contextPanel).toContainText('正文尚未写回')

  const contextPackResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(succeeded.context_pack_id as string)}`,
  )
  const routeResponse = await request.get(
    `${runtimeUrl}/api/v1/agent/context-packs/${encodeURIComponent(succeeded.context_pack_id as string)}/nlcl-route`,
  )
  expect(contextPackResponse.ok()).toBeTruthy()
  expect(routeResponse.ok()).toBeTruthy()
  const contextPackRecord = await contextPackResponse.json() as {
    schema: string
    pack: {
      context_pack_id: string
      pack_hash: string
      project_id: string
      project_revision: number
      project_fingerprint: string
      knowledge_fingerprint: string | null
      project_kb_manifest_hash: string | null
      included: Array<{ source_id: string; source_hash: string; content: string }>
      excluded: unknown[]
    }
  }
  const route = await routeResponse.json() as {
    schema: string
    route_receipt_id: string
    receipt_hash: string
    context_pack_id: string
    context_pack_hash: string
    project_id: string
    project_revision: number
    project_fingerprint: string
    project_kb_manifest_hash: string | null
    knowledge_fingerprint: string | null
    source: string
    status: string
    formal_draft_allowed: boolean
    project_truth_precedence: boolean
    whole_library_load_forbidden: boolean
    selected_cards: Array<{
      card_id: string
      card_type: KnowledgeCardType
      version_id: string
      content_hash: string
      route_terms: string[]
      safety_summary: string
    }>
    warnings: string[]
  }
  expect(contextPackRecord).toMatchObject({
    schema: 'zhimeng.context-pack-record.v1',
  })
  expect(contextPackRecord.pack).toMatchObject({
    context_pack_id: succeeded.context_pack_id,
    pack_hash: succeeded.context_pack_hash,
    project_id: projectId,
    project_revision: beforeSnapshot.project.revision,
    project_fingerprint: beforeSnapshot.project.fingerprint,
    knowledge_fingerprint: o2d.result?.distillation_index_hash,
    project_kb_manifest_hash: o2d.result?.distillation_index_hash,
  })
  expect(contextPackRecord.pack.included).toHaveLength(3)
  expect(contextPackRecord.pack.excluded).toEqual([])
  expect(contextPackRecord.pack.included.every((item) => item.source_id.startsWith('knowledge-card:'))).toBe(true)
  for (const included of contextPackRecord.pack.included) {
    expect(included.content).not.toContain('"summary"')
    expect(included.content).not.toContain('"reader_promise"')
    expect(included.content).not.toContain('"type_payload"')
  }
  expect(route).toMatchObject({
    schema: 'zhimeng.nlcl-route-receipt.v1',
    route_receipt_id: succeeded.route_receipt_id,
    receipt_hash: succeeded.route_receipt_hash,
    context_pack_id: succeeded.context_pack_id,
    context_pack_hash: succeeded.context_pack_hash,
    project_id: projectId,
    project_revision: beforeSnapshot.project.revision,
    project_fingerprint: beforeSnapshot.project.fingerprint,
    project_kb_manifest_hash: o2d.result?.distillation_index_hash,
    knowledge_fingerprint: o2d.result?.distillation_index_hash,
    source: 'external_distillation',
    status: 'ready',
    formal_draft_allowed: false,
    project_truth_precedence: true,
    whole_library_load_forbidden: true,
  })
  expect(route.selected_cards.map((card) => card.card_type).sort()).toEqual(['validator', 'voice', 'work'])
  expect(route.warnings).toEqual(expect.arrayContaining([
    'route_health_ready_not_validated',
    'project_truth_not_written',
  ]))

  const outputArtifactPath = succeeded.output_artifact_path as string
  const outputArtifactBytes = await readFile(outputArtifactPath)
  const outputArtifact = JSON.parse(outputArtifactBytes.toString('utf-8')) as {
    schema: string
    source_handoff_id: string
    distillation_index_hash: string
    selected_entries: Array<Record<string, unknown>>
    constraints: Record<string, unknown>
  }
  expect(outputArtifact.schema).toBe('zhimeng.o2e-context-pack-intent.v1')
  expect(outputArtifact.source_handoff_id).toBe(o2d.handoff_id)
  expect(outputArtifact.distillation_index_hash).toBe(o2d.result?.distillation_index_hash)
  expect(outputArtifact.selected_entries).toHaveLength(3)
  for (const entry of outputArtifact.selected_entries) {
    expect(entry).not.toHaveProperty('summary')
    expect(entry).not.toHaveProperty('reader_promise')
    expect(entry).not.toHaveProperty('type_payload')
    expect(entry).not.toHaveProperty('content')
  }

  await page.setViewportSize({ width: 1440, height: 900 })
  const result = contextPanel.locator('.context-handoff-result')
  await result.scrollIntoViewIfNeeded()
  await expect(result).toBeInViewport()
  const desktopSucceededWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopSucceededPath, fullPage: false })

  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByText('minimal-novel').first()).toBeVisible({ timeout: 30_000 })
  const contextTab = page.getByRole('tab', { name: 'Context', exact: true })
  await expect(contextTab).toHaveAttribute('aria-selected', 'true')
  const knowledgeTab = page.getByRole('tab', { name: '知识', exact: true })
  await knowledgeTab.click()
  await expect(knowledgeTab).toHaveAttribute('aria-selected', 'true')
  const recoveredPanel = page.getByRole('region', { name: '受控入口到 Context Pack', exact: true })
  await scrollContextPanelTop(page, recoveredPanel)
  await expect(recoveredPanel).toContainText('Context Pack 已生成')
  const recoveredResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/context-handoffs/novel-kb-assemble`,
  )
  expect(recoveredResponse.ok()).toBeTruthy()
  const recovered = (await recoveredResponse.json() as { record: O2eRecord }).record
  assertO2e(recovered, o2d)
  expect(recovered.context_handoff_id).toBe(succeeded.context_handoff_id)
  expect(recovered.context_pack_id).toBe(succeeded.context_pack_id)
  expect(recovered.context_pack_hash).toBe(succeeded.context_pack_hash)
  expect(recovered.route_receipt_id).toBe(succeeded.route_receipt_id)
  expect(recovered.route_receipt_hash).toBe(succeeded.route_receipt_hash)
  expect(recovered.activation?.skill_version_id).toBe(succeeded.activation?.skill_version_id)
  const desktopRecoveredWidths = await assertDocumentWidth(page, 1440)
  await page.screenshot({ path: desktopRecoveredPath, fullPage: false })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tablist', { name: '移动端工作区' }).getByRole('tab', { name: /Agent/ }).click()
  await scrollContextPanelTop(page, recoveredPanel)
  await expect(recoveredPanel.getByText('Context Pack 已生成', { exact: true }).first()).toBeInViewport()
  const recoveredResult = recoveredPanel.locator('.context-handoff-result')
  await expect(recoveredResult).toBeInViewport()
  const mobileRecoveredWidths = await assertDocumentWidth(page, 390)
  await page.screenshot({ path: mobileRecoveredPath, fullPage: false })

  const afterSnapshotResponse = await request.get(
    `${runtimeUrl}/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
  )
  expect(afterSnapshotResponse.ok()).toBeTruthy()
  const afterSnapshot = await afterSnapshotResponse.json() as {
    project: { revision: number; fingerprint: string }
  }
  expect(afterSnapshot.project).toMatchObject({
    revision: beforeSnapshot.project.revision,
    fingerprint: beforeSnapshot.project.fingerprint,
  })

  await writeFile(
    acceptancePath,
    JSON.stringify({
      schema: 'zhimeng.o2e-context-handoff-acceptance.v1',
      generated_at: new Date().toISOString(),
      selected_edge: 'novel-kb-manager.import-distillation-index -> novel-kb-manager.assemble-context / Runtime Context Pack',
      o2e_slice_only: true,
      not_claiming: [
        'creation_suite_drafting',
        'reader_gate',
        'four_skills_generic_orchestration',
        'local_v1_exe',
      ],
      local_url: page.url(),
      project: {
        project_id: projectId,
        revision: beforeSnapshot.project.revision,
        fingerprint: beforeSnapshot.project.fingerprint,
        unchanged_after_context_handoff: true,
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
      upstream_o2d: {
        handoff_id: o2d.handoff_id,
        status: o2d.status,
        workflow: o2d.workflow,
        activation: o2d.activation,
        approval: o2d.approval,
        permissions: o2d.permissions,
        skill_receipt_id: o2d.skill_receipt_id,
        skill_receipt_hash: o2d.skill_receipt_hash,
        result_hash: o2d.result_hash,
        artifact_hash: o2d.artifact_hash,
        output_artifact_path: o2d.output_artifact_path,
        distillation_index_hash: o2d.result?.distillation_index_hash,
        counts: o2d.result?.counts,
      },
      context_handoff: {
        context_handoff_id: succeeded.context_handoff_id,
        status: succeeded.status,
        workflow: succeeded.workflow,
        upstream: succeeded.upstream,
        downstream: succeeded.downstream,
        route: succeeded.route,
        activation: succeeded.activation,
        approval: succeeded.approval,
        permissions: succeeded.permissions,
        constraints: succeeded.constraints,
        formal_draft_allowed: succeeded.formal_draft_allowed,
        skill_receipt_id: succeeded.skill_receipt_id,
        skill_receipt_hash: succeeded.skill_receipt_hash,
        result_hash: succeeded.result_hash,
        artifact_hash: succeeded.artifact_hash,
        output_artifact_path: outputArtifactPath,
        output_artifact_file_sha256: sha256Bytes(outputArtifactBytes),
      },
      context_pack: {
        context_pack_id: contextPackRecord.pack.context_pack_id,
        pack_hash: contextPackRecord.pack.pack_hash,
        project_kb_manifest_hash: contextPackRecord.pack.project_kb_manifest_hash,
        knowledge_fingerprint: contextPackRecord.pack.knowledge_fingerprint,
        included_source_ids: contextPackRecord.pack.included.map((item) => item.source_id),
        excluded_count: contextPackRecord.pack.excluded.length,
        formal_draft_allowed: contextPackRecord.pack.formal_draft_allowed,
      },
      nlcl_route: {
        route_receipt_id: route.route_receipt_id,
        receipt_hash: route.receipt_hash,
        source: route.source,
        status: route.status,
        formal_draft_allowed: route.formal_draft_allowed,
        project_truth_precedence: route.project_truth_precedence,
        whole_library_load_forbidden: route.whole_library_load_forbidden,
        selected_cards: route.selected_cards,
        warnings: route.warnings,
      },
      reload_recovery: {
        context_handoff_id: recovered.context_handoff_id,
        context_pack_id: recovered.context_pack_id,
        context_pack_hash: recovered.context_pack_hash,
        route_receipt_id: recovered.route_receipt_id,
        route_receipt_hash: recovered.route_receipt_hash,
        activation_skill_version_id: recovered.activation?.skill_version_id,
        matches_original: true,
      },
      screenshots: {
        desktop_prepared: desktopPreparedPath,
        mobile_overview: mobileOverviewPath,
        mobile_approved_action: mobileApprovedActionPath,
        desktop_succeeded: desktopSucceededPath,
        desktop_recovered: desktopRecoveredPath,
        mobile_recovered: mobileRecoveredPath,
      },
      layout_widths: {
        desktop_prepared: desktopPreparedWidths,
        mobile_overview: mobileOverviewWidths,
        mobile_approved_action: mobileApprovedActionWidths,
        desktop_succeeded: desktopSucceededWidths,
        desktop_recovered: desktopRecoveredWidths,
        mobile_recovered: mobileRecoveredWidths,
      },
    }, null, 2),
    'utf-8',
  )
})
