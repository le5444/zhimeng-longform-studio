import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LiteraryQualityGatePanel } from './LiteraryQualityGatePanel'

const getLiteraryQualityGate = vi.fn()
const evaluateLiteraryQualityGate = vi.fn()

vi.mock('../api/literaryQualityGate', () => ({
  getLiteraryQualityGate: (...args: unknown[]) => getLiteraryQualityGate(...args),
  evaluateLiteraryQualityGate: (...args: unknown[]) => evaluateLiteraryQualityGate(...args),
}))

describe('LiteraryQualityGatePanel', () => {
  beforeEach(() => {
    getLiteraryQualityGate.mockReset()
    evaluateLiteraryQualityGate.mockReset()
  })

  it('renders deterministic rubric cards without claiming quality_assessed', async () => {
    getLiteraryQualityGate.mockResolvedValue({
      schema: 'zhimeng.literary-quality-gate-response.v1',
      change_set_id: 'changeset-1',
      receipt: null,
      history: [],
      not_claiming: ['quality_assessed', 'literary_quality_score'],
    })
    evaluateLiteraryQualityGate.mockResolvedValue({
      schema: 'zhimeng.literary-quality-gate-response.v1',
      change_set_id: 'changeset-1',
      receipt: {
        schema: 'zhimeng.literary-quality-gate-receipt.v1',
        receipt_id: 'literary-quality-' + 'a'.repeat(32),
        change_set_id: 'changeset-1',
        project_id: 'project-' + 'b'.repeat(24),
        outcome: 'applied',
        result_revision: 2,
        result_fingerprint: 'c'.repeat(64),
        review_revision: 1,
        gate_mode: 'deterministic_structural_rubric_v2',
        decision: 'advisory_only',
        authorization_unlock: false,
        formal_draft_allowed: false,
        quality_assessed: false,
        structural_checks_run: true,
        deterministic_rubric_scored: true,
        dimensions: [
          { code: 'structure', status: 'pass', detail: '结构完整' },
        ],
        findings: [],
        finding_counts: {},
        rubric_scores: [
          {
            code: 'dialogue_balance',
            score: 80,
            weight: 0.2,
            weighted_score: 16,
            detail: '对白占比健康',
          },
          {
            code: 'hook_opening',
            score: 62,
            weight: 0.2,
            weighted_score: 12.4,
            detail: '开场尚可',
          },
        ],
        rubric_total_score: 71,
        policy_version: 'local-v1-advisory-2',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'd'.repeat(64),
      },
      history: [],
      not_claiming: ['quality_assessed', 'literary_quality_score'],
    })

    const user = userEvent.setup()
    render(
      <LiteraryQualityGatePanel
        enabled
        changeSetId="changeset-1"
        reviewRevision={1}
      />,
    )

    await waitFor(() => expect(getLiteraryQualityGate).toHaveBeenCalled())
    await user.click(screen.getByLabelText('评估文学质量 Gate'))
    await waitFor(() => expect(evaluateLiteraryQualityGate).toHaveBeenCalled())
    expect(screen.getByText('对白平衡')).toBeInTheDocument()
    expect(screen.getByText('开场钩子')).toBeInTheDocument()
    expect(screen.getByLabelText('rubric 总分')).toHaveTextContent('71')
    expect(screen.getByText(/quality_assessed=false/)).toBeInTheDocument()
    expect(screen.getByText(/not quality_assessed/)).toBeInTheDocument()
  })
})
