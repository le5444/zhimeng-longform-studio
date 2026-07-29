import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { changeSetFixture } from '../test/changeSetFixture'
import { ReaderWorkbenchPanel } from './ReaderWorkbenchPanel'

const getReaderReview = vi.fn()
const getReaderReviewDecision = vi.fn()
const createReaderReview = vi.fn()
const decideReaderReview = vi.fn()

vi.mock('../api/changeSets', async () => {
  const actual = await vi.importActual<typeof import('../api/changeSets')>('../api/changeSets')
  return {
    ...actual,
    getReaderReview: (...args: unknown[]) => getReaderReview(...args),
    getReaderReviewDecision: (...args: unknown[]) => getReaderReviewDecision(...args),
    createReaderReview: (...args: unknown[]) => createReaderReview(...args),
    decideReaderReview: (...args: unknown[]) => decideReaderReview(...args),
  }
})

describe('ReaderWorkbenchPanel', () => {
  beforeEach(() => {
    getReaderReview.mockReset()
    getReaderReviewDecision.mockReset()
    createReaderReview.mockReset()
    decideReaderReview.mockReset()
  })

  it('runs cold-read review and author decisions without quality unlock claims', async () => {
    const detail = changeSetFixture({ status: 'applied' })
    getReaderReview.mockResolvedValue({
      schema: 'zhimeng.reader-review-response.v1',
      change_set_id: detail.change_set_id,
      receipt: null,
      history: [],
    })
    getReaderReviewDecision.mockResolvedValue({
      schema: 'zhimeng.reader-review-decision-response.v1',
      change_set_id: detail.change_set_id,
      receipt: null,
      history: [],
      follow_up_change_set: null,
    })
    createReaderReview.mockResolvedValue({
      schema: 'zhimeng.reader-review-response.v1',
      change_set_id: detail.change_set_id,
      receipt: {
        schema: 'zhimeng.reader-review-receipt.v1',
        receipt_id: 'reader-review-' + 'a'.repeat(32),
        change_set_id: detail.change_set_id,
        project_id: detail.project_id,
        outcome: 'applied',
        result_revision: 4,
        result_fingerprint: 'd'.repeat(64),
        review_mode: 'deterministic_cold_read_v1',
        allowed_input_classes: ['applied_result'],
        excluded_input_classes: ['gate_receipt'],
        candidate_relative_paths: ['05_正文内容/第一卷/CH001_正文.md'],
        findings: [{
          finding_id: 'finding-1',
          kind: 'continuity_risk',
          severity: 'watch',
          summary: '开场压力回落',
          evidence_quote: '林见川推开了门。',
          relative_path: '05_正文内容/第一卷/CH001_正文.md',
          overrides_gate: false,
        }],
        finding_counts: { watch: 1 },
        reader_opinion_recorded: true,
        overrides_gate: false,
        formal_draft_allowed: false,
        quality_assessed: false,
        novel_state_synced: false,
        created_at: '2026-07-26T00:00:00Z',
        receipt_hash: 'e'.repeat(64),
      },
      history: [],
    })
    decideReaderReview.mockResolvedValue({
      schema: 'zhimeng.reader-review-decision-response.v1',
      change_set_id: detail.change_set_id,
      receipt: {
        schema: 'zhimeng.reader-review-decision-receipt.v1',
        receipt_id: 'reader-decision-' + 'b'.repeat(32),
        change_set_id: detail.change_set_id,
        project_id: detail.project_id,
        reader_review_receipt_id: 'reader-review-' + 'a'.repeat(32),
        reader_review_receipt_hash: 'e'.repeat(64),
        result_revision: 4,
        result_fingerprint: 'd'.repeat(64),
        decisions: [{ finding_id: 'finding-1', decision: 'adopted' }],
        adopted_finding_ids: ['finding-1'],
        rejected_finding_ids: [],
        deferred_finding_ids: [],
        finalized: true,
        follow_up_change_set_id: 'changeset-follow-up',
        parent_reader_valid_for_new_candidate: false,
        formal_draft_allowed: false,
        quality_assessed: false,
        novel_state_synced: false,
        created_at: '2026-07-26T00:01:00Z',
        receipt_hash: 'f'.repeat(64),
      },
      history: [],
      follow_up_change_set: null,
    })

    const user = userEvent.setup()
    const onOpenChanges = vi.fn()
    render(
      <ReaderWorkbenchPanel
        enabled
        detail={detail}
        onOpenChanges={onOpenChanges}
      />,
    )

    expect(screen.getByLabelText('Reader 工作面')).toBeInTheDocument()
    expect(screen.getByText('quality_assessed=false')).toBeInTheDocument()
    expect(screen.getByText('formal_draft_allowed=false')).toBeInTheDocument()
    await waitFor(() => expect(getReaderReview).toHaveBeenCalled())

    await user.click(screen.getByLabelText('运行冷读 Reader'))
    await waitFor(() => expect(createReaderReview).toHaveBeenCalled())
    expect(screen.getByText(/开场压力回落/)).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('决策 finding-1'), 'adopted')
    await user.click(screen.getByRole('button', { name: /确认决策并生成 follow-up/ }))
    await waitFor(() => expect(decideReaderReview).toHaveBeenCalled())
    expect(screen.getByText(/follow-up changeset-follow-up/)).toBeInTheDocument()
    expect(screen.getByText('parent_reader_valid_for_new_candidate=false')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '在改动页继续反馈晋升' }))
    expect(onOpenChanges).toHaveBeenCalled()
  })
})
