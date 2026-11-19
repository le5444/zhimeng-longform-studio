import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyChangeSet,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  updateHunkDecisions,
} from '../api/changeSets'
import { RuntimeApiError } from '../api/runtime'
import {
  changeSetFixture,
  changeSetProjectId,
  changeSetSummaryFixture,
} from '../test/changeSetFixture'
import { useChangeSets } from './useChangeSets'

vi.mock('../api/changeSets', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/changeSets')>()
  return {
    ...actual,
    applyChangeSet: vi.fn(),
    getChangeSet: vi.fn(),
    listProjectChangeSets: vi.fn(),
    rollbackChangeSet: vi.fn(),
    updateHunkDecisions: vi.fn(),
  }
})

describe('useChangeSets', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    const pending = changeSetFixture()
    vi.mocked(listProjectChangeSets).mockResolvedValue([changeSetSummaryFixture()])
    vi.mocked(getChangeSet).mockResolvedValue(pending)
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

  it.each(['apply', 'rollback'] as const)('%s refreshes completed file changes after selecting another record', async (kind) => {
    const original = changeSetFixture({ status: kind === 'apply' ? 'ready' : 'applied', decision: 'accepted' })
    const updated = { ...original, status: kind === 'apply' ? 'applied' as const : 'rolled_back' as const }
    const other = { ...original, change_set_id: 'changeset-other', title: 'Other notes' }
    let persisted = original
    let finish!: (value: typeof updated) => void
    const pending = new Promise<typeof updated>((resolve) => { finish = resolve })
    vi.mocked(kind === 'apply' ? applyChangeSet : rollbackChangeSet).mockReturnValue(pending)
    vi.mocked(listProjectChangeSets).mockImplementation(async () => [
      { ...changeSetSummaryFixture(), change_set_id: original.change_set_id, status: persisted.status },
      { ...changeSetSummaryFixture(), change_set_id: other.change_set_id, status: original.status },
    ])
    vi.mocked(getChangeSet).mockImplementation(async (id) => id === other.change_set_id ? other : persisted)
    const changed = vi.fn()
    const { result } = renderHook(() => useChangeSets(changeSetProjectId, changed))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(original.change_set_id))
    act(() => { void result.current[kind]() })
    act(() => result.current.select(other.change_set_id))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(other.change_set_id))
    persisted = updated
    await act(async () => finish(updated))
    await waitFor(() => expect(result.current.records.find(r => r.change_set_id === original.change_set_id)?.status).toBe(updated.status))
    expect(result.current.selectedId).toBe(other.change_set_id)
    expect(result.current.detail?.change_set_id).toBe(other.change_set_id)
    expect(changed).toHaveBeenCalledOnce()
  })

  it.each(['apply', 'rollback'] as const)('%s remains in progress when leaving and returning to the same record', async (kind) => {
    const original = changeSetFixture({ status: kind === 'apply' ? 'ready' : 'applied', decision: 'accepted' })
    const other = { ...original, change_set_id: 'changeset-other' }
    const updated = { ...original, status: kind === 'apply' ? 'applied' as const : 'rolled_back' as const }
    let persisted = original
    let finish!: (value: typeof updated) => void
    const pending = new Promise<typeof updated>((resolve) => { finish = resolve })
    const endpoint = vi.mocked(kind === 'apply' ? applyChangeSet : rollbackChangeSet)
    endpoint.mockReturnValue(pending)
    vi.mocked(listProjectChangeSets).mockImplementation(async () => [
      { ...changeSetSummaryFixture(), status: persisted.status },
      { ...changeSetSummaryFixture(), change_set_id: other.change_set_id, status: original.status },
    ])
    vi.mocked(getChangeSet).mockImplementation(async (id) => id === other.change_set_id ? other : persisted)
    const { result } = renderHook(() => useChangeSets(changeSetProjectId))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(original.change_set_id))
    act(() => { void result.current[kind]() })
    act(() => result.current.select(other.change_set_id))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(other.change_set_id))
    act(() => result.current.select(original.change_set_id))
    await waitFor(() => expect(result.current.detail?.change_set_id).toBe(original.change_set_id))
    expect(result.current.action?.kind).toBe(kind)
    act(() => { void result.current[kind]() })
    expect(endpoint).toHaveBeenCalledOnce()
    persisted = updated
    await act(async () => finish(updated))
    await waitFor(() => expect(result.current.detail?.status).toBe(updated.status))
    expect(result.current.action).toBeNull()
  })

  it.each(['apply', 'rollback'] as const)('%s rechecks disk after a lost response while switching away and back', async (kind) => {
    const original = changeSetFixture({ status: kind === 'apply' ? 'ready' : 'applied', decision: 'accepted' })
    const updated = { ...original, status: kind === 'apply' ? 'applied' as const : 'rolled_back' as const }
    let persisted = original
    let reject!: (error: Error) => void
    const pending = new Promise<typeof updated>((_, no) => { reject = no })
    vi.mocked(kind === 'apply' ? applyChangeSet : rollbackChangeSet).mockReturnValue(pending)
    vi.mocked(listProjectChangeSets).mockImplementation(async (pid) => pid === changeSetProjectId
      ? [{ ...changeSetSummaryFixture(), status: persisted.status }] : [])
    vi.mocked(getChangeSet).mockImplementation(async () => persisted)
    const changed = vi.fn()
    const { result, rerender } = renderHook(({ pid }) => useChangeSets(pid, changed), { initialProps: { pid: changeSetProjectId } })
    await waitFor(() => expect(result.current.detail?.status).toBe(original.status))
    act(() => { void result.current[kind]() })
    rerender({ pid: 'another-project' })
    await waitFor(() => expect(result.current.records).toEqual([]))
    rerender({ pid: changeSetProjectId })
    await waitFor(() => expect(result.current.detail?.status).toBe(original.status))
    persisted = updated
    await act(async () => reject(new Error('Connection lost after saving')))
    await waitFor(() => expect(result.current.detail?.status).toBe(updated.status))
    expect(changed).toHaveBeenCalledOnce()
    expect(result.current.action).toBeNull()
    expect(result.current.actionError).toBe('Connection lost after saving')
  })

  it.each(['apply', 'rollback'] as const)('%s does not refresh an unrelated project or an unmounted workspace', async (kind) => {
    const original = changeSetFixture({ status: kind === 'apply' ? 'ready' : 'applied', decision: 'accepted' })
    const updated = { ...original, status: kind === 'apply' ? 'applied' as const : 'rolled_back' as const }
    let finish!: (value: typeof updated) => void
    const pending = new Promise<typeof updated>((resolve) => { finish = resolve })
    vi.mocked(kind === 'apply' ? applyChangeSet : rollbackChangeSet).mockReturnValue(pending)
    vi.mocked(getChangeSet).mockResolvedValue(original)
    vi.mocked(listProjectChangeSets).mockImplementation(async pid => pid === changeSetProjectId ? [changeSetSummaryFixture()] : [])
    const changed = vi.fn()
    const { result, rerender, unmount } = renderHook(({ pid }) => useChangeSets(pid, changed), { initialProps: { pid: changeSetProjectId } })
    await waitFor(() => expect(result.current.detail).toEqual(original))
    act(() => { void result.current[kind]() })
    rerender({ pid: 'unrelated-project' })
    await waitFor(() => expect(result.current.records).toEqual([]))
    await act(async () => finish(updated))
    expect(changed).not.toHaveBeenCalled()
    expect(result.current.records).toEqual([])
    expect(result.current.detail).toBeNull()
    rerender({ pid: changeSetProjectId })
    await waitFor(() => expect(result.current.detail).toEqual(original))
    vi.mocked(kind === 'apply' ? applyChangeSet : rollbackChangeSet).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    act(() => { void result.current[kind]() })
    unmount()
    await act(async () => finish(updated))
    expect(changed).not.toHaveBeenCalled()
  })
})
