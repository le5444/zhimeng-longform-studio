import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GatePipelinePanel } from './GatePipelinePanel'

const getGatePipeline = vi.fn()
const evaluateGatePipeline = vi.fn()

vi.mock('../api/gatePipeline', () => ({
  getGatePipeline: (...args: unknown[]) => getGatePipeline(...args),
  evaluateGatePipeline: (...args: unknown[]) => evaluateGatePipeline(...args),
}))

describe('GatePipelinePanel', () => {
  beforeEach(() => {
    getGatePipeline.mockReset()
    evaluateGatePipeline.mockReset()
  })

  it('loads and evaluates an ordered gate pipeline without unlocking formal draft', async () => {
    getGatePipeline.mockResolvedValue({
      schema: 'zhimeng.gate-pipeline-response.v1',
      change_set_id: 'changeset-1',
      receipt: null,
      history: [],
      not_claiming: ['literary_quality_score'],
    })
    evaluateGatePipeline.mockResolvedValue({
      schema: 'zhimeng.gate-pipeline-response.v1',
      change_set_id: 'changeset-1',
      receipt: {
        schema: 'zhimeng.gate-pipeline-receipt.v1',
        receipt_id: 'gate-pipeline-' + 'a'.repeat(32),
        change_set_id: 'changeset-1',
        project_id: 'project-' + 'b'.repeat(24),
        outcome: 'applied',
        result_revision: 2,
        result_fingerprint: 'c'.repeat(64),
        review_revision: 1,
        status: 'complete',
        steps: [
          {
            code: 'post_draft_gate',
            ordinal: 1,
            required: false,
            present: false,
            status: 'missing',
            receipt_id: null,
            detail: 'post-draft missing',
          },
          {
            code: 'literary_quality_gate',
            ordinal: 2,
            required: true,
            present: true,
            status: 'advisory_only',
            receipt_id: 'literary-quality-' + 'd'.repeat(32),
            detail: 'advisory',
          },
          {
            code: 'llm_literary_score',
            ordinal: 3,
            required: false,
            present: true,
            status: 'offline_stub',
            receipt_id: 'llm-literary-score-' + '1'.repeat(32),
            detail: 'offline stub',
          },
          {
            code: 'external_blind_review_matrix',
            ordinal: 4,
            required: false,
            present: true,
            status: 'missing_evidence',
            receipt_id: null,
            detail: 'missing evidence',
          },
          {
            code: 'hard_fact_gate',
            ordinal: 5,
            required: true,
            present: true,
            status: 'passed',
            receipt_id: 'hard-fact-gate-' + 'e'.repeat(32),
            detail: 'passed',
          },
        ],
        hard_fact_receipt_id: 'hard-fact-gate-' + 'e'.repeat(32),
        literary_quality_receipt_id: 'literary-quality-' + 'd'.repeat(32),
        llm_literary_score_receipt_id: 'llm-literary-score-' + '1'.repeat(32),
        external_blind_review_matrix_ready: false,
        post_draft_gate_status: 'missing',
        authorization_input_ready: true,
        authorization_unlock: false,
        formal_draft_allowed: false,
        quality_assessed: false,
        policy_version: 'local-v1-gate-pipeline-2',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'f'.repeat(64),
      },
      history: [],
      not_claiming: [
        'literary_quality_score',
        'llm_literary_score_quality_unlock',
        'formal_draft_authorization_unlock',
      ],
    })

    const user = userEvent.setup()
    render(
      <GatePipelinePanel
        enabled
        changeSetId="changeset-1"
        reviewRevision={1}
      />,
    )

    await waitFor(() => expect(getGatePipeline).toHaveBeenCalled())
    await user.click(screen.getByLabelText('评估 Gate Pipeline'))
    await waitFor(() => expect(evaluateGatePipeline).toHaveBeenCalled())
    expect(screen.getByText(/status=complete/)).toBeInTheDocument()
    expect(screen.getAllByText(/formal_draft_allowed=false/).length).toBeGreaterThan(0)
    expect(screen.getByText(/2\. 文学质量/)).toBeInTheDocument()
    expect(screen.getByText(/3\. LLM 形分/)).toBeInTheDocument()
    expect(screen.getByText(/4\. 外部盲评/)).toBeInTheDocument()
    expect(screen.getByText(/5\. 硬事实/)).toBeInTheDocument()
    expect(screen.getByText('literary_quality_score')).toBeInTheDocument()
  })
})
