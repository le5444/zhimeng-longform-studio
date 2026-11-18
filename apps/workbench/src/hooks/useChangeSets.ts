import { useCallback, useEffect, useRef, useState } from 'react'
import {
  applyChangeSet,
  applyChangeSetDirect,
  discardChangeSet,
  editChangeSetHunk,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  summarizeChangeSet,
  updateHunkDecisions,
  type ChangeSet,
  type ChangeSetSummary,
  type HunkDecisionUpdate,
} from '../api/changeSets'
import { RuntimeApiError } from '../api/runtime'

export type ChangeSetLoadState = 'idle' | 'loading' | 'ready' | 'error'
export type ChangeSetAction =
  | { kind: 'decision'; targetId: string }
  | { kind: 'edit'; targetId: string }
  | { kind: 'apply'; targetId: string }
  | { kind: 'rollback'; targetId: string }
  | { kind: 'discard'; targetId: string }
  | null

interface ChangeSetErrorInfo {
  message: string
  conflict: boolean
  retryable: boolean
  resultUnknown: boolean
}

function changeSetErrorInfo(error: unknown): ChangeSetErrorInfo {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { code?: unknown; message?: unknown; retryable?: unknown }
      detail?: unknown
    } | null
    const message = typeof payload?.error?.message === 'string'
      ? payload.error.message
      : typeof payload?.detail === 'string'
        ? payload.detail
        : error.message
    return {
      message,
      conflict: error.status === 409 || payload?.error?.code === 'change_set_conflict',
      retryable: error.status >= 500 || payload?.error?.retryable === true,
      resultUnknown: error.status >= 500 || payload?.error?.retryable === true,
    }
  }
  return {
    message: error instanceof Error ? error.message : '修改记录请求没有成功',
    conflict: false,
    retryable: false,
    resultUnknown: true,
  }
}

function preferredChangeSet(
  records: ChangeSetSummary[],
  requestedId: string | null = null,
): ChangeSetSummary | null {
  return records.find((record) => record.change_set_id === requestedId)
    ?? records.find((record) => record.status === 'reviewing')
    ?? records.find((record) => record.status === 'ready')
    ?? records.find((record) => record.status === 'conflicted')
    ?? records.find((record) => record.status === 'failed')
    ?? records.find((record) => record.status === 'applying')
    ?? records.find((record) => record.status === 'rolling_back')
    ?? records.find((record) => record.status === 'applied')
    ?? records[0]
    ?? null
}

export function useChangeSets(
  projectId: string | null,
  onProjectChanged?: () => void,
  preferredChangeSetId: string | null = null,
  preferredChangeSetRequestKey = 0,
) {
  const [records, setRecords] = useState<ChangeSetSummary[]>([])
  const [listState, setListState] = useState<ChangeSetLoadState>('idle')
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ChangeSet | null>(null)
  const [detailState, setDetailState] = useState<ChangeSetLoadState>('idle')
  const [detailError, setDetailError] = useState<string | null>(null)
  const [reviewAction, setAction] = useState<ChangeSetAction>(null)
  const [fileActions, setFileActions] = useState(new Map<string, { action: ChangeSetAction; error: string | null }>())
  const fileAction = selectedId ? fileActions.get(selectedId) : null
  const action = fileAction?.action ?? reviewAction
  const fileRequests = useRef(new Set<string>())
  const mounted = useRef(true)
  const projectChanged = useRef(onProjectChanged)
  projectChanged.current = onProjectChanged
  const [reviewActionError, setActionError] = useState<string | null>(null)
  const actionError = fileAction?.error ?? reviewActionError
  const [actionConflict, setActionConflict] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const currentProjectId = useRef(projectId)
  const currentSelectedId = useRef(selectedId)
  const actionEpoch = useRef(0)
  const appliedPreferredRequest = useRef<string | null>(null)

  if (currentProjectId.current !== projectId) {
    currentProjectId.current = projectId
    actionEpoch.current += 1
    setRecords([])
    setSelectedId(null)
    setDetail(null)
    setDetailState('idle')
    setDetailError(null)
    setAction(null)
    setActionError(null)
    setActionConflict(false)
    appliedPreferredRequest.current = null
  }
  if (currentSelectedId.current !== selectedId) {
    currentSelectedId.current = selectedId
    actionEpoch.current += 1
  }

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (preferredChangeSetId === null) appliedPreferredRequest.current = null
  }, [preferredChangeSetId])

  useEffect(() => {
    setAction(null)
  }, [selectedId])

  useEffect(() => {
    if (!projectId) {
      setRecords([])
      setListState('idle')
      setListError(null)
      setSelectedId(null)
      setDetail(null)
      setDetailState('idle')
      setDetailError(null)
      setActionError(null)
      setActionConflict(false)
      return
    }

    let cancelled = false
    setListState('loading')
    setListError(null)
    void listProjectChangeSets(projectId)
      .then((nextRecords) => {
        if (cancelled) return
        if (nextRecords.some((record) => record.project_id !== projectId)) {
          throw new Error('修改记录里混进了别的项目的数据')
        }
        setRecords(nextRecords)
        setSelectedId((current) => {
          // A refreshed list may lag behind a chat card. Keep its target and let
          // the detail request report a missing record instead of choosing another.
          if (current) return current
          return preferredChangeSet(nextRecords, preferredChangeSetId)?.change_set_id ?? null
        })
        if (nextRecords.length === 0 && !currentSelectedId.current) {
          setDetail(null)
          setDetailState('idle')
          setDetailError(null)
        }
        setListState('ready')
      })
      .catch((error) => {
        if (cancelled) return
        setListError(changeSetErrorInfo(error).message)
        setListState('error')
      })
    return () => {
      cancelled = true
    }
  }, [preferredChangeSetId, preferredChangeSetRequestKey, projectId, refreshKey])

  useEffect(() => {
    if (!projectId || !preferredChangeSetId) return
    const requestToken = `${projectId}:${preferredChangeSetRequestKey}:${preferredChangeSetId}`
    if (appliedPreferredRequest.current === requestToken) return
    if (!records.some((record) => (
      record.project_id === projectId && record.change_set_id === preferredChangeSetId
    ))) return
    appliedPreferredRequest.current = requestToken
    if (currentSelectedId.current !== preferredChangeSetId) {
      currentSelectedId.current = preferredChangeSetId
      actionEpoch.current += 1
    }
    setAction(null)
    setActionError(null)
    setActionConflict(false)
    setSelectedId(preferredChangeSetId)
  }, [preferredChangeSetId, preferredChangeSetRequestKey, projectId, records])

  useEffect(() => {
    if (!projectId || !selectedId) {
      setDetail(null)
      setDetailState('idle')
      setDetailError(null)
      return
    }

    let cancelled = false
    setDetail(null)
    setDetailState('loading')
    setDetailError(null)
    setActionError(null)
    setActionConflict(false)
    void getChangeSet(selectedId)
      .then((nextDetail) => {
        if (cancelled) return
        if (nextDetail.project_id !== projectId) {
          setDetailError('这条修改不属于现在这个项目')
          setDetailState('error')
          return
        }
        if (nextDetail.change_set_id !== selectedId) {
          setDetailError('修改详情与当前选中的记录不一致，请重新加载。')
          setDetailState('error')
          return
        }
        setDetail(nextDetail)
        setDetailState('ready')
      })
      .catch((error) => {
        if (cancelled) return
        setDetailError(changeSetErrorInfo(error).message)
        setDetailState('error')
      })
    return () => {
      cancelled = true
    }
  }, [projectId, refreshKey, selectedId])

  const select = useCallback((changeSetId: string) => {
    if (currentSelectedId.current !== changeSetId) {
      currentSelectedId.current = changeSetId
      actionEpoch.current += 1
    }
    setAction(null)
    setActionError(null)
    setActionConflict(false)
    setSelectedId(changeSetId)
  }, [])

  const storeUpdated = useCallback((updated: ChangeSet) => {
    const summary = summarizeChangeSet(updated)
    setDetail((current) => current?.change_set_id === updated.change_set_id ? updated : current)
    setRecords((current) => {
      const found = current.some((record) => record.change_set_id === updated.change_set_id)
      return found
        ? current.map((record) => record.change_set_id === updated.change_set_id ? summary : record)
        : [summary, ...current]
    })
  }, [])

  const responseMatches = useCallback((
    updated: ChangeSet,
    expectedProjectId: string,
    expectedChangeSetId: string,
  ) => updated.project_id === expectedProjectId
    && updated.change_set_id === expectedChangeSetId, [])

  const actionIsCurrent = useCallback((
    epoch: number,
    expectedProjectId: string,
    expectedChangeSetId: string,
  ) => mounted.current && actionEpoch.current === epoch
    && currentProjectId.current === expectedProjectId
    && currentSelectedId.current === expectedChangeSetId, [])

  const syncLatest = useCallback(async (
    changeSetId: string,
    expectedProjectId: string,
    epoch: number,
  ) => {
    try {
      const latest = await getChangeSet(changeSetId)
      if (
        actionIsCurrent(epoch, expectedProjectId, changeSetId)
        && responseMatches(latest, expectedProjectId, changeSetId)
      ) {
        storeUpdated(latest)
      }
    } catch {
      // The original action error remains actionable even if this read fails.
    }
  }, [actionIsCurrent, responseMatches, storeUpdated])

  const finishFileAction = useCallback((expectedProjectId: string, changeSetId: string, epoch: number, error: string | null) => {
    fileRequests.current.delete(changeSetId)
    if (!mounted.current) return
    setFileActions((value) => {
      const next = new Map(value)
      if (error) next.set(changeSetId, { action: null, error })
      else next.delete(changeSetId)
      return next
    })
    if (currentProjectId.current !== expectedProjectId) return
    // 切换页面只改变查看对象，已经发出的写入仍然需要同步结果。
    if (!actionIsCurrent(epoch, expectedProjectId, changeSetId)) refresh()
    projectChanged.current?.()
  }, [actionIsCurrent, refresh])

  const decide = useCallback(async (decision: HunkDecisionUpdate) => {
    if (!detail || action) return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'decision', targetId: decision.hunk_id })
    setActionError(null)
    setActionConflict(false)
    try {
      const updated = await updateHunkDecisions(
        expectedChangeSetId,
        detail.review_revision,
        [decision],
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('修改记录的返回结果对不上这次操作')
      }
      storeUpdated(updated)
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, responseMatches, storeUpdated, syncLatest])

  const decideMany = useCallback(async (decisions: HunkDecisionUpdate[]) => {
    if (!detail || action || decisions.length === 0) return null
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'decision', targetId: decisions[0]?.hunk_id ?? expectedChangeSetId })
    setActionError(null)
    setActionConflict(false)
    try {
      const updated = await updateHunkDecisions(
        expectedChangeSetId,
        detail.review_revision,
        decisions,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('修改记录的返回结果对不上这次操作')
      }
      storeUpdated(updated)
      return updated
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
      return null
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, responseMatches, storeUpdated, syncLatest])

  const editHunk = useCallback(async (hunkId: string, afterText: string) => {
    if (!detail || action) return null
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'edit', targetId: hunkId })
    setActionError(null)
    setActionConflict(false)
    try {
      const updated = await editChangeSetHunk(
        expectedChangeSetId,
        hunkId,
        detail.review_revision,
        afterText,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return null
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('返回的修改和现在这个项目对不上')
      }
      storeUpdated(updated)
      return updated
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return null
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return null
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
      return null
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, responseMatches, storeUpdated, syncLatest])

  const apply = useCallback(async (acceptPending = false) => {
    if (!detail || action || fileRequests.current.has(detail.change_set_id)) return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    fileRequests.current.add(expectedChangeSetId)
    setFileActions((value) => new Map(value).set(expectedChangeSetId, { action: { kind: 'apply', targetId: expectedChangeSetId }, error: null }))
    setActionError(null)
    setActionConflict(false)
    let fileError: string | null = null
    try {
      // 接受和写入由同一次后端请求完成，浏览器离开后不再依赖下一次请求。
      const save = acceptPending ? applyChangeSetDirect : applyChangeSet
      const updated = await save(
        expectedChangeSetId,
        detail.review_revision,
      )
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('修改记录的返回结果对不上这次操作')
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      storeUpdated(updated)
    } catch (error) {
      fileError = changeSetErrorInfo(error).message
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      finishFileAction(expectedProjectId, expectedChangeSetId, epoch, fileError)
    }
  }, [action, actionIsCurrent, detail, finishFileAction, responseMatches, storeUpdated, syncLatest])

  const rollback = useCallback(async () => {
    if (!detail || action || fileRequests.current.has(detail.change_set_id)) return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    fileRequests.current.add(expectedChangeSetId)
    setFileActions((value) => new Map(value).set(expectedChangeSetId, { action: { kind: 'rollback', targetId: expectedChangeSetId }, error: null }))
    setActionError(null)
    setActionConflict(false)
    let fileError: string | null = null
    try {
      const updated = await rollbackChangeSet(
        expectedChangeSetId,
        detail.review_revision,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('修改记录的返回结果对不上这次操作')
      }
      storeUpdated(updated)
    } catch (error) {
      fileError = changeSetErrorInfo(error).message
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      finishFileAction(expectedProjectId, expectedChangeSetId, epoch, fileError)
    }
  }, [action, actionIsCurrent, detail, finishFileAction, responseMatches, storeUpdated, syncLatest])

  // 批量作废：主要给「清掉过期提案」用。书已经往前走了、永远应用不了的
  // 旧候选不该一直挂在作者眼前，一键全部丢弃。
  // 不走 actionIsCurrent 的选中守卫：批量目标通常不是当前选中的那条，
  // 只用 epoch + 项目判断，中途切书就立刻停手。
  const discardByIds = useCallback(async (
    items: Array<{ changeSetId: string; expectedReviewRevision: number }>,
  ) => {
    if (action || items.length === 0) return
    const projectIdOfFirst = records.find(
      (record) => record.change_set_id === items[0].changeSetId,
    )?.project_id ?? null
    if (!projectIdOfFirst) return
    const myEpoch = ++actionEpoch.current
    // 批量期间切换了书 / 换了选中项，就立刻停手。
    const batchAlive = () =>
      actionEpoch.current === myEpoch
      && currentProjectId.current === projectIdOfFirst
    setAction({ kind: 'discard', targetId: items[0].changeSetId })
    setActionError(null)
    setActionConflict(false)
    let processed = 0
    // 各目标互相独立：某一条撞上 409（比如它的 revision 被并发操作顶过）
    // 不该让剩下的全部放弃，逐条尝试，最后汇总第一处错误。
    let firstError: ReturnType<typeof changeSetErrorInfo> | null = null
    for (const item of items) {
      if (!batchAlive()) return
      try {
        const updated = await discardChangeSet(item.changeSetId, item.expectedReviewRevision)
        if (!batchAlive()) return
        if (!responseMatches(updated, projectIdOfFirst, item.changeSetId)) {
          throw new Error('修改记录的返回结果对不上这次操作')
        }
        storeUpdated(updated)
        processed += 1
      } catch (error) {
        if (!batchAlive()) return
        const info = changeSetErrorInfo(error)
        if (!firstError) firstError = info
      }
    }
    if (firstError) {
      if (firstError.conflict || firstError.retryable || firstError.resultUnknown) refresh()
      setActionError(
        processed > 0
          ? `已清掉 ${processed} 条，还有 ${items.length - processed} 条没处理：${firstError.message}`
          : firstError.message,
      )
    } else {
      onProjectChanged?.()
    }
    setAction(null)
  }, [action, onProjectChanged, records, refresh, responseMatches, storeUpdated])

  return {
    records,
    listState,
    listError,
    selectedId,
    detail,
    detailState,
    detailError,
    action,
    actionError,
    actionConflict,
    select,
    decide,
    decideMany,
    editHunk,
    apply,
    rollback,
    discardByIds,
    refresh,
  }
}
