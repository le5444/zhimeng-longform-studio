import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyChangeSet,
  getPostDraftGate,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  runPostDraftGate,
  updateHunkDecisions,
} from '../api/changeSets'
import { RuntimeApiError } from '../api/runtime'
import {
  changeSetFixture,
  changeSetProjectId,
  changeSetSummaryFixture,
  postDraftGateReceiptFixture,
} from '../test/changeSetFixture'
import { deriveChangeSetGateState, useChangeSets } from './useChangeSets'

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

describe('useChangeSets', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    const pending = changeSetFixture()
    vi.mocked(listProjectChangeSets).mockResolvedValue([changeSetSummaryFixture()])
    vi.mocked(getChangeSet).mockResolvedValue(pending)
    vi.mocked(getPostDraftGate).mockResolvedValue({
      schema: 'zhimeng.post-draft-gate-response.v1',
      change_set_id: pending.change_set_id,
      project_id: pending.project_id,
      gate_required: false,
      review_revision: pending.review_revision,
      receipt: null,
    })
  })

  it('treats the Gate endpoint null receipt and review revision as authoritative', () => {
    const detail = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 2,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 2 }),
    })
    const response = {
      schema: 'zhimeng.post-draft-gate-response.v1' as const,
      change_set_id: detail.change_set_id,
      project_id: detail.project_id,
      gate_required: true,
      review_revision: 2,
      receipt: null,
    }

    expect(deriveChangeSetGateState(detail, response)).toMatchObject({
      required: true,
      status: 'missing',
      receipt: null,
    })
    expect(deriveChangeSetGateState(detail, { ...response, review_revision: 1 })).toMatchObject({
      required: true,
      status: 'stale',
      receipt: null,
    })
    expect(deriveChangeSetGateState(detail, {
      ...response,
      change_set_id: 'changeset-fedcba9876543210fedcba9876543210',
    })).toMatchObject({
      required: true,
      status: 'stale',
      receipt: null,
    })
  })

  it('loads the queue and selected ChangeSet detail from separate endpoints', async () => {
    const { result } = renderHook(() => useChangeSets(changeSetProjectId))

    await waitFor(() => expect(result.current.detailState).toBe('ready'))

    expect(listProjectChangeSets).toHaveBeenCalledWith(changeSetProjectId)
    expect(getChangeSet).toHaveBeenCalledWith(result.current.selectedId)
    expect(result.current.detail?.title).toBe('收紧第一章开场')
  })

  it('selects the exact ChangeSet requested by a Run Activity', async () => {
    const requestedId = 'changeset-fedcba9876543210fedcba9876543210'
    const requestedDetail = {
      ...changeSetFixture(),
      change_set_id: requestedId,
      title: 'Agent 生成的指定候选',
    }
    const requestedSummary = {
      ...changeSetSummaryFixture(),
      change_set_id: requestedId,
      title: requestedDetail.title,
    }
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture(),
      requestedSummary,
    ])
    vi.mocked(getChangeSet).mockImplementation((changeSetId) => Promise.resolve(
      changeSetId === requestedId ? requestedDetail : changeSetFixture(),
    ))

    const { result } = renderHook(() => useChangeSets(
      changeSetProjectId,
      undefined,
      requestedId,
    ))

    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(requestedId))
    expect(result.current.selectedId).toBe(requestedId)
    expect(result.current.detail?.title).toBe('Agent 生成的指定候选')
  })

  it('reapplies the same Activity target after the reviewer manually selects another ChangeSet', async () => {
    const requested = changeSetFixture()
    const alternateId = 'changeset-fedcba9876543210fedcba9876543210'
    const alternate = {
      ...changeSetFixture(),
      change_set_id: alternateId,
      title: '手动查看的其他候选',
    }
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture(),
      { ...changeSetSummaryFixture(), change_set_id: alternateId, title: alternate.title },
    ])
    vi.mocked(getChangeSet).mockImplementation((changeSetId) => Promise.resolve(
      changeSetId === alternateId ? alternate : requested,
    ))

    const { result, rerender } = renderHook(
      ({ requestKey }) => useChangeSets(
        changeSetProjectId,
        undefined,
        requested.change_set_id,
        requestKey,
      ),
      { initialProps: { requestKey: 1 } },
    )

    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(requested.change_set_id))
    act(() => result.current.select(alternateId))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(alternateId))

    rerender({ requestKey: 2 })
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(requested.change_set_id))
  })

  it('uses each server revision for decision and apply, then refreshes the project', async () => {
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
    })
    const applied = changeSetFixture({
      status: 'applied',
      decision: 'accepted',
      reviewRevision: 1,
    })
    vi.mocked(updateHunkDecisions).mockResolvedValue(ready)
    vi.mocked(applyChangeSet).mockResolvedValue(applied)
    const projectChanged = vi.fn()
    const { result } = renderHook(() => useChangeSets(changeSetProjectId, projectChanged))

    await waitFor(() => expect(result.current.detailState).toBe('ready'))
    const hunkId = result.current.detail?.changes[0].hunks[0].hunk_id
    expect(hunkId).toBeTruthy()

    await act(async () => {
      await result.current.decide({ hunk_id: String(hunkId), decision: 'accepted' })
    })
    expect(updateHunkDecisions).toHaveBeenCalledWith(
      result.current.detail?.change_set_id,
      0,
      [{ hunk_id: hunkId, decision: 'accepted' }],
    )

    await act(async () => {
      await result.current.apply()
    })
    expect(applyChangeSet).toHaveBeenCalledWith(applied.change_set_id, 1)
    expect(result.current.detail?.status).toBe('applied')
    expect(projectChanged).toHaveBeenCalledOnce()
  })

  it('loads Gate status, runs the current revision, and exposes the passing receipt', async () => {
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
    })
    const passed = {
      ...ready,
      post_draft_gate: postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 1 }),
    }
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

    const { result } = renderHook(() => useChangeSets(changeSetProjectId))
    await waitFor(() => expect(result.current.detailState).toBe('ready'))
    await waitFor(() => expect(result.current.gate.status).toBe('missing'))

    await act(async () => {
      await result.current.runGate()
    })
    expect(runPostDraftGate).toHaveBeenCalledWith(ready.change_set_id, 1)
    expect(result.current.gate.status).toBe('passed')
    expect(result.current.gate.receipt?.review_revision).toBe(1)
  })

  it.each(['succeeded', 'failed'] as const)(
    'ignores a delayed Gate result after selecting another ChangeSet (%s)',
    async (outcome) => {
      const alternateId = 'changeset-fedcba9876543210fedcba9876543210'
      const current = changeSetFixture({
        status: 'ready',
        decision: 'accepted',
        reviewRevision: 1,
        gateRequired: true,
      })
      const alternate = {
        ...changeSetFixture({
          status: 'ready',
          decision: 'accepted',
          reviewRevision: 1,
          gateRequired: true,
        }),
        change_set_id: alternateId,
        title: '另一个待审候选',
      }
      const passed = {
        ...current,
        post_draft_gate: postDraftGateReceiptFixture({
          status: 'passed',
          reviewRevision: 1,
          changeSetId: current.change_set_id,
        }),
      }
      let resolveGate: ((value: typeof passed) => void) | undefined
      let rejectGate: ((reason?: unknown) => void) | undefined
      const delayedGate = new Promise<typeof passed>((resolve, reject) => {
        resolveGate = resolve
        rejectGate = reject
      })
      vi.mocked(listProjectChangeSets).mockResolvedValue([
        changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
        {
          ...changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1, gateRequired: true }),
          change_set_id: alternateId,
          title: alternate.title,
        },
      ])
      vi.mocked(getChangeSet).mockImplementation((changeSetId) => Promise.resolve(
        changeSetId === alternateId ? alternate : current,
      ))
      vi.mocked(getPostDraftGate).mockImplementation((changeSetId) => {
        const record = changeSetId === alternateId ? alternate : current
        return Promise.resolve({
          schema: 'zhimeng.post-draft-gate-response.v1',
          change_set_id: record.change_set_id,
          project_id: record.project_id,
          gate_required: true,
          review_revision: record.review_revision,
          receipt: null,
        })
      })
      vi.mocked(runPostDraftGate).mockReturnValue(delayedGate)

      const { result } = renderHook(() => useChangeSets(changeSetProjectId))
      await waitFor(() => expect(result.current.gate.status).toBe('missing'))
      let gateOperation: Promise<void> | undefined
      act(() => {
        gateOperation = result.current.runGate()
      })
      await waitFor(() => expect(result.current.action?.kind).toBe('gate'))

      act(() => result.current.select(alternateId))
      await waitFor(() => expect(result.current.detail?.change_set_id).toBe(alternateId))
      await waitFor(() => expect(result.current.gateResponse?.change_set_id).toBe(alternateId))

      await act(async () => {
        if (outcome === 'succeeded') resolveGate?.(passed)
        else rejectGate?.(new Error('旧 ChangeSet 的 Gate 失败'))
        await gateOperation
      })

      expect(result.current.detail?.change_set_id).toBe(alternateId)
      expect(result.current.gateResponse?.change_set_id).toBe(alternateId)
      expect(result.current.gate.status).toBe('missing')
      expect(result.current.gateError).toBeNull()
      expect(result.current.actionError).toBeNull()
      expect(result.current.action).toBeNull()
      await act(async () => {
        await result.current.apply()
      })
      expect(applyChangeSet).not.toHaveBeenCalled()
    },
  )

  it('does not expose an older receipt as current after review revision changes', async () => {
    const stale = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 2,
      gateRequired: true,
      postDraftGate: postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 1 }),
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

    const { result } = renderHook(() => useChangeSets(changeSetProjectId))
    await waitFor(() => expect(result.current.gate.status).toBe('stale'))
    expect(result.current.gate.receipt).toBeNull()
  })

  it('stops requiring Gate when a Hunk decision removes the final accepted prose body', async () => {
    const receipt = postDraftGateReceiptFixture({ status: 'passed', reviewRevision: 1 })
    const current = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
      gateRequired: true,
      postDraftGate: receipt,
    })
    const revised = changeSetFixture({
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
    vi.mocked(updateHunkDecisions).mockResolvedValue(revised)

    const { result } = renderHook(() => useChangeSets(changeSetProjectId))
    await waitFor(() => expect(result.current.gate.status).toBe('passed'))
    await act(async () => {
      await result.current.decide({
        hunk_id: current.changes[0].hunks[0].hunk_id,
        decision: 'rejected',
      })
    })

    expect(result.current.detail?.review_revision).toBe(2)
    expect(result.current.gate.status).toBe('not_required')
    expect(result.current.gate.receipt).toBeNull()
  })

  it('keeps list failures explicit and does not request mixed detail', async () => {
    vi.mocked(listProjectChangeSets).mockRejectedValue(new Error('queue unavailable'))
    const { result } = renderHook(() => useChangeSets(changeSetProjectId))

    await waitFor(() => expect(result.current.listState).toBe('error'))

    expect(result.current.listError).toBe('queue unavailable')
    expect(result.current.detail).toBeNull()
    expect(getChangeSet).not.toHaveBeenCalled()
    expect(rollbackChangeSet).not.toHaveBeenCalled()
  })

  it('does not let a stale project request replace the newly selected project', async () => {
    const secondProjectId = 'project-fedcba9876543210fedcba98'
    const secondDetail = {
      ...changeSetFixture(),
      project_id: secondProjectId,
      change_set_id: 'changeset-fedcba9876543210fedcba9876543210',
    }
    const second = {
      ...changeSetSummaryFixture(),
      project_id: secondDetail.project_id,
      change_set_id: secondDetail.change_set_id,
    }
    let resolveFirst: ((records: ReturnType<typeof changeSetSummaryFixture>[]) => void) | null = null
    const firstRequest = new Promise<ReturnType<typeof changeSetSummaryFixture>[]>((resolve) => {
      resolveFirst = resolve
    })
    vi.mocked(listProjectChangeSets).mockImplementation((projectId) => (
      projectId === changeSetProjectId ? firstRequest : Promise.resolve([second])
    ))
    vi.mocked(getChangeSet).mockResolvedValue(secondDetail)

    const { result, rerender } = renderHook(
      ({ projectId }) => useChangeSets(projectId),
      { initialProps: { projectId: changeSetProjectId } },
    )
    rerender({ projectId: secondProjectId })

    await waitFor(() => expect(result.current.detail?.project_id).toBe(secondProjectId))
    await act(async () => {
      resolveFirst?.([changeSetSummaryFixture()])
      await firstRequest
    })

    expect(result.current.records).toHaveLength(1)
    expect(result.current.records[0].project_id).toBe(secondProjectId)
    expect(result.current.detail?.change_set_id).toBe(second.change_set_id)
  })

  it('refreshes the server state after a review revision conflict', async () => {
    const pending = changeSetFixture()
    const conflicted = {
      ...changeSetFixture(),
      status: 'conflicted' as const,
      error: '项目文件已变化',
    }
    vi.mocked(getChangeSet)
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce(conflicted)
    vi.mocked(updateHunkDecisions).mockRejectedValue(
      new RuntimeApiError(409, {
        error: {
          code: 'change_set_conflict',
          message: '审阅版本已变化，请刷新',
        },
      }),
    )
    const { result } = renderHook(() => useChangeSets(changeSetProjectId))

    await waitFor(() => expect(result.current.detailState).toBe('ready'))
    const hunkId = result.current.detail?.changes[0].hunks[0].hunk_id
    await act(async () => {
      await result.current.decide({
        hunk_id: String(hunkId),
        decision: 'accepted',
      })
    })

    expect(result.current.actionConflict).toBe(true)
    expect(result.current.actionError).toBe('审阅版本已变化，请刷新')
    expect(result.current.detail?.status).toBe('conflicted')
    expect(getChangeSet).toHaveBeenCalledTimes(2)
  })

  it('refreshes the server state after a retryable writeback failure', async () => {
    const ready = changeSetFixture({
      status: 'ready',
      decision: 'accepted',
      reviewRevision: 1,
    })
    const failed = {
      ...changeSetFixture({
        status: 'failed',
        decision: 'accepted',
        reviewRevision: 1,
      }),
      error: '磁盘写回失败',
    }
    vi.mocked(getChangeSet)
      .mockResolvedValueOnce(ready)
      .mockResolvedValueOnce(failed)
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({ status: 'ready', decision: 'accepted', reviewRevision: 1 }),
    ])
    vi.mocked(applyChangeSet).mockRejectedValue(
      new RuntimeApiError(500, {
        error: {
          code: 'change_set_writeback_failed',
          message: '磁盘写回失败',
          retryable: true,
        },
      }),
    )
    const projectChanged = vi.fn()
    const { result } = renderHook(() => useChangeSets(changeSetProjectId, projectChanged))

    await waitFor(() => expect(result.current.detailState).toBe('ready'))
    await act(async () => {
      await result.current.apply()
    })

    expect(getChangeSet).toHaveBeenCalledTimes(2)
    expect(result.current.detail?.status).toBe('failed')
    expect(result.current.actionError).toBe('磁盘写回失败')
    expect(projectChanged).toHaveBeenCalledOnce()
  })

  it('reloads the project after a rollback response is lost but Runtime persisted the result', async () => {
    const applied = changeSetFixture({
      status: 'applied',
      decision: 'accepted',
      reviewRevision: 1,
    })
    const rolledBack = changeSetFixture({
      status: 'rolled_back',
      decision: 'accepted',
      reviewRevision: 1,
    })
    vi.mocked(listProjectChangeSets).mockResolvedValue([
      changeSetSummaryFixture({
        status: 'applied',
        decision: 'accepted',
        reviewRevision: 1,
      }),
    ])
    vi.mocked(getChangeSet)
      .mockResolvedValueOnce(applied)
      .mockResolvedValueOnce(rolledBack)
    vi.mocked(rollbackChangeSet).mockRejectedValue(
      new TypeError('network connection closed after writeback'),
    )
    const projectChanged = vi.fn()
    const { result } = renderHook(() => useChangeSets(changeSetProjectId, projectChanged))

    await waitFor(() => expect(result.current.detail?.status).toBe('applied'))
    await act(async () => {
      await result.current.rollback()
    })

    expect(rollbackChangeSet).toHaveBeenCalledWith(applied.change_set_id, 1)
    expect(getChangeSet).toHaveBeenCalledTimes(2)
    expect(result.current.detail?.status).toBe('rolled_back')
    expect(result.current.actionError).toBe('network connection closed after writeback')
    expect(projectChanged).toHaveBeenCalledOnce()
  })
})
