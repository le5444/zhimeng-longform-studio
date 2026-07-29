import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExternalBlindReviewMatrixPanel } from './ExternalBlindReviewMatrixPanel'

const evaluateExternalBlindReviewMatrix = vi.fn()

vi.mock('../api/externalBlindReviewMatrix', () => ({
  evaluateExternalBlindReviewMatrix: (...args: unknown[]) =>
    evaluateExternalBlindReviewMatrix(...args),
}))

describe('ExternalBlindReviewMatrixPanel', () => {
  it('evaluates fail-closed empty matrix and never claims quality unlock', async () => {
    evaluateExternalBlindReviewMatrix.mockReset()
    evaluateExternalBlindReviewMatrix.mockResolvedValue({
      schema: 'zhimeng.external-blind-review-matrix-response.v1',
      receipt: {
        schema: 'zhimeng.external-blind-review-matrix-receipt.v1',
        receipt_id: 'external-blind-review-matrix-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        policy_version: 'local-v1-external-blind-review-matrix-1',
        suite_id: null,
        evidence_id: null,
        matrix_ready: false,
        required_cell_count: 6,
        passed_cell_count: 0,
        missing_required: [
          'external_reviewer_1:chapter_sample_a',
          'external_reviewer_1:chapter_sample_b',
        ],
        cells: [
          {
            reviewer_id: 'external_reviewer_1',
            sample_id: 'chapter_sample_a',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
          {
            reviewer_id: 'external_reviewer_1',
            sample_id: 'chapter_sample_b',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
          {
            reviewer_id: 'external_reviewer_2',
            sample_id: 'chapter_sample_a',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
          {
            reviewer_id: 'external_reviewer_2',
            sample_id: 'chapter_sample_b',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
          {
            reviewer_id: 'external_reviewer_3',
            sample_id: 'chapter_sample_a',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
          {
            reviewer_id: 'external_reviewer_3',
            sample_id: 'chapter_sample_b',
            required: true,
            status: 'missing',
            present: false,
            detail: 'required cell missing from evidence',
            score: null,
            evidence_ref: null,
          },
        ],
        quality_assessed: false,
        formal_draft_allowed: false,
        llm_literary_score_claim_allowed: false,
        local_v1_exe_claim_allowed: false,
        detail: 'no external blind-review matrix evidence provided',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'c'.repeat(64),
      },
      not_claiming: ['quality_assessed', 'external_blind_review_matrix', 'local_v1_exe'],
    })

    const user = userEvent.setup()
    render(<ExternalBlindReviewMatrixPanel enabled />)
    await user.click(screen.getByLabelText('评估外部盲评矩阵'))
    await waitFor(() => expect(evaluateExternalBlindReviewMatrix).toHaveBeenCalled())
    expect(screen.getByText(/矩阵未就绪/)).toBeInTheDocument()
    expect(screen.getAllByText(/quality_assessed=false/).length).toBeGreaterThan(0)
  })
})
