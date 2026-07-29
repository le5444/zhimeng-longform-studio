import { describe, expect, it, vi } from 'vitest'
import {
  CHANGE_SET_WRITEBACK_TIMEOUT_MS,
  POST_DRAFT_GATE_TIMEOUT_MS,
  applyChangeSet,
  createProjectChangeSet,
  getPostDraftGate,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  runPostDraftGate,
  updateHunkDecisions,
  exportEvalCorpus,
  actFormalKnowledgePromotion,
  getFormalKnowledgePromotion,} from './changeSets'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('change set API client', () => {
  it('lists project change sets and loads selected detail through stable IDs', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.change-set-list.v1',
        change_sets: [{ change_set_id: 'changeset-first' }],
      }))
      .mockResolvedValueOnce(jsonResponse({ change_set_id: 'changeset-first' }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    const records = await listProjectChangeSets('project-one', options)
    await getChangeSet('changeset-first', options)

    expect(records[0].change_set_id).toBe('changeset-first')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      'http://127.0.0.1:8787/api/v1/projects/project-one/change-sets',
      'http://127.0.0.1:8787/api/v1/change-sets/changeset-first',
    ])
  })

  it('sends decisions and writeback commands with the current review revision', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    await updateHunkDecisions(
      'changeset-one',
      4,
      [{ hunk_id: 'hunk-one', decision: 'accepted' }],
      options,
    )
    await applyChangeSet('changeset-one', 5, options)
    await rollbackChangeSet('changeset-one', 6, options)
    await runPostDraftGate('changeset-one', 7, options)
    await getPostDraftGate('changeset-one', options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/decisions',
        method: 'PATCH',
        body: {
          expected_review_revision: 4,
          decisions: [{ hunk_id: 'hunk-one', decision: 'accepted' }],
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/apply',
        method: 'POST',
        body: { expected_review_revision: 5 },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/rollback',
        method: 'POST',
        body: { expected_review_revision: 6 },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/post-draft-gate',
        method: 'POST',
        body: { expected_review_revision: 7 },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/post-draft-gate',
        method: undefined,
        body: undefined,
      },
    ])
    expect(CHANGE_SET_WRITEBACK_TIMEOUT_MS).toBeGreaterThan(3_500)
    expect(POST_DRAFT_GATE_TIMEOUT_MS).toBeGreaterThan(60_000)
  })

  it('creates a manual author change set against the project writeback endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      change_set_id: 'changeset-created',
      status: 'reviewing',
    }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    await createProjectChangeSet('project-one', {
      base_revision: 3,
      base_fingerprint: 'a'.repeat(64),
      title: '作者修改 CH001',
      summary: '作者手改正文',
      origin: 'manual',
      changes: [{
        operation: 'update',
        file_id: 'file-0123456789abcdef0123456789abcdef',
        after_content: '新的正文\n',
        rationale: '作者手改正文。',
      }],
    }, options)

    expect(fetcher).toHaveBeenCalledTimes(1)
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8787/api/v1/projects/project-one/change-sets')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      schema: 'zhimeng.change-set-create.v1',
      origin: 'manual',
      gate_required: false,
      title: '作者修改 CH001',
      changes: [{ operation: 'update' }],
    })
  })

  it('exports desensitized eval corpus and stages formal knowledge', async () => {
    const changeSetId = 'changeset-' + 'a'.repeat(32)
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.eval-corpus-export-response.v1',
        change_set_id: changeSetId,
        receipt: {
          schema: 'zhimeng.eval-corpus-export-receipt.v1',
          receipt_id: 'eval-corpus-export-' + 'b'.repeat(32),
          change_set_id: changeSetId,
          project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
          outcome: 'applied',
          export_relative_path: '07_运营数据/eval_corpus/x.json',
          export_file_sha256: 'c'.repeat(64),
          artifact_count: 1,
          artifacts: [{
            kind: 'hard_fact_gate',
            present: true,
            relative_artifact: 'hard-fact-gate-receipt.json',
            content_sha256: 'd'.repeat(64),
            desensitized: true,
          }],
          redaction_policy: 'local-v1-eval-corpus-redact-1',
          formal_draft_allowed: false,
          quality_assessed: false,
          formal_knowledge_contaminated: false,
          training_ready: false,
          created_at: '2026-07-26T00:00:00+00:00',
          receipt_hash: 'e'.repeat(64),
        },
        history: [],
        package: { schema: 'zhimeng.eval-corpus-package.v1' },
        not_claiming: ['training_dataset_ready'],
      }))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.formal-knowledge-promotion-response.v1',
        change_set_id: changeSetId,
        receipt: {
          schema: 'zhimeng.formal-knowledge-promotion-receipt.v1',
          receipt_id: 'formal-knowledge-promotion-' + 'f'.repeat(32),
          change_set_id: changeSetId,
          project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
          feedback_candidate_bundle_id: null,
          feedback_candidate_bundle_hash: null,
          status: 'staged',
          authorization_input_ready: true,
          formal_knowledge_contaminated: false,
          formal_draft_allowed: false,
          writes_formal_knowledge: false,
          quality_assessed: false,
          checks: [],
          missing_required: [],
          items: [],
          packages: [],
          queue_relative_path: '07_运营数据/nlcl_feedback/formal_knowledge_promotion_queue.jsonl',
          queue_rows_appended: 1,
          policy_version: 'local-v1-formal-knowledge-promotion-1',
          evaluated_at: '2026-07-26T00:00:00+00:00',
          receipt_hash: '1'.repeat(64),
        },
        history: [],
        not_claiming: ['formal_knowledge_auto_activation'],
      }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const exported = await exportEvalCorpus(changeSetId, options)
    expect(exported.receipt?.training_ready).toBe(false)
    expect(String(fetcher.mock.calls[0]?.[0])).toContain('/eval-corpus-export')
    const staged = await actFormalKnowledgePromotion(
      changeSetId,
      '2'.repeat(64),
      [{ candidate_id: 'feedback-candidate-' + '3'.repeat(32), action: 'stage_for_review' }],
      options,
    )
    expect(staged.receipt?.writes_formal_knowledge).toBe(false)
    expect(String(fetcher.mock.calls[1]?.[0])).toContain('/formal-knowledge-promotion')
  })

  it('loads formal knowledge promotion and supports reject/rollback actions', async () => {
    const changeSetId = 'changeset-' + 'a'.repeat(32)
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.formal-knowledge-promotion-response.v1',
        change_set_id: changeSetId,
        receipt: {
          schema: 'zhimeng.formal-knowledge-promotion-receipt.v1',
          receipt_id: 'formal-knowledge-promotion-' + 'f'.repeat(32),
          change_set_id: changeSetId,
          project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
          feedback_candidate_bundle_id: null,
          feedback_candidate_bundle_hash: null,
          status: 'staged',
          authorization_input_ready: true,
          formal_knowledge_contaminated: false,
          formal_draft_allowed: false,
          writes_formal_knowledge: false,
          quality_assessed: false,
          checks: [],
          missing_required: [],
          items: [{
            candidate_id: 'feedback-candidate-' + '3'.repeat(32),
            finding_id: 'fact-1',
            finding_kind: 'fact_risk',
            summary: 'x',
            evidence_quote: 'y',
            relative_path: null,
            experience_relative_path: null,
            promotion_status: 'formal_promotion_staged',
            regression_result: 'pass_with_watch',
            staged_package_id: 'formal-knowledge-package-' + '4'.repeat(32),
            formal_knowledge_contaminated: false,
            formal_draft_allowed: false,
            writes_formal_knowledge: false,
          }],
          packages: [],
          queue_relative_path: '07_运营数据/nlcl_feedback/formal_knowledge_promotion_queue.jsonl',
          queue_rows_appended: 1,
          policy_version: 'local-v1-formal-knowledge-promotion-1',
          evaluated_at: '2026-07-26T00:00:00+00:00',
          receipt_hash: '1'.repeat(64),
        },
        history: [],
        not_claiming: ['formal_knowledge_auto_activation'],
      }))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.formal-knowledge-promotion-response.v1',
        change_set_id: changeSetId,
        receipt: {
          schema: 'zhimeng.formal-knowledge-promotion-receipt.v1',
          receipt_id: 'formal-knowledge-promotion-' + '5'.repeat(32),
          change_set_id: changeSetId,
          project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
          feedback_candidate_bundle_id: null,
          feedback_candidate_bundle_hash: null,
          status: 'rejected',
          authorization_input_ready: false,
          formal_knowledge_contaminated: false,
          formal_draft_allowed: false,
          writes_formal_knowledge: false,
          quality_assessed: false,
          checks: [],
          missing_required: [],
          items: [],
          packages: [],
          queue_relative_path: null,
          queue_rows_appended: 0,
          policy_version: 'local-v1-formal-knowledge-promotion-1',
          evaluated_at: '2026-07-26T00:00:00+00:00',
          receipt_hash: '6'.repeat(64),
        },
        history: [],
        not_claiming: ['formal_knowledge_auto_activation'],
      }))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.formal-knowledge-promotion-response.v1',
        change_set_id: changeSetId,
        receipt: {
          schema: 'zhimeng.formal-knowledge-promotion-receipt.v1',
          receipt_id: 'formal-knowledge-promotion-' + '7'.repeat(32),
          change_set_id: changeSetId,
          project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
          feedback_candidate_bundle_id: null,
          feedback_candidate_bundle_hash: null,
          status: 'rolled_back',
          authorization_input_ready: false,
          formal_knowledge_contaminated: false,
          formal_draft_allowed: false,
          writes_formal_knowledge: false,
          quality_assessed: false,
          checks: [],
          missing_required: [],
          items: [],
          packages: [],
          queue_relative_path: null,
          queue_rows_appended: 0,
          policy_version: 'local-v1-formal-knowledge-promotion-1',
          evaluated_at: '2026-07-26T00:00:00+00:00',
          receipt_hash: '8'.repeat(64),
        },
        history: [],
        not_claiming: ['formal_knowledge_auto_activation'],
      }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const loaded = await getFormalKnowledgePromotion(changeSetId, options)
    expect(loaded.receipt?.status).toBe('staged')
    expect(String(fetcher.mock.calls[0]?.[0])).toContain('/formal-knowledge-promotion')
    const rejected = await actFormalKnowledgePromotion(
      changeSetId,
      '2'.repeat(64),
      [{ candidate_id: 'feedback-candidate-' + '3'.repeat(32), action: 'reject' }],
      options,
    )
    expect(rejected.receipt?.status).toBe('rejected')
    expect(rejected.receipt?.writes_formal_knowledge).toBe(false)
    const rolled = await actFormalKnowledgePromotion(
      changeSetId,
      '2'.repeat(64),
      [{ candidate_id: 'feedback-candidate-' + '3'.repeat(32), action: 'rollback_stage' }],
      options,
    )
    expect(rolled.receipt?.status).toBe('rolled_back')
    expect(rolled.receipt?.formal_draft_allowed).toBe(false)
  })

})
