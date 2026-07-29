import type {
  KnowledgeCandidate,
  KnowledgeCandidateList,
  KnowledgeCard,
  KnowledgeCardList,
  KnowledgeFormalizationRecord,
  KnowledgeFormalizationReceipt,
  KnowledgeScopedCardList,
  KnowledgeScopeRecord,
  ProjectKbStateRecord,
} from '../api/knowledge'
import type { SourceImportExecutionReceiptRecord } from '../api/sourceImports'

const projectKbStateIdFixture = `project-kb-state-${'6'.repeat(32)}`
const projectKbManifestHashFixture = '7'.repeat(64)

export function currentProjectKbStateFixture({
  projectId = 'project-0123456789abcdef01234567',
  revision = 1,
  fingerprint = 'd'.repeat(64),
  stateId = projectKbStateIdFixture,
  manifestHash = projectKbManifestHashFixture,
  knowledgeScopeStateRevision = 1,
  knowledgeFingerprint = 'e'.repeat(64),
}: {
  projectId?: string
  revision?: number
  fingerprint?: string
  stateId?: string
  manifestHash?: string
  knowledgeScopeStateRevision?: number
  knowledgeFingerprint?: string
} = {}): ProjectKbStateRecord {
  const stateHash = '8'.repeat(64)
  return {
    schema: 'zhimeng.project-kb-state-record.v1',
    project_id: projectId,
    status: 'current',
    runtime_relative_root: `project-kb/${projectId}.kb`,
    pointer: {
      schema: 'zhimeng.project-kb-current-pointer.v1',
      project_id: projectId,
      state_id: stateId,
      state_hash: stateHash,
      manifest_hash: manifestHash,
      manifest_file_sha256: '9'.repeat(64),
      pointer_hash: 'a'.repeat(64),
    },
    manifest: {
      schema: 'zhimeng.project-kb-state.v1',
      state_id: stateId,
      state_hash: stateHash,
      project: {
        schema: 'zhimeng.project-kb-project-ref.v1',
        project_id: projectId,
        revision,
        fingerprint,
      },
      knowledge_scope: {
        schema: 'zhimeng.knowledge-scope-ref.v1',
        scope_id: `knowledge-scope-${'1'.repeat(32)}`,
        scope_type: 'project',
        project_binding_id: projectId,
        state_revision: knowledgeScopeStateRevision,
      },
      knowledge_fingerprint: knowledgeFingerprint,
      inherited_card_ids: [],
      source_of_truth: {
        schema: 'zhimeng.project-kb-source-of-truth.v1',
        project_files: 'work_truth',
        runtime_sqlite: 'run_truth',
        derived: true,
        rebuildable: true,
      },
      counts: {
        candidates: 3,
        cards: 3,
        active_cards: 3,
        formalization_receipts: 1,
        active_index_entries: 3,
      },
      artifacts: [],
      manifest_hash: manifestHash,
    },
    reasons: [],
  }
}

export const reviewedWorkCardFixture: Record<string, unknown> = {
  card_id: 'work-native-fixture',
  card_type: 'work',
  title: '阶段兑现循环',
  source: {
    name: '测试作品',
    scope: '完整作品跨章节结构复核',
    evidence_quality: 'medium',
    notes: '跨章节结构与独立样本复核完成。',
  },
  taxonomy: {
    genre: ['玄幻'],
    scene_function: ['阶段兑现'],
    emotion: ['压迫', '释放'],
    craft: ['升级循环'],
  },
  tags: ['玄幻', '升级', '兑现'],
  keywords: ['阶段兑现', '升级循环'],
  applicable_tasks: ['chapter_brief', 'review'],
  summary: '用压力、选择、代价与阶段兑现组成可变化的推进循环，并让回报改变人物下一步行动空间。',
  reader_promise: '每次承担代价都会带来可见回报，同时留下更高层的新债务。',
  reader_effect: {
    author_intent: '让选择同时推进力量、关系和后续危机。',
    observed_reader_effect: '读者在小兑现后追问下一次选择的更高代价。',
    effect_evidence: ['跨章节样本都在兑现后留下了可追踪的新债务。'],
    effect_failure_boundary: '只有升级说明而没有选择与代价时会失效。',
    misread_risk: '只复制升级名词会丢失完整因果关系。',
    when_it_turns_bad: '回报不改变人物处境或新债务与选择无关时。',
  },
  type_payload: {
    premise_pattern: '主角在资源受限时承担代价换取行动空间。',
    core_reader_promise: '选择带来可见回报并抬高下一次代价。',
    opening_contract: '先展示可见损失，再给出只能冒险验证的机会。',
    progression_map: ['压力外显后做选择', '付出代价后小兑现'],
    recurring_scene_loops: ['资源受限 -> 风险试探 -> 小兑现 -> 更高压力'],
    transfer_limits: ['不得复制人物', '不得复制设定壳', '不得复刻场景顺序'],
  },
  mechanics: [{
    name: '代价绑定的阶段兑现',
    use_when: '章节需要同时推进力量与关系时。',
    rule: '先做不可撤销的选择，再用小兑现证明选择有效。',
    failure_mode: '先给奖励再补代价会削弱选择重量。',
  }],
  validation_tests: ['兑现是否改变下一步选择。', '新增债务是否来自本章代价。'],
  validation_ladder: Object.fromEntries([
    'internal_consistency',
    'predictive_validation',
    'blind_sample_validation',
    'extreme_scenario_stress_test',
  ].map((step) => [step, { status: 'pass', evidence: [`${step} 已完成独立验证。`] }])),
  scores: { evidence: 0.7, specificity: 0.7, generativity: 0.7, retrieval: 0.6, safety_risk: 0.2 },
  nlcl_route_hints: {
    recommended_profiles: ['chapter', 'review'],
    route_terms: ['玄幻', '升级'],
    negative_routes: ['纯日常'],
  },
  safety: {
    copyright_boundary: '只保留可迁移机制，不保存来源表达。',
    do_not_reuse: ['来源人物', '来源设定壳', '来源场景顺序'],
  },
  draft_candidate: false,
  requires_human_distillation: false,
}

const sourceImportId = `source-import-${'b'.repeat(32)}`
const sourceReceiptId = `source-import-receipt-${'1'.repeat(32)}`
const candidateId = `knowledge-candidate-${'a'.repeat(32)}`
const versionId = `knowledge-version-${'e'.repeat(32)}`
const formalizationReceiptId = `knowledge-formalization-${'7'.repeat(32)}`

const candidateOrigin = {
  schema: 'zhimeng.knowledge-candidate-origin.v1' as const,
  source_receipt_id: sourceReceiptId,
  source_receipt_hash: '2'.repeat(64),
  source_packet_fingerprint: '3'.repeat(64),
  draft_fingerprint: '4'.repeat(64),
  cards_draft_hash: '5'.repeat(64),
  source_card_index: 0,
  source_card_hash: '6'.repeat(64),
  origin_hash: '7'.repeat(64),
}

const sourceArtifacts = [
  'downloaded_epub',
  'packet_epub',
  'packet_manifest',
  'packet_metadata',
  'packet_text',
].map((kind, index) => ({
  kind,
  relative_path: `source/${kind}`,
  size_bytes: index + 100,
  sha256: String(index + 1).repeat(64),
}))

const draftArtifacts = [
  'cards_draft_json',
  'distillation_report_md',
  'tool_result_envelope_json',
  'validation_report_md',
].map((kind, index) => ({
  kind,
  relative_path: `draft/${kind}`,
  size_bytes: index + 200,
  sha256: String(index + 5).repeat(64),
}))

export const knowledgeSourceReceiptFixture: SourceImportExecutionReceiptRecord = {
  schema: 'zhimeng.source-import-execution-receipt-record.v1',
  source_import_id: sourceImportId,
  lineage_status: 'verified',
  receipt: {
    schema: 'zhimeng.source-import-execution-receipt.v1',
    receipt_id: sourceReceiptId,
    source_import_id: sourceImportId,
    run_id: `source-import-run-${'3'.repeat(32)}`,
    revision: 1,
    idempotency_key: `source-import-idempotency-${'4'.repeat(32)}`,
    lineage_status: 'verified',
    provider: 'tomato',
    request: {
      query: '测试作品',
      candidate_index: 1,
      book_id: '123456',
      title: '测试作品',
      author: '测试作者',
    },
    request_hash: '1'.repeat(64),
    retry_of: null,
    prepare_invocation_id: `source-import-invocation-${'1'.repeat(32)}`,
    prepare_tool: {
      tool_id: 'tomato.prepare',
      tool_version: '1',
      tool_fingerprint: '2'.repeat(64),
    },
    prepare_input_hash: '3'.repeat(64),
    prepare_output_hash: '4'.repeat(64),
    download_scope: 'full',
    source_packet_fingerprint: candidateOrigin.source_packet_fingerprint,
    packet_manifest_hash: '7'.repeat(64),
    packet_text_hash: '5'.repeat(64),
    packet_metadata_hash: '4'.repeat(64),
    source_artifacts: sourceArtifacts,
    distillation_invocation_id: `source-import-invocation-${'2'.repeat(32)}`,
    distillation_tool: {
      tool_id: 'novel-distillation.run',
      tool_version: '1',
      tool_fingerprint: '8'.repeat(64),
    },
    distillation_input_hash: '9'.repeat(64),
    distillation_output_hash: 'a'.repeat(64),
    draft_fingerprint: candidateOrigin.draft_fingerprint,
    cards_draft_hash: candidateOrigin.cards_draft_hash,
    envelope_hash: '7'.repeat(64),
    envelope_schema: 'novel_distillation.source_packet_draft.v1',
    envelope_status: 'draft_ready',
    card_count: 1,
    draft_artifacts: draftArtifacts,
    created_at: '2026-07-20T07:55:00Z',
    receipt_hash: candidateOrigin.source_receipt_hash,
  },
}

export const knowledgeCandidateFixture: KnowledgeCandidate = {
  schema: 'zhimeng.knowledge-candidate.v2',
  candidate_id: candidateId,
  source_import_id: sourceImportId,
  source_card_index: 0,
  source_draft_hash: candidateOrigin.cards_draft_hash,
  lineage_status: 'verified',
  origin: candidateOrigin,
  card_id: 'work-native-fixture',
  card_type: 'work',
  status: 'pending',
  revision: 1,
  content_hash: 'd'.repeat(64),
  card: { ...reviewedWorkCardFixture, draft_candidate: true, requires_human_distillation: true },
  issues: [
    { code: 'draft_flag_present', field: 'draft_candidate', message: 'Clear the draft flag' },
  ],
  promoted_version_id: null,
  created_at: '2026-07-20T08:00:00Z',
  updated_at: '2026-07-20T08:00:00Z',
}

export const knowledgeCandidateListFixture: KnowledgeCandidateList = {
  schema: 'zhimeng.knowledge-candidate-list.v2',
  candidates: [knowledgeCandidateFixture],
  candidate_set_hash: 'b'.repeat(64),
}

const lineageReceipt = {
  schema: 'zhimeng.knowledge-version-lineage-receipt.v1' as const,
  lineage_receipt_id: `knowledge-lineage-${'c'.repeat(32)}`,
  version_id: versionId,
  card_id: 'work-native-fixture',
  version_content_hash: 'f'.repeat(64),
  candidate_id: candidateId,
  approved_candidate_revision: 1,
  approved_candidate_content_hash: 'f'.repeat(64),
  candidate_origin: candidateOrigin,
  source_import_id: sourceImportId,
  activation_mode: 'initial' as const,
  replacement_authorized: false,
  activation_expected_version_id: versionId,
  activation_expected_state_revision: 1,
  created_at: '2026-07-20T08:10:00Z',
  receipt_hash: 'c'.repeat(64),
}

export const knowledgeFormalizationReceiptFixture: KnowledgeFormalizationReceipt = {
  schema: 'zhimeng.knowledge-formalization-receipt.v1',
  formalization_receipt_id: formalizationReceiptId,
  source_import_id: sourceImportId,
  source_receipt_id: sourceReceiptId,
  source_receipt_hash: candidateOrigin.source_receipt_hash,
  source_packet_fingerprint: candidateOrigin.source_packet_fingerprint,
  draft_fingerprint: candidateOrigin.draft_fingerprint,
  cards_draft_hash: candidateOrigin.cards_draft_hash,
  candidate_set_hash: 'b'.repeat(64),
  decisions: [{
    candidate_id: candidateId,
    source_card_index: 0,
    origin_hash: candidateOrigin.origin_hash,
    decision: 'promoted',
    revision: 1,
    content_hash: 'f'.repeat(64),
    promoted_version_id: versionId,
    version_lineage_receipt_id: lineageReceipt.lineage_receipt_id,
    version_lineage_receipt_hash: lineageReceipt.receipt_hash,
  }],
  knowledge_state_hash: 'd'.repeat(64),
  created_at: '2026-07-20T08:12:00Z',
  receipt_hash: 'e'.repeat(64),
}

export const knowledgeAwaitingFormalizationFixture: KnowledgeFormalizationRecord = {
  schema: 'zhimeng.knowledge-formalization-record.v1',
  source_import_id: sourceImportId,
  status: 'awaiting_review',
  receipt: null,
}

export const knowledgeFormalizationFixture: KnowledgeFormalizationRecord = {
  ...knowledgeAwaitingFormalizationFixture,
  status: 'verified',
  receipt: knowledgeFormalizationReceiptFixture,
}

const version = {
  schema: 'zhimeng.knowledge-card-version.v2' as const,
  version_id: versionId,
  card_id: 'work-native-fixture',
  version_number: 1,
  content_hash: 'f'.repeat(64),
  card: reviewedWorkCardFixture,
  source_candidate_id: candidateId,
  lineage_status: 'verified' as const,
  lineage: lineageReceipt,
  formalization_status: 'verified' as const,
  formalization: {
    formalization_receipt_id: formalizationReceiptId,
    formalization_receipt_hash: knowledgeFormalizationReceiptFixture.receipt_hash,
  },
  created_at: '2026-07-20T08:10:00Z',
}

export const knowledgeCardFixture: KnowledgeCard = {
  schema: 'zhimeng.knowledge-card.v2',
  card_id: version.card_id,
  status: 'active',
  state_revision: 1,
  active_version_id: version.version_id,
  active_version: version,
  versions: [version],
  created_at: version.created_at,
  updated_at: version.created_at,
}

export const knowledgeCardListFixture: KnowledgeCardList = {
  schema: 'zhimeng.knowledge-card-list.v2',
  cards: [knowledgeCardFixture],
  fingerprint: '1'.repeat(64),
}

export const knowledgeProjectScopeFixture: KnowledgeScopeRecord = {
  schema: 'zhimeng.knowledge-scope-record.v1',
  scope_id: `knowledge-scope-project-${'9'.repeat(32)}`,
  scope_type: 'project',
  project_binding_id: `project-${'8'.repeat(32)}`,
  state_revision: 1,
  created_at: '2026-07-21T05:00:00Z',
  updated_at: '2026-07-21T05:00:00Z',
}

export const knowledgeScopedCardListFixture: KnowledgeScopedCardList = {
  schema: 'zhimeng.knowledge-scoped-card-list.v1',
  scope: knowledgeProjectScopeFixture,
  includes_application_scope: true,
  cards: [knowledgeCardFixture],
  inherited_card_ids: [knowledgeCardFixture.card_id],
  fingerprint: '9'.repeat(64),
}
