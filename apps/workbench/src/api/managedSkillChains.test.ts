import { describe, expect, it, vi } from 'vitest'
import {
  WORKBENCH_DUAL_PROJECT_AUDIT_STEPS,
  prepareManagedSkillChain,
  runManagedSkillChain,
} from './managedSkillChains'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const responseFixture = {
  schema: 'zhimeng.managed-skill-chain-response.v1',
  record: {
    schema: 'zhimeng.managed-skill-chain-run.v1',
    chain_id: 'managed-skill-chain-' + 'a'.repeat(32),
    chain_key: 'workbench-dual-project-audit',
    status: 'prepared',
    project: {
      project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      revision: 1,
      fingerprint: 'b'.repeat(64),
      root_path: 'C:/tmp/book',
    },
    steps: WORKBENCH_DUAL_PROJECT_AUDIT_STEPS.map((step, index) => ({
      ...step,
      ordinal: index + 1,
      status: 'prepared',
      input_payload: step.input_payload ?? {},
      input_hash: 'c'.repeat(64),
      skill_receipt_id: null,
      skill_receipt_hash: null,
      result_hash: null,
      domain_status: null,
      error_code: null,
      error_message: null,
    })),
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
    risk_dossier: {
      formal_draft_allowed: false,
      network: false,
      secrets: [],
      permissions: {
        read: ['bound_project_snapshot'],
        write: ['run_staging'],
        network: false,
        secrets: [],
      },
      skills: [],
      risk_summary: 'fixture',
      not_claiming: ['four_skills_generic_exactly_once'],
    },
    risk_approval_token: null,
    workflow_plan: null,
    workflow_checkpoints: [],
    formal_draft_allowed: false,
    current_step_ordinal: 0,
    error_code: null,
    error_message: null,
    created_at: '2026-07-26T00:00:00+00:00',
    updated_at: '2026-07-26T00:00:00+00:00',
  },
  not_claiming: [
    'four_skills_generic_exactly_once',
    'raw_skill_md_execution',
    'creation_suite_drafting',
    'reader_gate',
    'local_v1_exe',
    'formal_prose_writeback',
  ],
}

describe('managedSkillChains API', () => {
  it('prepares a dual project-audit chain', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(responseFixture))
    const result = await prepareManagedSkillChain(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      {
        expected_project_revision: 1,
        expected_project_fingerprint: 'b'.repeat(64),
        chain_key: 'workbench-dual-project-audit',
        steps: WORKBENCH_DUAL_PROJECT_AUDIT_STEPS,
      },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.record.status).toBe('prepared')
    expect(result.record.formal_draft_allowed).toBe(false)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/managed-skill-chains/prepare',
    )
  })

  it('runs with a long timeout', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      ...responseFixture,
      record: { ...responseFixture.record, status: 'succeeded' },
    }))
    await runManagedSkillChain(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      responseFixture.record.chain_id,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(fetcher).toHaveBeenCalled()
  })
})
