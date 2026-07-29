import type { AdjacentChainRecord, AdjacentChainResponse } from '../api/adjacentChains'

export function adjacentChainRecordFixture(
  overrides: Partial<AdjacentChainRecord> = {},
): AdjacentChainRecord {
  const base: AdjacentChainRecord = {
    schema: 'zhimeng.adjacent-skill-chain-run.v1',
    chain_id: `adjacent-chain-${'a'.repeat(32)}`,
    chain_key: 'novel-distillation-kb-to-context',
    status: 'prepared',
    project: {
      project_id: 'project-adjacent-fixture',
      revision: 3,
      fingerprint: 'f'.repeat(64),
    },
    route: {
      profile: 'chapter',
      stage: 'draft',
      chapter_id: 'CH001',
      task_intent: '连续执行 O2-D 到 O2-E，只生成写前 Context Pack。',
    },
    steps: [
      {
        step_id: 'o2d-kb-handoff',
        ordinal: 1,
        title: 'O2-D distillation -> KB handoff',
        skill_id: 'novel-distillation.kb-handoff',
        capability: 'import-distillation-index',
        status: 'prepared',
        child_id: `skill-handoff-${'b'.repeat(32)}`,
        result_hash: null,
        error_code: null,
        error_message: null,
      },
      {
        step_id: 'o2e-context-handoff',
        ordinal: 2,
        title: 'O2-E KB index -> Context Pack',
        skill_id: 'novel-kb.context-handoff',
        capability: 'assemble-context',
        status: 'pending',
        child_id: null,
        result_hash: null,
        error_code: null,
        error_message: null,
      },
    ],
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
    risk_dossier: {
      formal_draft_allowed: false,
      network: false,
      secrets: [],
      permissions: {
        read: ['bound_project_snapshot', 'run_staging'],
        write: ['run_staging', 'runtime_context_store'],
        network: false,
        secrets: [],
      },
      skills: [
        {
          step_id: 'o2d-kb-handoff',
          skill_id: 'novel-distillation.kb-handoff',
          capability: 'import-distillation-index',
          skill_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          skill_version: '2026.07.24-o2d.1',
          manifest_fingerprint: '1'.repeat(64),
          package_fingerprint: '2'.repeat(64),
          effective_scope: 'project',
          input_contract: 'https://zhimeng.local/contracts/novel-kb-handoff-request.schema.json',
          output_contract: 'https://zhimeng.local/contracts/novel-kb-handoff-result.schema.json',
        },
        {
          step_id: 'o2e-context-handoff',
          skill_id: 'novel-kb.context-handoff',
          capability: 'assemble-context',
          skill_version_id: 'skill-version-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          skill_version: '2026.07.23-o2e.1',
          manifest_fingerprint: '3'.repeat(64),
          package_fingerprint: '4'.repeat(64),
          effective_scope: 'project',
          input_contract: 'https://zhimeng.local/contracts/novel-kb-context-handoff-request.schema.json',
          output_contract: 'https://zhimeng.local/contracts/novel-kb-context-handoff-result.schema.json',
        },
      ],
      risk_summary: '一次作者风险批准覆盖固定两步相邻链；formal_draft_allowed 保持 false。',
      not_claiming: [
        'four_skills_generic_exactly_once',
        'creation_suite_drafting',
        'reader_gate',
        'local_v1_exe',
        'formal_prose_writeback',
      ],
    },
    risk_approval_token: null,
    workflow_plan: null,
    workflow_checkpoints: [],
    formal_draft_allowed: false,
    source_import_id: `source-import-${'d'.repeat(32)}`,
    skill_handoff_id: `skill-handoff-${'b'.repeat(32)}`,
    context_handoff_id: null,
    context_pack_id: null,
    context_pack_hash: null,
    route_receipt_id: null,
    route_receipt_hash: null,
    current_step_ordinal: 1,
    error_code: null,
    error_message: null,
    created_at: '2026-07-25T12:00:00+00:00',
    updated_at: '2026-07-25T12:00:00+00:00',
  }
  return { ...base, ...overrides, steps: overrides.steps ?? base.steps }
}

export function adjacentChainResponseFixture(
  overrides: Partial<AdjacentChainRecord> = {},
): AdjacentChainResponse {
  return {
    schema: 'zhimeng.adjacent-skill-chain-response.v1',
    record: adjacentChainRecordFixture(overrides),
    not_claiming: [
      'four_skills_generic_exactly_once',
      'creation_suite_drafting',
      'reader_gate',
      'local_v1_exe',
    ],
  }
}
