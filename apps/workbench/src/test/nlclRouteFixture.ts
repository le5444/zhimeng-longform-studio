import type {
  NlclRouteExecutionState,
  NlclRouteExecutionReceipt,
  NlclRouteReceipt,
  PrewriteOperationPackage,
} from '../api/contextPacks'
import {
  projectKbManifestHashFixture,
  projectKbStateIdFixture,
} from './contextPackFixture'

export function nlclRouteFixture(
  overrides: Partial<NlclRouteReceipt> = {},
): NlclRouteReceipt {
  const contextPackId = overrides.context_pack_id ?? 'context-pack-0123456789abcdef'
  return {
    schema: 'zhimeng.nlcl-route-receipt.v1',
    route_receipt_id: `nlcl-route-${'a'.repeat(32)}`,
    context_pack_id: contextPackId,
    context_pack_hash: '1'.repeat(64),
    project_id: 'project-0123456789abcdef01234567',
    project_revision: 1,
    project_fingerprint: 'd'.repeat(64),
    active_chapter_id: 'chapter-001',
    profile: 'chapter',
    objective_hash: '4'.repeat(64),
    knowledge_fingerprint: 'e'.repeat(64),
    project_kb_state_id: projectKbStateIdFixture,
    project_kb_manifest_hash: projectKbManifestHashFixture,
    source: 'native_knowledge',
    status: 'ready',
    selected_cards: [{
      card_id: 'work-pressure-loop',
      card_type: 'work',
      title: '阶段压力兑现',
      version_id: `knowledge-version-${'b'.repeat(32)}`,
      content_hash: '5'.repeat(64),
      route_terms: ['玄幻', '升级'],
      recommended_profiles: ['chapter'],
      negative_routes: ['纯日常'],
      safety_summary: '仅使用抽象机制，不复用来源表达。',
      feedback_layer: 'L3_DISTILLATION_RETRIEVAL',
    }],
    chapter_call_list: [
      {
        ordinal: 1,
        skill_id: 'novel-creation-suite.project-audit',
        purpose: '正式创作前核对项目真值与 Creation readiness。',
        execution_status: 'requires_runtime_catalog_check',
        input_contract: 'zhimeng.creation-project-audit-request.v1',
        context_pack_id: contextPackId,
        selected_card_ids: ['work-pressure-loop'],
      },
      {
        ordinal: 2,
        skill_id: 'novel-creation-suite.prewrite-gate',
        purpose: '消费运行包与项目审计收据，输出写前门禁结论。',
        execution_status: 'requires_runtime_catalog_check',
        input_contract: 'zhimeng.creation-prewrite-gate-request.v1',
        context_pack_id: contextPackId,
        selected_card_ids: ['work-pressure-loop'],
      },
      {
        ordinal: 3,
        skill_id: 'novel-creation-suite.creation-handoff',
        purpose: '在 formal_draft_allowed=false 下生成 Creation 可消费交接，不写正式正文。',
        execution_status: 'requires_runtime_catalog_check',
        input_contract: 'zhimeng.creation-handoff-request.v1',
        context_pack_id: contextPackId,
        selected_card_ids: ['work-pressure-loop'],
      },
    ],
    whole_library_load_forbidden: true,
    project_truth_precedence: true,
    formal_draft_allowed: false,
    allowed_output: 'context_and_creation_audit_only',
    warnings: [
      'creation_runtime_catalog_check_required',
      'formal_draft_not_authorized',
    ],
    strict_failures: [],
    created_at: '2026-07-20T00:00:00Z',
    receipt_hash: '6'.repeat(64),
    ...overrides,
  }
}

export function nlclRouteExecutionReceiptFixture(
  route: NlclRouteReceipt,
  overrides: Partial<NlclRouteExecutionReceipt> = {},
): NlclRouteExecutionReceipt {
  const callOrdinal = overrides.call_ordinal ?? 1
  const call = route.chapter_call_list.find((item) => item.ordinal === callOrdinal)
    ?? route.chapter_call_list[0]
  return {
    schema: 'zhimeng.nlcl-route-execution-receipt.v1',
    execution_receipt_id: `nlcl-route-exec-${'7'.repeat(32)}`,
    route_receipt_id: route.route_receipt_id,
    route_receipt_hash: route.receipt_hash,
    context_pack_id: route.context_pack_id,
    context_pack_hash: route.context_pack_hash,
    project_kb_state_id: route.project_kb_state_id,
    project_kb_manifest_hash: route.project_kb_manifest_hash,
    call_ordinal: callOrdinal,
    skill_id: call.skill_id,
    input_contract: call.input_contract,
    run_id: 'run-0123456789abcdef0123456789',
    tool_call_id: 'tool-0123456789abcdef0123456789',
    skill_call_id: 'skill-call-0123456789abcdef01234567',
    skill_receipt_id: 'skill-receipt-0123456789abcdef01234567',
    skill_receipt_hash: '8'.repeat(64),
    execution_status: 'succeeded',
    domain_status: 'blocked',
    output_hash: '9'.repeat(64),
    tool_result_hash: 'a'.repeat(64),
    created_at: '2026-07-20T01:00:00Z',
    receipt_hash: 'b'.repeat(64),
    ...overrides,
  }
}

export function nlclRouteExecutionFixture(
  route: NlclRouteReceipt,
  overrides: Partial<NlclRouteExecutionState> = {},
): NlclRouteExecutionState {
  return {
    schema: 'zhimeng.nlcl-route-execution-state.v1',
    route_receipt_id: route.route_receipt_id,
    context_pack_id: route.context_pack_id,
    call_ordinal: 1,
    status: 'awaiting_execution',
    run_id: null,
    tool_call_id: null,
    receipt: null,
    error_code: null,
    error_message: null,
    ...overrides,
  }
}

export function prewriteOperationPackageFixture(
  route: NlclRouteReceipt,
  overrides: Partial<PrewriteOperationPackage> = {},
): PrewriteOperationPackage {
  return {
    schema: 'zhimeng.prewrite-operation-package.v1',
    operation_package_id: `prewrite-package-${'c'.repeat(32)}`,
    context_pack_id: route.context_pack_id,
    context_pack_hash: route.context_pack_hash,
    route_receipt_id: route.route_receipt_id,
    route_receipt_hash: route.receipt_hash,
    project_id: route.project_id,
    project_revision: route.project_revision,
    project_fingerprint: route.project_fingerprint,
    active_chapter_id: route.active_chapter_id,
    task_goal: '收紧章节开头。',
    must_obey_truth: [],
    current_character_state: [],
    relevant_evidence_refs: [],
    constraints_and_taboos: [
      'project_truth_precedence',
      'formal_draft_requires_separate_authorization',
    ],
    scene_contract: {
      active_chapter_id: route.active_chapter_id,
      task_goal: '收紧章节开头。',
      evidence_source_ids: [],
      status: 'unbound',
    },
    style_output_contract: {
      selected_card_ids: ['work-pressure-loop'],
      selected_card_hashes: ['5'.repeat(64)],
      whole_library_load_forbidden: true,
      status: 'bound',
    },
    acceptance_gate: {
      required_checks: ['project_audit', 'author_approval'],
      minimum_review_score: 80,
      author_approval_required: true,
      writeback_receipt_required: true,
    },
    failure_routing: [{
      failure_signature: 'scene_contract_unbound',
      rewrite_from: 'L2_CONTROL_CARD',
    }],
    library_activation_budget: {
      max_characters: 48_000,
      used_characters: 1_024,
      max_tokens: 24_000,
      used_tokens: 400,
      activated_source_ids: [],
    },
    whole_library_load_forbidden: true,
    readiness: 'blocked',
    formal_draft_allowed: false,
    missing_requirements: ['scene_contract_unbound'],
    warnings: ['formal_writer_contract_unavailable'],
    created_at: '2026-07-20T00:00:00Z',
    package_hash: 'd'.repeat(64),
    ...overrides,
  }
}
