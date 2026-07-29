import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LlmLiteraryScorePanel } from './LlmLiteraryScorePanel'

const getLlmLiteraryScore = vi.fn()
const evaluateLlmLiteraryScore = vi.fn()

vi.mock('../api/llmLiteraryScore', () => ({
  getLlmLiteraryScore: (...args: unknown[]) => getLlmLiteraryScore(...args),
  evaluateLlmLiteraryScore: (...args: unknown[]) => evaluateLlmLiteraryScore(...args),
}))

describe('LlmLiteraryScorePanel', () => {
  it('loads and evaluates offline stub score without quality unlock claims', async () => {
    getLlmLiteraryScore.mockReset()
    evaluateLlmLiteraryScore.mockReset()
    getLlmLiteraryScore.mockResolvedValue({
      schema: 'zhimeng.llm-literary-score-response.v1',
      change_set_id: 'changeset-1',
      receipt: null,
      history: [],
      not_claiming: ['quality_assessed', 'local_v1_exe'],
    })
    evaluateLlmLiteraryScore.mockResolvedValue({
      schema: 'zhimeng.llm-literary-score-response.v1',
      change_set_id: 'changeset-1',
      receipt: {
        schema: 'zhimeng.llm-literary-score-receipt.v1',
        receipt_id: 'llm-literary-score-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        change_set_id: 'changeset-1',
        project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
        outcome: 'ready_candidate',
        result_revision: null,
        result_fingerprint: null,
        review_revision: 1,
        scorer_mode: 'offline_stub',
        advisory_only: true,
        llm_score_recorded: true,
        quality_assessed: false,
        subjective_quality_unlocked: false,
        formal_draft_allowed: false,
        authorization_unlock: false,
        provider_model: null,
        literary_quality_receipt_id: null,
        axes: [
          { code: 'immersion', score: 70, weight: 22, weighted_score: 1540, rationale: 'stub' },
          { code: 'character_voice', score: 68, weight: 20, weighted_score: 1360, rationale: 'stub' },
          { code: 'emotional_arc', score: 66, weight: 20, weighted_score: 1320, rationale: 'stub' },
          { code: 'prose_craft', score: 72, weight: 20, weighted_score: 1440, rationale: 'stub' },
          { code: 'suspense_pull', score: 64, weight: 18, weighted_score: 1152, rationale: 'stub' },
        ],
        total_score: 68,
        detail: 'offline stub only',
        policy_version: 'local-v1-llm-literary-score-1',
        evaluated_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'b'.repeat(64),
      },
      history: [],
      not_claiming: ['quality_assessed', 'local_v1_exe'],
    })

    const user = userEvent.setup()
    render(
      <LlmLiteraryScorePanel
        enabled
        changeSetId="changeset-1"
        reviewRevision={1}
      />,
    )

    await waitFor(() => expect(getLlmLiteraryScore).toHaveBeenCalled())
    await user.click(screen.getByLabelText('评估 LLM 形文学分'))
    await waitFor(() => expect(evaluateLlmLiteraryScore).toHaveBeenCalled())
    expect(screen.getAllByText(/quality_assessed=false/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/offline stub/).length).toBeGreaterThan(0)
    expect(screen.getByText('沉浸感')).toBeInTheDocument()
  })
})
