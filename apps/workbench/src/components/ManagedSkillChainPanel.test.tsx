import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { ManagedSkillChainPanel } from './ManagedSkillChainPanel'
import { WORKBENCH_DUAL_PROJECT_AUDIT_STEPS } from '../api/managedSkillChains'

const prepared = {
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
      risk_summary: '一次批准覆盖双步项目审计；formal_draft_allowed 保持 false。',
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

describe('ManagedSkillChainPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('prepares a dual project-audit chain from the skills surface', async () => {
    const user = userEvent.setup()
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response(JSON.stringify(prepared), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    render(
      <ManagedSkillChainPanel
        enabled
        projectId="project-aaaaaaaaaaaaaaaaaaaaaaaa"
        projectRevision={1}
        projectFingerprint={'b'.repeat(64)}
      />,
    )

    await user.click(screen.getByRole('button', { name: '准备双步项目审计' }))
    await waitFor(() => {
      expect(screen.getByText('通用链已准备')).toBeInTheDocument()
    })
    expect(screen.getByText(/第一步项目审计/)).toBeInTheDocument()
    expect(screen.getAllByText(/formal_draft_allowed=false/).length).toBeGreaterThan(0)
    expect(vi.mocked(globalThis.fetch).mock.calls[0]?.[0]).toContain('/managed-skill-chains/prepare')
  })
})
