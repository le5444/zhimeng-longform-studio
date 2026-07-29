import type { ContextHandoffRecord, ContextHandoffResponse } from '../api/contextHandoffs'

export function contextHandoffRecordFixture(
  overrides: Partial<ContextHandoffRecord> = {},
): ContextHandoffRecord {
  const entries: NonNullable<ContextHandoffRecord['upstream']>['entries'] = [
    {
      card_id: 'work-o2e-fixture',
      card_type: 'work',
      version_id: `knowledge-version-${'1'.repeat(32)}`,
      content_hash: '6'.repeat(64),
      title: '长篇升级循环与读者承诺',
      route_terms: ['玄幻', '升级', '阶段兑现'],
      tags: ['work', 'route'],
      safety_summary: '只交接抽象机制入口。',
    },
    {
      card_id: 'voice-o2e-fixture',
      card_type: 'voice',
      version_id: `knowledge-version-${'2'.repeat(32)}`,
      content_hash: '7'.repeat(64),
      title: '贴近人物压力的叙事声音',
      route_terms: ['人物压力', '自然叙事'],
      tags: ['voice'],
      safety_summary: '不迁移来源声纹。',
    },
    {
      card_id: 'validator-o2e-fixture',
      card_type: 'validator',
      version_id: `knowledge-version-${'3'.repeat(32)}`,
      content_hash: '8'.repeat(64),
      title: '正文拒收闸门',
      route_terms: ['来源污染', '人物压力'],
      tags: ['validator'],
      safety_summary: '发现污染必须拒收。',
    },
  ]
  const base: ContextHandoffRecord = {
    schema: 'zhimeng.context-handoff-run.v1',
    context_handoff_id: `context-handoff-${'a'.repeat(32)}`,
    handoff_key: 'novel-kb-import-index-to-assemble-context',
    status: 'prepared',
    project: {
      project_id: 'project-o2e-fixture',
      revision: 3,
      fingerprint: 'f'.repeat(64),
    },
    workflow: {
      workflow_plan_id: `workflow-plan-${'b'.repeat(32)}`,
      workflow_plan_hash: '1'.repeat(64),
      checkpoint_id: `workflow-checkpoint-${'c'.repeat(32)}`,
      checkpoint_hash: '2'.repeat(64),
      tool_call_id: 'context_handoff_fixture_tool',
      skill_call_id: 'skill_call_context_fixture',
    },
    upstream: {
      source_handoff_id: `skill-handoff-${'d'.repeat(32)}`,
      source_handoff_key: 'novel-distillation-to-kb-manager',
      source_skill_receipt_id: `skill-receipt-${'e'.repeat(32)}`,
      source_skill_receipt_hash: '3'.repeat(64),
      source_result_hash: '4'.repeat(64),
      distillation_index_hash: '5'.repeat(64),
      distillation_index_artifact_path: 'C:/runtime/context-handoffs/distillation_index_handoff.json',
      distillation_index_artifact_file_hash: '5'.repeat(64),
      source_project_revision: 3,
      source_project_fingerprint: 'f'.repeat(64),
      upstream_activation_skill_version_id: `skill-version-${'9'.repeat(32)}`,
      entries,
      no_full_card_text_copied: true,
    },
    downstream: {
      target_skill_id: 'novel-kb-manager',
      capability: 'assemble-context',
      contract: 'distillation_index entries become controlled Context Pack input; route_health ready is not validated',
    },
    route: {
      profile: 'chapter',
      stage: 'draft',
      chapter_id: 'CH001',
      task_intent: '把 O2-D 受控 distillation_index 入口装配成写前 Context Pack；不写正文。',
      route_terms: ['chapter', 'draft', 'CH001', '玄幻', '升级', '人物压力', '来源污染'],
    },
    activation: {
      managed_skill_id: 'novel-kb.context-handoff',
      skill_version_id: `skill-version-${'a'.repeat(32)}`,
      skill_version: '2026.07.23-o2e.1',
      manifest_fingerprint: 'a'.repeat(64),
      package_fingerprint: 'b'.repeat(64),
      effective_scope: 'project',
    },
    approval: {
      approved: false,
      approved_by: null,
      approved_at: null,
      risk_acknowledged: false,
    },
    permissions: {
      read: ['bound_project_snapshot', 'run_staging'],
      write: ['run_staging', 'runtime_context_store'],
      network: false,
      secrets: [],
    },
    constraints: {
      no_full_card_text_copied: true,
      formal_draft_allowed: false,
      project_truth_not_written: true,
      route_health_ready_is_not_validated: true,
      writes_formal_prose: false,
    },
    formal_draft_allowed: false,
    skill_receipt_id: null,
    skill_receipt_hash: null,
    result_hash: null,
    artifact_hash: null,
    output_artifact_path: null,
    context_pack_id: null,
    context_pack_hash: null,
    route_receipt_id: null,
    route_receipt_hash: null,
    result: null,
    error_code: null,
    error_message: null,
    created_at: '2026-07-23T01:00:00.000Z',
    updated_at: '2026-07-23T01:00:00.000Z',
  }
  return { ...base, ...overrides }
}

export function contextHandoffResponseFixture(
  overrides: Partial<ContextHandoffRecord> = {},
): ContextHandoffResponse {
  return {
    schema: 'zhimeng.context-handoff-response.v1',
    record: contextHandoffRecordFixture(overrides),
  }
}
