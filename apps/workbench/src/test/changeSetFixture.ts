import {
  summarizeChangeSet,
  type ChangeSet,
  type ChangeSetSummary,
  type HunkDecision,
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

export function changeSetFixture({
  status = 'reviewing',
  decision = 'pending',
  reviewRevision = 0,
  gateRequired = false,
  postDraftGateScope = 'prose_body',
  origin = gateRequired ? 'agent' : 'manual',
  aiSmell = null,
}: {
  status?: ChangeSet['status']
  decision?: HunkDecision
  reviewRevision?: number
  gateRequired?: boolean
  postDraftGateScope?: ChangeSet['changes'][number]['post_draft_gate_scope']
  origin?: ChangeSet['origin']
  aiSmell?: ChangeSet['ai_smell']
} = {}): ChangeSet {
  const appliedManifest = ['applied', 'rolled_back'].includes(status)
    ? writeback('applied')
    : null
  const hasAgentProvenance = origin === 'agent'
  const hasSkillProvenance = origin === 'agent' || origin === 'skill_workflow'
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
    ai_smell: aiSmell,
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
