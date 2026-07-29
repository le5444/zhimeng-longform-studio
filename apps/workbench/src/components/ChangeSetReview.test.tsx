import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyChangeSet,
  getPostDraftGate,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  runPostDraftGate,
  updateHunkDecisions,
} from '../api/changeSets'
import {
  changeSetFixture,
  changeSetProjectId,
  postDraftGateReceiptFixture,
  changeSetSummaryFixture,
} from '../test/changeSetFixture'
import { ChangeSetReview } from './ChangeSetReview'

vi.mock('../api/changeSets', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/changeSets')>()
  return {
    ...actual,
    applyChangeSet: vi.fn(),
    getPostDraftGate: vi.fn(),
    getChangeSet: vi.fn(),
    listProjectChangeSets: vi.fn(),
    rollbackChangeSet: vi.fn(),
    runPostDraftGate: vi.fn(),
    updateHunkDecisions: vi.fn(),
  }
})

describe('ChangeSetReview', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('reviews a Hunk, applies the ready ChangeSet, and exposes rollback', async () => {
    const user = userEvent.setup()
    const pending = changeSetFixture()
    const ready = changeSetFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1 })
    const applied = changeSetFixture({ status: 'applied', decision: 'accepted', reviewRevision: 1 })
    vi.mocked(listProjectChangeSets).mockResolvedValue([changeSetSummaryFixture()])
    vi.mocked(getChangeSet).mockResolvedValue(pending)
    vi.mocked(updateHunkDecisions).mockResolvedValue(ready)
    vi.mocked(applyChangeSet).mockResolvedValue(applied)
    const projectChanged = vi.fn()

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={projectChanged} />)

    expect(await screen.findByText(/黄铜钥匙硌着掌心/)).toBeInTheDocument()
    expect(screen.getByLabelText('Gate 状态：未启用')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /接受 .* Hunk 1/ }))
    await waitFor(() => expect(updateHunkDecisions).toHaveBeenCalledOnce())

    await user.click(await screen.findByRole('button', { name: '应用所选更改' }))
    await waitFor(() => expect(applyChangeSet).toHaveBeenCalledWith(applied.change_set_id, 1))
    expect(await screen.findByRole('button', { name: '回滚 ChangeSet' })).toBeInTheDocument()
    expect(projectChanged).toHaveBeenCalledOnce()
  })

  it('rolls back an applied ChangeSet through the current review revision', async () => {
    const user = userEvent.setup()
    const applied = changeSetFixture({ status: 'applied', decision: 'accepted', reviewRevision: 3 })
    const rolledBack = changeSetFixture({ status: 'rolled_back', decision: 'accepted', reviewRevision: 3 })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'applied', decision: 'accepted', reviewRevision: 3 }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(applied)
    vi.mocked(rollbackChangeSet).mockResolvedValue(rolledBack)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    await user.click(await screen.findByRole('button', { name: '回滚 ChangeSet' }))
    await waitFor(() => expect(rollbackChangeSet).toHaveBeenCalledWith(applied.change_set_id, 3))
    expect((await screen.findAllByText('已回滚')).length).toBeGreaterThanOrEqual(1)
    expect(await screen.findByText('项目结果 revision 5')).toBeInTheDocument()
    expect(screen.queryByText('项目结果 revision 4')).not.toBeInTheDocument()
  })

  it('blocks writeback until a current Gate passes, then supports rerunning Gate', async () => {
    const user = userEvent.setup()
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
    })
    const passed = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 1 }),
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(ready)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: ready.change_set_id,
      project_id: ready.project_id,
      gate_required: true,
      review_revision: 1,
      receipt: null,
    })
    vi.mocked(runPostDraftGate).mockResolvedValue(passed)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('未运行')).toBeInTheDocument()
    expect(screen.getByLabelText('ChangeSet 来源')).toHaveTextContent('Run run-local-1')
    expect(screen.getByLabelText('ChangeSet 来源')).toHaveTextContent('Tool tool-call-local-1')
    const blockedApply = screen.getByRole('button', { name: '应用所选更改' })
    expect(blockedApply).toBeDisabled()
    expect(blockedApply).toHaveAccessibleDescription('尚未针对当前候选运行 Gate，应用按钮保持禁用。')
    await user.click(screen.getByRole('button', { name: '运行 Gate' }))
    await waitFor(() => expect(runPostDraftGate).toHaveBeenCalledWith(ready.change_set_id, 1))
    expect(await screen.findByText('通过')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '应用所选更改' })).toBeEnabled()
  })

  it('allows an all-rejected gated ChangeSet to be discarded without running Gate', async () => {
    const user = userEvent.setup()
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'rejected',
      reviewRevision: 1,
      gateRequired: true,
    })
    const discarded = changeSetFixture({
      status: 'discarded',
      decision: 'rejected',
      reviewRevision: 1,
      gateRequired: true,
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'rejected', reviewRevision: 1, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(ready)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: ready.change_set_id,
      project_id: ready.project_id,
      gate_required: true,
      review_revision: 1,
      receipt: null,
    })
    vi.mocked(applyChangeSet).mockResolvedValue(discarded)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('没有接受的 Hunk；放弃候选不会写回项目，无需 Gate 收据。')).toBeInTheDocument()
    const discard = screen.getByRole('button', { name: '放弃这组改动' })
    expect(discard).toBeEnabled()
    expect(screen.queryByRole('button', { name: /运行 Gate/ })).not.toBeInTheDocument()
    expect(getPostDraftGate).not.toHaveBeenCalled()
    await user.click(discard)
    await waitFor(() => expect(applyChangeSet).toHaveBeenCalledWith(ready.change_set_id, 1))
    expect(runPostDraftGate).not.toHaveBeenCalled()
  })

  it('allows accepted non-prose changes to apply without a post-draft body Gate', async () => {
    const user = userEvent.setup()
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGateScope: 'none',
    })
    const applied = changeSetFixture({
      status: 'applied',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGateScope: 'none',
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({
        status: 'ready',
        decision: 'accepted',
        reviewRevision: 1,
        gateRequired: true,
        postDraftGateScope: 'none',
      }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(ready)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: ready.change_set_id,
      project_id: ready.project_id,
      gate_required: true,
      review_revision: 1,
      receipt: null,
    })
    vi.mocked(applyChangeSet).mockResolvedValue(applied)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('当前接受的 Hunk 不属于正文 Gate 范围，无需 Gate 收据。')).toBeInTheDocument()
    expect(screen.getByLabelText('Gate 状态：未启用')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /运行 Gate/ })).not.toBeInTheDocument()
    expect(getPostDraftGate).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '应用所选更改' }))
    await waitFor(() => expect(applyChangeSet).toHaveBeenCalledWith(ready.change_set_id, 1))
    expect(runPostDraftGate).not.toHaveBeenCalled()
  })

  it('renders blocked Gate checks and file findings with source locations', async () => {
    const blocked = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'blocked', reviewRevision: 1 }),
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(blocked)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: blocked.change_set_id,
      project_id: blocked.project_id,
      gate_required: true,
      review_revision: 1,
      receipt: blocked.post_draft_gate,
    })

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('阻断')).toBeInTheDocument()
    expect(screen.getByLabelText('Gate 检查项')).toBeInTheDocument()
    expect(screen.getByText('检查状态：阻断')).toBeInTheDocument()
    expect(screen.getByLabelText('Gate 阻断定位')).toBeInTheDocument()
    expect(screen.getAllByText('draft_role_marker_leak').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/L1:C1-L1:C4/)).toBeInTheDocument()
    expect(screen.getAllByText('05_正文内容/第一卷/CH001_正文.md').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByRole('button', { name: '应用所选更改' })).toBeDisabled()
  })

  it('offers discard instead of an impossible Gate rerun after the final accepted Hunk is rejected', async () => {
    const user = userEvent.setup()
    const receipt = postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 1 })
    const current = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGate: receipt,
    })
    const rejected = changeSetFixture({
      status: 'ready',
      decision: 'rejected',
      reviewRevision: 2,
      gateRequired: true,
      postDraftGate: null,
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(current)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: current.change_set_id,
      project_id: current.project_id,
      gate_required: true,
      review_revision: 1,
      receipt,
    })
    vi.mocked(updateHunkDecisions).mockResolvedValue(rejected)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByLabelText('Gate 状态：通过')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /拒绝 .* Hunk 1/ }))

    expect(await screen.findByText('没有接受的 Hunk；放弃候选不会写回项目，无需 Gate 收据。')).toBeInTheDocument()
    expect(screen.queryByText('审阅 revision 已变化，旧收据已隐藏。请重新运行 Gate。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重跑 Gate/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '放弃这组改动' })).toBeEnabled()
  })

  it('hides a receipt from an older review revision and marks Gate stale', async () => {
    const stale = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 2,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'blocked', reviewRevision: 1 }),
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 2, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(stale)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: stale.change_set_id,
      project_id: stale.project_id,
      gate_required: true,
      review_revision: 2,
      receipt: stale.post_draft_gate,
    })

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('已过期')).toBeInTheDocument()
    expect(screen.getByText('审阅 revision 已变化，旧收据已隐藏。请重新运行 Gate。')).toBeInTheDocument()
    expect(screen.queryByText('候选包含阻断项')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '应用所选更改' })).toBeDisabled()
  })

  it('shows a failed Gate receipt and keeps writeback disabled', async () => {
    const failed = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'failed', reviewRevision: 1 }),
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(failed)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: failed.change_set_id,
      project_id: failed.project_id,
      gate_required: true,
      review_revision: 1,
      receipt: failed.post_draft_gate,
    })

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('Gate 执行失败，候选未获得写回授权。')).toBeInTheDocument()
    expect(screen.getByText(/post_draft_gate_unavailable/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '应用所选更改' })).toBeDisabled()
  })

  it('renders a stable empty state when no ChangeSet exists', async () => {
    vi.mocked(listProjectChangeSets).mockResolvedValue([])

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('没有候选改动')).toBeInTheDocument()
    expect(getChangeSet).not.toHaveBeenCalled()
  })

  it('labels an all-rejected ready ChangeSet as an explicit discard action', async () => {
    const user = userEvent.setup()
    const ready = changeSetFixture({ status: 'ready', decision: 'rejected', reviewRevision: 1 })
    const discarded = changeSetFixture({ status: 'discarded', decision: 'rejected', reviewRevision: 1 })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'rejected', reviewRevision: 1 }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(ready)
    vi.mocked(applyChangeSet).mockResolvedValue(discarded)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    await user.click(await screen.findByRole('button', { name: '放弃这组改动' }))
    await waitFor(() => expect(applyChangeSet).toHaveBeenCalledWith(ready.change_set_id, 1))
    expect(await screen.findByText('这组改动已放弃，未写入项目')).toBeInTheDocument()
  })

  it('does not describe terminal conflict and failure states as refreshable retries', async () => {
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'conflicted', decision: 'accepted', reviewRevision: 1 }),
    ])
    vi.mocked(getChangeSet).mockResolvedValue(
      changeSetFixture({ status: 'conflicted', decision: 'accepted', reviewRevision: 1 }),
    )

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText('项目内容已变化，请基于最新项目重新生成候选')).toBeInTheDocument()
    expect(screen.queryByText(/检查冲突原因/)).not.toBeInTheDocument()
  })

  it('accepts all pending hunks through batch decision controls', async () => {
    const user = userEvent.setup()
    const pending = changeSetFixture()
    const ready = changeSetFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1 })
    vi.mocked(listProjectChangeSets).mockResolvedValue([changeSetSummaryFixture()])
    vi.mocked(getChangeSet).mockResolvedValue(pending)
    vi.mocked(updateHunkDecisions).mockResolvedValue(ready)

    render(<ChangeSetReview projectId={changeSetProjectId} onProjectChanged={vi.fn()} />)

    expect(await screen.findByText(/黄铜钥匙硌着掌心/)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: '全部接受待定' }))
    await waitFor(() => expect(updateHunkDecisions).toHaveBeenCalled())
    const decisions = vi.mocked(updateHunkDecisions).mock.calls[0]?.[2]
    expect(Array.isArray(decisions)).toBe(true)
    expect(decisions?.every((item) => item.decision === 'accepted')).toBe(true)
  })

})
