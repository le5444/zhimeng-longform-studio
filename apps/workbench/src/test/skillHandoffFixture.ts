import type { SkillHandoffRecord, SkillHandoffResponse } from '../api/skillHandoffs'

export function skillHandoffRecordFixture(
  overrides: Partial<SkillHandoffRecord> = {},
): SkillHandoffRecord {
  const base: SkillHandoffRecord = {
    schema: 'zhimeng.skill-handoff-run.v1',
    handoff_id: `skill-handoff-${'a'.repeat(32)}`,
    handoff_key: 'novel-distillation-to-kb-manager',
    status: 'prepared',
    project: {
      project_id: 'project-o2d-fixture',
      revision: 3,
      fingerprint: 'f'.repeat(64),
    },
    workflow: {
      workflow_plan_id: `workflow-plan-${'b'.repeat(32)}`,
      workflow_plan_hash: '1'.repeat(64),
      checkpoint_id: `workflow-checkpoint-${'c'.repeat(32)}`,
      checkpoint_hash: '2'.repeat(64),
      tool_call_id: 'skill_handoff_fixture_tool',
      skill_call_id: 'skill_call_fixture',
    },
    upstream: {
      source_skill_id: 'novel-distillation',
      source_import_id: `source-import-${'d'.repeat(32)}`,
      formalization_receipt_id: `knowledge-formalization-${'e'.repeat(32)}`,
      formalization_receipt_hash: '3'.repeat(64),
      knowledge_fingerprint: '4'.repeat(64),
      cards_hash: '5'.repeat(64),
      cards: [
        {
          card_id: 'work-o2d-fixture',
          card_type: 'work',
          version_id: `knowledge-version-${'1'.repeat(32)}`,
          content_hash: '6'.repeat(64),
          title: '长篇升级循环与读者承诺',
          summary: '把已正式化的作品机制变成下游 KB 可检索入口。',
          route_terms: ['玄幻', '升级', '阶段兑现'],
          tags: ['work', 'route'],
          safety_summary: '只交接抽象机制入口。',
        },
        {
          card_id: 'voice-o2d-fixture',
          card_type: 'voice',
          version_id: `knowledge-version-${'2'.repeat(32)}`,
          content_hash: '7'.repeat(64),
          title: '贴近人物压力的叙事声音',
          summary: '把声音卡作为受控入口，不复制来源表达。',
          route_terms: ['人物压力', '自然叙事'],
          tags: ['voice'],
          safety_summary: '不迁移来源声纹。',
        },
        {
          card_id: 'validator-o2d-fixture',
          card_type: 'validator',
          version_id: `knowledge-version-${'3'.repeat(32)}`,
          content_hash: '8'.repeat(64),
          title: '正文拒收闸门',
          summary: '进入写作前保留来源污染和人物压力检查入口。',
          route_terms: ['来源污染', '人物压力'],
          tags: ['validator'],
          safety_summary: '发现污染必须拒收。',
        },
      ],
    },
    downstream: {
      target_skill_id: 'novel-kb-manager',
      capability: 'import-distillation-index',
      contract: 'distillation_index active-card entries only; no full card text copied',
    },
    activation: {
      managed_skill_id: 'novel-distillation.kb-handoff',
      skill_version_id: `skill-version-${'9'.repeat(32)}`,
      skill_version: '2026.07.24-o2d.1',
      manifest_fingerprint: 'a'.repeat(64),
      package_fingerprint: 'b'.repeat(64),
      effective_scope: 'application',
    },
    approval: {
      approved: false,
      approved_by: null,
      approved_at: null,
      risk_acknowledged: false,
    },
    permissions: {
      read: ['bound_project_snapshot'],
      write: ['run_staging'],
      network: false,
      secrets: [],
    },
    formal_draft_allowed: false,
    skill_receipt_id: null,
    skill_receipt_hash: null,
    result_hash: null,
    artifact_hash: null,
    output_artifact_path: null,
    result: null,
    error_code: null,
    error_message: null,
    created_at: '2026-07-24T01:00:00.000Z',
    updated_at: '2026-07-24T01:00:00.000Z',
  }
  return { ...base, ...overrides }
}

export function skillHandoffResponseFixture(
  overrides: Partial<SkillHandoffRecord> = {},
): SkillHandoffResponse {
  return {
    schema: 'zhimeng.skill-handoff-response.v1',
    record: skillHandoffRecordFixture(overrides),
  }
}
