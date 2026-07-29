import type {
  ContextIncludedFragment,
  ContextPack,
  ContextPackRecord,
  KnowledgeProvenanceRef,
} from '../api/contextPacks'

export const projectKbStateIdFixture = `project-kb-state-${'6'.repeat(32)}`
export const projectKbManifestHashFixture = '7'.repeat(64)

export function knowledgeProvenanceFixture(
  overrides: Partial<KnowledgeProvenanceRef> = {},
): KnowledgeProvenanceRef {
  return {
    schema: 'zhimeng.knowledge-provenance-ref.v1',
    source_import_id: `source-import-${'a'.repeat(32)}`,
    source_receipt_id: 'source-import-receipt-0b65c29a14b65d5c98ac301f672138c0',
    source_receipt_hash: '1'.repeat(64),
    source_packet_fingerprint: '2'.repeat(64),
    cards_draft_hash: '3'.repeat(64),
    candidate_id: `knowledge-candidate-${'c'.repeat(32)}`,
    candidate_origin_hash: '4'.repeat(64),
    card_id: 'work-pressure-loop',
    version_id: `knowledge-version-${'d'.repeat(32)}`,
    version_content_hash: '5'.repeat(64),
    version_lineage_receipt_id: `knowledge-lineage-${'e'.repeat(32)}`,
    version_lineage_receipt_hash: '6'.repeat(64),
    formalization_receipt_id: 'knowledge-formalization-7889af1170bbe5bf1a0120df7b724cd4',
    formalization_receipt_hash: '7'.repeat(64),
    ...overrides,
  }
}

export function nativeKnowledgeFragmentFixture(
  overrides: Partial<ContextIncludedFragment> = {},
): ContextIncludedFragment {
  const provenance = Object.prototype.hasOwnProperty.call(overrides, 'knowledge_provenance')
    ? overrides.knowledge_provenance
    : knowledgeProvenanceFixture()
  return {
    source_id: `knowledge-card:${provenance?.card_id ?? 'work-pressure-loop'}`,
    source_type: 'native_knowledge_card:work',
    title: '阶段压力兑现',
    authority: 'formal_knowledge',
    version: provenance?.version_id ?? `knowledge-version-${'d'.repeat(32)}`,
    source_hash: provenance?.version_content_hash ?? '5'.repeat(64),
    content: '压力必须通过角色行动兑现。',
    priority: 80,
    mandatory: false,
    knowledge_provenance: provenance,
    fragment_hash: '8'.repeat(64),
    estimated_characters: 180,
    estimated_tokens: 72,
    selection_reason: 'priority',
    ...overrides,
  }
}

export function contextPackFixture(
  overrides: Partial<ContextPack> = {},
  consumedByRunId: string | null = null,
): ContextPackRecord {
  const projectId = overrides.project_id ?? 'project-0123456789abcdef01234567'
  const sessionId = overrides.session_id ?? 'session-0123456789abcdef01234567'
  const objective = overrides.objective ?? '收紧开头'
  const pack: ContextPack = {
    schema: 'zhimeng.context-pack.v1',
    context_pack_id: 'context-pack-0123456789abcdef',
    session_id: sessionId,
    project_id: projectId,
    history_through_ordinal: 0,
    history_message_count: 0,
    objective,
    profile: 'chapter',
    active_chapter_id: 'chapter-001',
    project_revision: 1,
    project_fingerprint: 'd'.repeat(64),
    knowledge_fingerprint: 'e'.repeat(64),
    project_kb_state_id: projectKbStateIdFixture,
    project_kb_manifest_hash: projectKbManifestHashFixture,
    compiler_version: 'context-compiler.v1',
    policy_version: 'context-selection.v1',
    budget: {
      character_estimator: 'unicode_codepoints_v1',
      token_estimator: 'conservative_utf8_bytes_div3_v1',
      max_characters: 48_000,
      used_characters: 1_200,
      remaining_characters: 46_800,
      overflow_characters: 0,
      max_tokens: 24_000,
      used_tokens: 500,
      remaining_tokens: 23_500,
      overflow_tokens: 0,
    },
    included: [{
      source_id: 'project-truth-one',
      source_type: 'project_truth',
      title: '项目控制卡',
      authority: 'project_truth',
      version: 'revision-1',
      source_hash: 'a'.repeat(64),
      content: '必须保留黄铜钥匙。',
      priority: 100,
      mandatory: true,
      fragment_hash: 'b'.repeat(64),
      estimated_characters: 120,
      estimated_tokens: 50,
      selection_reason: 'mandatory',
    }],
    excluded: [],
    gates: [],
    exact_provider_messages: [
      { role: 'system', content: '项目控制卡：必须保留黄铜钥匙。' },
      { role: 'user', content: objective },
    ],
    pack_hash: '1'.repeat(64),
    injection_hash: '2'.repeat(64),
    provider_messages_hash: '3'.repeat(64),
    readiness: 'ready',
    created_at: '2026-07-20T00:00:00Z',
    ...overrides,
  }
  return {
    schema: 'zhimeng.context-pack-record.v1',
    pack,
    consumed_by_run_id: consumedByRunId,
  }
}
