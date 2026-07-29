import { describe, expect, it, vi } from 'vitest'
import { evaluateFormalDraftAuthorization, evaluateFormalDraftContinuousO2 } from './formalDraftAuthorization'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('formalDraftAuthorization API', () => {
  it('evaluates formal draft authorization as denied', async () => {
    const fixture = {
      schema: 'zhimeng.formal-draft-authorization-response.v1',
      receipt: {
        schema: 'zhimeng.formal-draft-authorization-receipt.v1',
        receipt_id: 'formal-draft-auth-' + 'a'.repeat(32),
        project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
        project_revision: 1,
        project_fingerprint: 'b'.repeat(64),
        context_pack_id: null,
        run_id: null,
        change_set_id: null,
        decision: 'denied',
        formal_draft_allowed: false,
        writes_project_truth: false,
        prerequisites: [
          { code: 'project_bound', satisfied: true, detail: 'bound' },
          { code: 'four_skills_continuous_o2', satisfied: false, detail: 'missing' },
        ],
        denials: [
          { code: 'local_v1_policy_locked', message: 'locked' },
        ],
        policy_version: 'local-v1-locked-1',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'c'.repeat(64),
      },
      not_claiming: ['formal_prose_writeback'],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(fixture))
    const result = await evaluateFormalDraftAuthorization(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      { expected_project_revision: 1, expected_project_fingerprint: 'b'.repeat(64) },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.receipt.formal_draft_allowed).toBe(false)
    expect(result.receipt.decision).toBe('denied')
    expect(fetcher.mock.calls[0]?.[0]).toContain('/formal-draft-authorization/evaluate')
  })

  it('evaluates continuous O2 readiness without unlock', async () => {
    const fixture = {
      schema: 'zhimeng.formal-draft-continuous-o2-response.v1',
      receipt: {
        schema: 'zhimeng.formal-draft-continuous-o2-receipt.v1',
        receipt_id: 'formal-draft-continuous-o2-' + 'd'.repeat(32),
        project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
        project_revision: 1,
        project_fingerprint: 'b'.repeat(64),
        status: 'incomplete',
        authorization_input_ready: false,
        authorization_unlock: false,
        formal_draft_allowed: false,
        writes_project_truth: false,
        four_skills_generic_exactly_once: false,
        checks: [
          { code: 'project_bound', required: true, present: true, detail: 'bound', evidence_id: null },
          { code: 'adjacent_o2_chain_succeeded', required: true, present: false, detail: 'missing', evidence_id: null },
        ],
        missing_required: ['adjacent_o2_chain_succeeded'],
        adjacent_chain_id: null,
        managed_skill_chain_id: null,
        creation_suite_chain_id: null,
        policy_version: 'local-v1-continuous-o2-1',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'e'.repeat(64),
      },
      not_claiming: ['four_skills_generic_exactly_once'],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(fixture))
    const result = await evaluateFormalDraftContinuousO2(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      { expected_project_revision: 1 },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.receipt.formal_draft_allowed).toBe(false)
    expect(result.receipt.four_skills_generic_exactly_once).toBe(false)
    expect(fetcher.mock.calls[0]?.[0]).toContain('/continuous-o2/evaluate')
  })
})
