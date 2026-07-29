import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelSourceImport,
  createLocalSourceImport,
  createSourceImport,
  getSourceImportExecutionReceipt,
  inspectLocalSource,
  listSourceImports,
  retrySourceImport,
  searchTomatoNovels,
  type LocalFileSourceCandidate,
  type SourceImportExecutionReceiptRecord,
  type SourceImportJob,
  type SourceImportStatus,
  type TomatoSearchCandidate,
  type TomatoSearchResolution,
} from '../api/sourceImports'
import {
  getKnowledgeSourceFormalization,
  getProjectKnowledgeSourceFormalization,
  type KnowledgeFormalizationRecord,
} from '../api/knowledge'
import { RuntimeApiError } from '../api/runtime'

export type SourceImportLoadState = 'idle' | 'loading' | 'ready' | 'error'
export type SourceImportSearchState =
  | 'idle'
  | 'searching'
  | 'selection_required'
  | 'not_found'
  | 'creating'
  | 'created'
  | 'error'

export type LocalSourceImportState =
  | 'idle'
  | 'inspecting'
  | 'creating'
  | 'created'
  | 'error'

export type SourceImportAction =
  | { kind: 'cancel'; targetId: string }
  | { kind: 'retry'; targetId: string }
  | null

export const ACTIVE_SOURCE_IMPORT_STATUSES = new Set<SourceImportStatus>([
  'queued',
  'downloading',
  'source_ready',
  'drafting',
  'formalizing',
  'cancel_requested',
])

const POLL_INTERVAL_MS = 1_500
const RECEIPT_SOURCE_IMPORT_STATUSES = new Set<SourceImportStatus>([
  'awaiting_review',
  'formalizing',
  'completed',
])

function sourceImportErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  return error instanceof Error ? error.message : '资料导入请求失败'
}

function orderJobs(records: SourceImportJob[]): SourceImportJob[] {
  return [...records].sort((left, right) => {
    const leftTime = Date.parse(left.updated_at)
    const rightTime = Date.parse(right.updated_at)
    return (Number.isNaN(rightTime) ? 0 : rightTime)
      - (Number.isNaN(leftTime) ? 0 : leftTime)
  })
}

export function useSourceImports(enabled = true, projectId: string | null = null) {
  const scopedProjectId = projectId?.trim() || null
  const [query, setQueryValue] = useState('')
  const [searchState, setSearchState] = useState<SourceImportSearchState>('idle')
  const [searchResolution, setSearchResolution] = useState<TomatoSearchResolution | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [candidates, setCandidates] = useState<TomatoSearchCandidate[]>([])
  const [searchError, setSearchError] = useState<string | null>(null)
  const [localPath, setLocalPathValue] = useState('')
  const [localState, setLocalState] = useState<LocalSourceImportState>('idle')
  const [localCandidate, setLocalCandidate] = useState<LocalFileSourceCandidate | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [jobs, setJobs] = useState<SourceImportJob[]>([])
  const [listState, setListState] = useState<SourceImportLoadState>('idle')
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [action, setAction] = useState<SourceImportAction>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [evidenceState, setEvidenceState] = useState<SourceImportLoadState>('idle')
  const [executionReceipt, setExecutionReceipt] = useState<SourceImportExecutionReceiptRecord | null>(null)
  const [formalization, setFormalization] = useState<KnowledgeFormalizationRecord | null>(null)
  const [evidenceError, setEvidenceError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const searchEpoch = useRef(0)
  const localEpoch = useRef(0)
  const actionEpoch = useRef(0)
  const jobMutationEpoch = useRef(0)
  const evidenceEpoch = useRef(0)

  const selected = useMemo(
    () => jobs.find((job) => job.import_id === selectedId) ?? null,
    [jobs, selectedId],
  )

  const loadFormalization = useCallback((targetId: string) => (
    scopedProjectId
      ? getProjectKnowledgeSourceFormalization(scopedProjectId, targetId)
      : getKnowledgeSourceFormalization(targetId)
  ), [scopedProjectId])

  const hasActiveJobs = jobs.some((job) => (
    ACTIVE_SOURCE_IMPORT_STATUSES.has(job.status)
  ))

  const refresh = useCallback(() => {
    setRefreshKey((value) => value + 1)
  }, [])

  const setQuery = useCallback((value: string) => {
    searchEpoch.current += 1
    setQueryValue(value)
    setSearchState('idle')
    setSearchResolution(null)
    setSearchQuery('')
    setCandidates([])
    setSearchError(null)
    setActionError(null)
  }, [])

  const setLocalPath = useCallback((value: string) => {
    localEpoch.current += 1
    setLocalPathValue(value)
    setLocalState('idle')
    setLocalCandidate(null)
    setLocalError(null)
    setActionError(null)
  }, [])

  const storeJob = useCallback((job: SourceImportJob) => {
    jobMutationEpoch.current += 1
    setJobs((current) => orderJobs([
      job,
      ...current.filter((item) => item.import_id !== job.import_id),
    ]))
    setSelectedId(job.import_id)
  }, [])

  useEffect(() => {
    if (!enabled) {
      searchEpoch.current += 1
      localEpoch.current += 1
      actionEpoch.current += 1
      setJobs([])
      setListState('idle')
      setListError(null)
      setSelectedId(null)
      setEvidenceState('idle')
      setExecutionReceipt(null)
      setFormalization(null)
      setEvidenceError(null)
      return
    }

    let cancelled = false
    const mutationAtStart = jobMutationEpoch.current
    setListState('loading')
    setListError(null)
    void listSourceImports()
      .then((records) => {
        if (cancelled) return
        const ordered = orderJobs(records)
        if (jobMutationEpoch.current !== mutationAtStart) {
          setJobs((current) => {
            const localIds = new Set(current.map((job) => job.import_id))
            return orderJobs([
              ...current,
              ...ordered.filter((job) => !localIds.has(job.import_id)),
            ])
          })
          setSelectedId((current) => current ?? ordered[0]?.import_id ?? null)
        } else {
          setJobs(ordered)
          setSelectedId((current) => (
            current && ordered.some((job) => job.import_id === current)
              ? current
              : ordered[0]?.import_id ?? null
          ))
        }
        setListState('ready')
      })
      .catch((error) => {
        if (cancelled) return
        setListError(sourceImportErrorMessage(error))
        setListState('error')
      })
    return () => {
      cancelled = true
    }
  }, [enabled, refreshKey])

  useEffect(() => {
    if (!enabled || !hasActiveJobs || listState !== 'ready') return
    const timer = window.setTimeout(refresh, POLL_INTERVAL_MS)
    return () => window.clearTimeout(timer)
  }, [enabled, hasActiveJobs, listState, refresh, refreshKey])

  useEffect(() => {
    const targetId = selected?.import_id ?? null
    const targetStatus = selected?.status ?? null
    const epoch = ++evidenceEpoch.current
    if (
      !enabled
      || !targetId
      || !targetStatus
      || !RECEIPT_SOURCE_IMPORT_STATUSES.has(targetStatus)
    ) {
      setEvidenceState('idle')
      setExecutionReceipt(null)
      setFormalization(null)
      setEvidenceError(null)
      return
    }

    setEvidenceState('loading')
    setExecutionReceipt(null)
    setFormalization(null)
    setEvidenceError(null)
    void Promise.allSettled([
      getSourceImportExecutionReceipt(targetId),
      loadFormalization(targetId),
    ]).then(([sourceResult, formalizationResult]) => {
      if (evidenceEpoch.current !== epoch) return
      const errors: string[] = []
      if (sourceResult.status === 'fulfilled') {
        if (sourceResult.value.source_import_id === targetId) {
          setExecutionReceipt(sourceResult.value)
        } else {
          errors.push('来源执行收据不属于当前导入任务')
        }
      } else {
        errors.push(`来源执行收据：${sourceImportErrorMessage(sourceResult.reason)}`)
      }
      if (formalizationResult.status === 'fulfilled') {
        if (formalizationResult.value.source_import_id === targetId) {
          setFormalization(formalizationResult.value)
        } else {
          errors.push('知识正式化收据不属于当前导入任务')
        }
      } else {
        errors.push(`知识正式化收据：${sourceImportErrorMessage(formalizationResult.reason)}`)
      }
      setEvidenceError(errors.length > 0 ? errors.join('；') : null)
      setEvidenceState(errors.length > 0 ? 'error' : 'ready')
    })
    return () => {
      if (evidenceEpoch.current === epoch) evidenceEpoch.current += 1
    }
  }, [enabled, loadFormalization, refreshKey, scopedProjectId, selected?.import_id, selected?.status])

  const createFromCandidate = useCallback(async (
    resolvedQuery: string,
    candidate: TomatoSearchCandidate,
    epoch: number,
  ) => {
    setSearchState('creating')
    try {
      const created = await createSourceImport(resolvedQuery, candidate)
      if (searchEpoch.current !== epoch) return
      storeJob(created)
      setSearchState('created')
      setCandidates([])
      setSearchError(null)
    } catch (error) {
      if (searchEpoch.current !== epoch) return
      setSearchError(sourceImportErrorMessage(error))
      setSearchState('error')
    }
  }, [storeJob])

  const search = useCallback(async () => {
    const trimmed = query.trim()
    if (!trimmed || searchState === 'searching' || searchState === 'creating') return
    const epoch = ++searchEpoch.current
    setSearchState('searching')
    setSearchResolution(null)
    setSearchQuery(trimmed)
    setCandidates([])
    setSearchError(null)
    setActionError(null)
    try {
      const result = await searchTomatoNovels(trimmed)
      if (searchEpoch.current !== epoch) return
      setSearchResolution(result.resolution)
      setSearchQuery(result.query)
      setCandidates(result.candidates)
      if (result.resolution === 'auto_selected' && result.selected_candidate) {
        await createFromCandidate(result.query, result.selected_candidate, epoch)
      } else if (result.resolution === 'selection_required') {
        setSearchState('selection_required')
      } else {
        setSearchState('not_found')
      }
    } catch (error) {
      if (searchEpoch.current !== epoch) return
      setSearchError(sourceImportErrorMessage(error))
      setSearchState('error')
    }
  }, [createFromCandidate, query, searchState])

  const chooseCandidate = useCallback(async (candidate: TomatoSearchCandidate) => {
    if (!searchQuery || searchState === 'creating') return
    const epoch = ++searchEpoch.current
    setSearchError(null)
    await createFromCandidate(searchQuery, candidate, epoch)
  }, [createFromCandidate, searchQuery, searchState])

  const importLocalSource = useCallback(async () => {
    const trimmed = localPath.trim()
    if (!trimmed || localState === 'inspecting' || localState === 'creating') return
    const epoch = ++localEpoch.current
    setLocalState('inspecting')
    setLocalCandidate(null)
    setLocalError(null)
    setActionError(null)
    try {
      const candidate = await inspectLocalSource(trimmed)
      if (localEpoch.current !== epoch) return
      setLocalCandidate(candidate)
      setLocalState('creating')
      const created = await createLocalSourceImport(candidate)
      if (localEpoch.current !== epoch) return
      storeJob(created)
      setLocalPathValue('')
      setLocalState('created')
      setLocalError(null)
    } catch (error) {
      if (localEpoch.current !== epoch) return
      setLocalError(sourceImportErrorMessage(error))
      setLocalState('error')
    }
  }, [localPath, localState, storeJob])

  const cancel = useCallback(async () => {
    if (!selected || action) return
    const targetId = selected.import_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'cancel', targetId })
    setActionError(null)
    try {
      const updated = await cancelSourceImport(targetId)
      if (actionEpoch.current !== epoch) return
      storeJob(updated)
    } catch (error) {
      if (actionEpoch.current !== epoch) return
      setActionError(sourceImportErrorMessage(error))
    } finally {
      if (actionEpoch.current === epoch) setAction(null)
    }
  }, [action, selected, storeJob])

  const retry = useCallback(async () => {
    if (!selected || action) return
    const targetId = selected.import_id
    const epoch = ++actionEpoch.current
    setAction({ kind: 'retry', targetId })
    setActionError(null)
    try {
      const created = await retrySourceImport(targetId)
      if (actionEpoch.current !== epoch) return
      storeJob(created)
    } catch (error) {
      if (actionEpoch.current !== epoch) return
      setActionError(sourceImportErrorMessage(error))
    } finally {
      if (actionEpoch.current === epoch) setAction(null)
    }
  }, [action, selected, storeJob])

  return {
    query,
    setQuery,
    searchState,
    searchResolution,
    searchQuery,
    candidates,
    searchError,
    localPath,
    localState,
    localCandidate,
    localError,
    jobs,
    listState,
    listError,
    selectedId,
    selected,
    action,
    actionError,
    evidenceState,
    executionReceipt,
    formalization,
    evidenceError,
    hasActiveJobs,
    search,
    chooseCandidate,
    setLocalPath,
    importLocalSource,
    select: setSelectedId,
    cancel,
    retry,
    refresh,
  }
}
