import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FormalDraftAuthorizationPanel } from './FormalDraftAuthorizationPanel'

const authFixture = {
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
      { code: 'project_bound', satisfied: true, detail: 'project bound at revision 1' },
      { code: 'four_skills_continuous_o2', satisfied: false, detail: 'incomplete' },
      { code: 'author_explicit_authorization', satisfied: false, detail: 'no author artifact' },
    ],
    denials: [
      { code: 'local_v1_policy_locked', message: 'Local v1 keeps formal_draft_allowed locked false' },
    ],
    policy_version: 'local-v1-locked-1',
    evaluated_at: '2026-07-26T00:00:00+00:00',
    receipt_hash: 'c'.repeat(64),
  },
  not_claiming: ['formal_prose_writeback', 'local_v1_exe'],
}

const decisionFixture = {
  schema: 'zhimeng.formal-draft-author-decision-response.v1',
  receipt: null,
  history: [],
  not_claiming: ['formal_draft_authorization_grant'],
}

describe('FormalDraftAuthorizationPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/author-decision')) {
        return new Response(JSON.stringify(decisionFixture), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/provenance/evaluate')) {
        return new Response(JSON.stringify({
          schema: 'zhimeng.formal-draft-provenance-response.v1',
          receipt: {
            schema: 'zhimeng.formal-draft-provenance-receipt.v1',
            receipt_id: 'formal-draft-provenance-' + 'd'.repeat(32),
            project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
            project_revision: 1,
            project_fingerprint: 'b'.repeat(64),
            change_set_id: null,
            status: 'scope_limited',
            authorization_input_ready: false,
            authorization_unlock: false,
            formal_draft_allowed: false,
            writes_project_truth: false,
            checks: [
              { code: 'project_bound', required: true, present: true, detail: 'bound' },
              { code: 'author_decision_receipt', required: true, present: false, detail: 'missing' },
            ],
            missing_required: ['author_decision_receipt'],
            policy_version: 'local-v1-provenance-1',
            evaluated_at: '2026-07-26T00:00:00+00:00',
            receipt_hash: 'e'.repeat(64),
          },
          not_claiming: ['formal_draft_authorization_unlock'],
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
            if (url.includes('/continuous-o2/evaluate')) {
        return new Response(JSON.stringify({
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
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
return new Response(JSON.stringify(authFixture), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('shows a locked formal-draft authorization receipt', async () => {
    render(
      <FormalDraftAuthorizationPanel
        enabled
        projectId="project-aaaaaaaaaaaaaaaaaaaaaaaa"
        projectRevision={1}
        projectFingerprint={'b'.repeat(64)}
      />,
    )
    await waitFor(() => {
      expect(screen.getByText('正式正文写权未授予')).toBeInTheDocument()
    })
    expect(screen.getByText(/local_v1_policy_locked/)).toBeInTheDocument()
    expect(screen.getByText(/four_skills_continuous_o2/)).toBeInTheDocument()
    expect(screen.getByText(/formal_prose_writeback/)).toBeInTheDocument()
    expect(screen.getByText(/尚未记录作者正式起稿授权决策/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '请求正式起稿授权' })).toBeInTheDocument()
    expect(screen.getByText(/连续 O2 就绪/)).toBeInTheDocument()
    expect(screen.getAllByText(/adjacent_o2_chain_succeeded/).length).toBeGreaterThan(0)
  })
})
