import { useCallback, useEffect, useRef, useState } from 'react'
import {
  applyChangeSet,
  getPostDraftGate,
  getChangeSet,
  listProjectChangeSets,
  rollbackChangeSet,
  runPostDraftGate,
  summarizeChangeSet,
  updateHunkDecisions,
  type ChangeSet,
  type PostDraftGateReceipt,
  type PostDraftGateResponse,
  type ChangeSetSummary,
  type HunkDecisionUpdate,
} from '../api/changeSets'
import { RuntimeApiError } from '../api/runtime'

export type ChangeSetLoadState = 'idle' | 'loading' | 'ready' | 'error'
export type ChangeSetAction =
  | { kind: 'decision'; targetId: string }
  | { kind: 'apply'; targetId: string }
  | { kind: 'rollback'; targetId: string }
  | { kind: 'gate'; targetId: string }
  | null

export type ChangeSetGateStatus =
  | 'not_required'
  | 'missing'
  | 'stale'
  | 'passed'
  | 'blocked'
  | 'failed'

export interface ChangeSetGateState {
  required: boolean
  status: ChangeSetGateStatus
  receipt: PostDraftGateReceipt | null
  reviewRevision: number | null
}

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
      conflict: error.status === 409 || [
        'change_set_conflict',
        'post_draft_gate_stale',
      ].includes(String(payload?.error?.code ?? '')),
      retryable: error.status >= 500 || payload?.error?.retryable === true,
      resultUnknown: error.status >= 500 || payload?.error?.retryable === true,
    }
  }
  return {
    message: error instanceof Error ? error.message : 'ChangeSet 请求失败',
    conflict: false,
    retryable: false,
    resultUnknown: true,
  }
}

function embeddedGateResponse(detail: ChangeSet): PostDraftGateResponse {
  return {
    schema: 'zhimeng.post-draft-gate-response.v1',
    change_set_id: detail.change_set_id,
    project_id: detail.project_id,
    gate_required: detail.gate_required,
    review_revision: detail.review_revision,
    receipt: detail.post_draft_gate,
  }
}

export function hasAcceptedPostDraftBody(
  detail: Pick<ChangeSet, 'changes'> | null,
): boolean {
  return detail?.changes.some((change) => (
    change.post_draft_gate_scope === 'prose_body'
    && change.operation !== 'delete'
    && change.hunks.some((hunk) => hunk.decision === 'accepted')
  )) === true
}

export function deriveChangeSetGateState(
  detail: Pick<
    ChangeSet,
    | 'change_set_id'
    | 'project_id'
    | 'gate_required'
    | 'review_revision'
    | 'changes'
    | 'post_draft_gate'
  > | null,
  response: PostDraftGateResponse | null = null,
): ChangeSetGateState {
  if (!detail) {
    return { required: false, status: 'not_required', receipt: null, reviewRevision: null }
  }
  const policyRequired = detail.gate_required || response?.gate_required === true
  const required = policyRequired && hasAcceptedPostDraftBody(detail)
  const reviewRevision = detail.review_revision
  const receipt = response === null ? detail.post_draft_gate : response.receipt
  if (!required) {
    return { required: false, status: 'not_required', receipt: null, reviewRevision }
  }
  if (
    response !== null
    && (
      response.change_set_id !== detail.change_set_id
      || response.project_id !== detail.project_id
      || response.review_revision !== reviewRevision
    )
  ) {
    return { required: true, status: 'stale', receipt: null, reviewRevision }
  }
  if (!receipt) {
    return { required: true, status: 'missing', receipt: null, reviewRevision }
  }
  if (
    receipt.change_set_id !== detail.change_set_id
    || receipt.project_id !== detail.project_id
    || receipt.review_revision !== reviewRevision
  ) {
    return { required: true, status: 'stale', receipt: null, reviewRevision }
  }
  if (receipt.execution_status === 'failed' || receipt.status === 'failed') {
    return { required: true, status: 'failed', receipt, reviewRevision }
  }
  return {
    required: true,
    status: receipt.status,
    receipt,
    reviewRevision,
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
  const [gateResponse, setGateResponse] = useState<PostDraftGateResponse | null>(null)
  const [gateState, setGateState] = useState<ChangeSetLoadState>('idle')
  const [gateError, setGateError] = useState<string | null>(null)
  const [action, setAction] = useState<ChangeSetAction>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionConflict, setActionConflict] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const currentProjectId = useRef(projectId)
  const currentSelectedId = useRef(selectedId)
  const actionEpoch = useRef(0)
  const appliedPreferredRequest = useRef<string | null>(null)

  if (currentProjectId.current !== projectId) {
    currentProjectId.current = projectId
    actionEpoch.current += 1
  }
  if (currentSelectedId.current !== selectedId) {
    currentSelectedId.current = selectedId
    actionEpoch.current += 1
  }

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    setRecords([])
    setSelectedId(null)
    setDetail(null)
    setDetailState('idle')
    setDetailError(null)
    setGateResponse(null)
    setGateState('idle')
    setGateError(null)
    setAction(null)
    setActionError(null)
    setActionConflict(false)
    appliedPreferredRequest.current = null
  }, [projectId])

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
      setGateResponse(null)
      setGateState('idle')
      setGateError(null)
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
          throw new Error('ChangeSet 队列包含其他项目的数据')
        }
        setRecords(nextRecords)
        setSelectedId((current) => {
          if (current && nextRecords.some((record) => record.change_set_id === current)) {
            return current
          }
          return preferredChangeSet(nextRecords, preferredChangeSetId)?.change_set_id ?? null
        })
        if (nextRecords.length === 0) {
          setDetail(null)
          setDetailState('idle')
          setDetailError(null)
          setGateResponse(null)
          setGateState('idle')
          setGateError(null)
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
      setGateResponse(null)
      setGateState('idle')
      setGateError(null)
      return
    }

    let cancelled = false
    setDetail(null)
    setDetailState('loading')
    setDetailError(null)
    setGateResponse(null)
    setGateState('loading')
    setGateError(null)
    setActionError(null)
    setActionConflict(false)
    void getChangeSet(selectedId)
      .then((nextDetail) => {
        if (cancelled) return
        if (nextDetail.project_id !== projectId) {
          setDetailError('ChangeSet 不属于当前项目')
          setDetailState('error')
          return
        }
        setDetail(nextDetail)
        setDetailState('ready')
        const embedded = embeddedGateResponse(nextDetail)
        setGateResponse(embedded)
        if (!deriveChangeSetGateState(nextDetail, embedded).required) {
          setGateState('ready')
          return
        }
        void getPostDraftGate(selectedId)
          .then((nextGate) => {
            if (cancelled) return
            if (
              nextGate.change_set_id !== nextDetail.change_set_id
              || nextGate.project_id !== nextDetail.project_id
            ) {
              throw new Error('Gate 响应身份与当前 ChangeSet 不一致')
            }
            setGateResponse(nextGate)
            setGateState('ready')
            setGateError(null)
          })
          .catch((error) => {
            if (cancelled) return
            setGateError(changeSetErrorInfo(error).message)
            setGateState('error')
          })
      })
      .catch((error) => {
        if (cancelled) return
        setDetailError(changeSetErrorInfo(error).message)
        setDetailState('error')
        setGateState('idle')
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
    setGateResponse((current) => {
      if (currentSelectedId.current !== updated.change_set_id) return current
      const embedded = embeddedGateResponse(updated)
      if (
        embedded.receipt === null
        && current?.change_set_id === updated.change_set_id
        && current.receipt !== null
        && current.receipt.review_revision !== updated.review_revision
      ) {
        return { ...embedded, receipt: current.receipt }
      }
      return embedded
    })
    if (currentSelectedId.current === updated.change_set_id) {
      setGateState('ready')
      setGateError(null)
    }
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
  ) => actionEpoch.current === epoch
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
        throw new Error('ChangeSet 响应身份与当前操作不一致')
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
    if (!detail || action || decisions.length === 0) return
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
        throw new Error('ChangeSet 响应身份与当前操作不一致')
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

  const runGate = useCallback(async () => {
    if (!detail || action || detail.status !== 'ready') return
    if (!deriveChangeSetGateState(detail, gateResponse).required) return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'gate', targetId: expectedChangeSetId })
    setActionError(null)
    setActionConflict(false)
    setGateError(null)
    setGateState('loading')
    try {
      const updated = await runPostDraftGate(
        expectedChangeSetId,
        detail.review_revision,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('Gate 响应身份与当前操作不一致')
      }
      storeUpdated(updated)
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      setGateState('error')
      setGateError(info.message)
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, gateResponse, responseMatches, storeUpdated, syncLatest])

  const apply = useCallback(async () => {
    if (!detail || action) return
    const gate = deriveChangeSetGateState(detail, gateResponse)
    if (gate.required && gate.status !== 'passed') return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'apply', targetId: expectedChangeSetId })
    setActionError(null)
    setActionConflict(false)
    try {
      const updated = await applyChangeSet(
        expectedChangeSetId,
        detail.review_revision,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('ChangeSet 响应身份与当前操作不一致')
      }
      storeUpdated(updated)
      onProjectChanged?.()
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (info.conflict || info.resultUnknown) onProjectChanged?.()
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, gateResponse, onProjectChanged, responseMatches, storeUpdated, syncLatest])

  const rollback = useCallback(async () => {
    if (!detail || action) return
    const expectedProjectId = detail.project_id
    const expectedChangeSetId = detail.change_set_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'rollback', targetId: expectedChangeSetId })
    setActionError(null)
    setActionConflict(false)
    try {
      const updated = await rollbackChangeSet(
        expectedChangeSetId,
        detail.review_revision,
      )
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (!responseMatches(updated, expectedProjectId, expectedChangeSetId)) {
        throw new Error('ChangeSet 响应身份与当前操作不一致')
      }
      storeUpdated(updated)
      onProjectChanged?.()
    } catch (error) {
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      const info = changeSetErrorInfo(error)
      if (info.conflict || info.retryable || info.resultUnknown) {
        await syncLatest(expectedChangeSetId, expectedProjectId, epoch)
      }
      if (!actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) return
      if (info.conflict || info.resultUnknown) onProjectChanged?.()
      if (currentSelectedId.current === expectedChangeSetId) {
        setActionError(info.message)
        setActionConflict(info.conflict)
      }
    } finally {
      if (actionIsCurrent(epoch, expectedProjectId, expectedChangeSetId)) setAction(null)
    }
  }, [action, actionIsCurrent, detail, onProjectChanged, responseMatches, storeUpdated, syncLatest])

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
    gateResponse,
    gateState,
    gateError,
    gate: deriveChangeSetGateState(detail, gateResponse),
    select,
    decide,
    decideMany,
    runGate,
    apply,
    rollback,
    refresh,
  }
}

export type ChangeSetReviewState = ReturnType<typeof useChangeSets>
