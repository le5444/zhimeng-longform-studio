import {
  summarizeChangeSet,
  type ChangeSet,
  type ChangeSetSummary,
  type HunkDecision,
  type PostDraftGateReceipt,
  type WritebackManifest,
} from '../api/changeSets'

const projectId = 'project-0123456789abcdef01234567'
const changeSetId = 'changeset-0123456789abcdef0123456789abcdef'

function writeback(outcome: 'applied' | 'rolled_back'): WritebackManifest {
  return {
    schema: 'zhimeng.writeback-manifest.v1',
    change_set_id: changeSetId,
    project_id: projectId,
    base_revision: 3,
    base_fingerprint: 'a'.repeat(64),
    result_revision: outcome === 'applied' ? 4 : 5,
    result_fingerprint: outcome === 'applied' ? 'd'.repeat(64) : 'a'.repeat(64),
    outcome,
    files: [{
      change_id: 'change-0123456789abcdef0123456789abcdef',
      operation: 'update',
      relative_path: '05_正文内容/第一卷/CH001_正文.md',
      before_sha256: 'b'.repeat(64),
      after_sha256: outcome === 'applied' ? 'c'.repeat(64) : 'b'.repeat(64),
      result_file_id: 'file-0123456789abcdef0123456789abcdef',
      accepted_hunk_ids: ['hunk-0123456789abcdef0123456789abcdef'],
      rejected_hunk_ids: [],
      backup_ref: 'backups/chapter.md',
    }],
    created_directories: [],
    created_at: '2026-07-19T04:10:00Z',
  }
}

export function postDraftGateReceiptFixture({
  status = 'passed',
  reviewRevision = 1,
  changeSetId: receiptChangeSetId = changeSetId,
  projectId: receiptProjectId = projectId,
}: {
  status?: PostDraftGateReceipt['status']
  reviewRevision?: number
  changeSetId?: string
  projectId?: string
} = {}): PostDraftGateReceipt {
  const passed = status === 'passed'
  return {
    schema: 'zhimeng.post-draft-gate-receipt.v1',
    gate_receipt_id: 'post-draft-gate-0123456789abcdef0123456789abcdef',
    change_set_id: receiptChangeSetId,
    project_id: receiptProjectId,
    review_revision: reviewRevision,
    base_revision: 3,
    base_fingerprint: 'a'.repeat(64),
    candidate_hash: 'c'.repeat(64),
    validator_id: 'novel-creation-suite.post-draft-gate',
    validator_version: '0.1.0',
    validator_manifest_fingerprint: 'd'.repeat(64),
    validator_package_fingerprint: 'e'.repeat(64),
    context_pack_id: null,
    context_pack_hash: null,
    knowledge_fingerprint: null,
    candidate_file_count: 1,
    skill_receipt_id: 'skill-receipt-local-1',
    skill_receipt_hash: 'f'.repeat(64),
    skill_output_hash: '1'.repeat(64),
    execution_status: status === 'failed' ? 'failed' : 'succeeded',
    status,
    result: status === 'failed' ? null : {
      schema: 'zhimeng.creation-draft-gate-result.v1',
      status: passed ? 'passed' : 'blocked',
      summary: passed ? '候选通过交付检查' : '候选包含阻断项',
      candidate_id: 'candidate-local-1',
      candidate_manifest_hash: '2'.repeat(64),
      quality_assessed: false,
      formal_draft_allowed: false,
      global_findings: passed ? [] : [{
        code: 'draft_role_marker_leak',
        failure_signature: 'draft_role_marker_leak',
        severity: 'blocked',
        message: '候选正文包含草稿角色标记',
        evidence: '第 1 行',
        location: {
          start: { line: 1, column: 1, offset: 0 },
          end: { line: 1, column: 5, offset: 4 },
        },
      }],
      checks: [{
        code: 'candidate_manifest',
        status: passed ? 'passed' : 'blocked',
        message: passed ? '候选文件清单完整' : '候选清单存在阻断项',
        evidence_refs: ['candidate-local-1'],
      }],
      files: [{
        path: '05_正文内容/第一卷/CH001_正文.md',
        status: passed ? 'passed' : 'blocked',
        content_hash: '3'.repeat(64),
        byte_length: 24,
        body_line_count: 2,
        findings: passed ? [] : [{
          code: 'draft_role_marker_leak',
          failure_signature: 'draft_role_marker_leak',
          severity: 'blocked',
          message: '正文标记需要移除',
          evidence: '系统：',
          location: {
            start: { line: 1, column: 1, offset: 0 },
            end: { line: 1, column: 4, offset: 3 },
          },
        }],
      }],
      failure_signatures: passed ? [] : ['draft_role_marker_leak'],
    },
    error_code: status === 'failed' ? 'post_draft_gate_unavailable' : null,
    error_message: status === 'failed' ? 'Gate 执行器不可用' : null,
    quality_assessed: false,
    formal_draft_allowed: false,
    created_at: '2026-07-19T04:10:00Z',
    receipt_hash: '4'.repeat(64),
  }
}

export function changeSetFixture({
  status = 'reviewing',
  decision = 'pending',
  reviewRevision = 0,
  gateRequired = false,
  postDraftGate = null,
  postDraftGateScope = 'prose_body',
  origin = gateRequired ? 'agent' : 'manual',
}: {
  status?: ChangeSet['status']
  decision?: HunkDecision
  reviewRevision?: number
  gateRequired?: boolean
  postDraftGate?: PostDraftGateReceipt | null
  postDraftGateScope?: ChangeSet['changes'][number]['post_draft_gate_scope']
  origin?: ChangeSet['origin']
} = {}): ChangeSet {
  const appliedManifest = ['applied', 'rolled_back'].includes(status)
    ? writeback('applied')
    : null
  const hasAgentProvenance = origin === 'agent'
  const hasSkillProvenance = origin !== 'manual'
  return {
    schema: 'zhimeng.change-set.v1',
    change_set_id: changeSetId,
    project_id: projectId,
    base_revision: 3,
    base_fingerprint: 'a'.repeat(64),
    title: '收紧第一章开场',
    summary: '保留黄铜钥匙线索，删去重复的环境说明。',
    origin,
    source_run_id: hasAgentProvenance ? 'run-local-1' : null,
    source_tool_call_id: hasAgentProvenance ? 'tool-call-local-1' : null,
    source_request_hash: hasAgentProvenance ? '1'.repeat(64) : null,
    source_skill_ids: hasSkillProvenance ? ['novel-style-deai'] : [],
    gate_required: gateRequired,
    status,
    review_revision: reviewRevision,
    changes: [{
      change_id: 'change-0123456789abcdef0123456789abcdef',
      operation: 'update',
      file_id: 'file-0123456789abcdef0123456789abcdef',
      relative_path: '05_正文内容/第一卷/CH001_正文.md',
      rationale: '开场需要更快进入黄铜钥匙造成的压力。',
      before_sha256: 'b'.repeat(64),
      proposed_after_sha256: 'c'.repeat(64),
      preserve_utf8_bom: false,
      post_draft_gate_scope: postDraftGateScope,
      hunks: [{
        hunk_id: 'hunk-0123456789abcdef0123456789abcdef',
        index: 0,
        before_start: 1,
        before_end: 2,
        after_start: 1,
        after_end: 2,
        before_text: '雨下了一整夜。\n林见川在门口停了很久。',
        after_text: '黄铜钥匙硌着掌心。\n林见川推开了门。',
        before_sha256: 'e'.repeat(64),
        after_sha256: 'f'.repeat(64),
        decision,
      }],
    }],
    post_draft_gate: postDraftGate,
    writeback: appliedManifest,
    rollback: status === 'rolled_back' ? writeback('rolled_back') : null,
    error: status === 'failed' ? '写回失败' : null,
    created_at: '2026-07-19T04:00:00Z',
    updated_at: '2026-07-19T04:05:00Z',
  }
}

export function changeSetSummaryFixture(
  options: Parameters<typeof changeSetFixture>[0] = {},
): ChangeSetSummary {
  return summarizeChangeSet(changeSetFixture(options))
}

export const changeSetProjectId = projectId
