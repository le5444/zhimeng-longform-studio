import { useCallback, useEffect, useState } from 'react'
import { RuntimeApiError } from '../api/runtime'
import {
  approveAdjacentChain,
  disableAdjacentChain,
  getAdjacentChain,
  prepareAdjacentChain,
  rejectAdjacentChain,
  runAdjacentChain,
  type AdjacentChainRecord,
} from '../api/adjacentChains'

export type AdjacentChainLoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface AdjacentChainState {
  state: AdjacentChainLoadState
  record: AdjacentChainRecord | null
  busy: boolean
  error: string | null
  refresh: () => Promise<void>
  prepare: (sourceImportId?: string | null) => Promise<void>
  approve: () => Promise<void>
  reject: () => Promise<void>
  disable: () => Promise<void>
  run: () => Promise<void>
}

function humanRuntimeError(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as { error?: { message?: string; code?: string }; detail?: string } | null
    return payload?.error?.message
      ?? payload?.detail
      ?? payload?.error?.code
      ?? `Runtime HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '未知错误'
}

export function useAdjacentChain(
  enabled: boolean,
  projectId: string | null | undefined,
  projectRevision?: number | null,
  projectFingerprint?: string | null,
  activeChapterId?: string | null,
): AdjacentChainState {
  const [state, setState] = useState<AdjacentChainLoadState>('idle')
  const [record, setRecord] = useState<AdjacentChainRecord | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const normalizedProjectId = projectId?.trim() || null

  const refresh = useCallback(async () => {
    if (!enabled || !normalizedProjectId) {
      setState('idle')
      setRecord(null)
      setError(null)
      return
    }
    setState('loading')
    setError(null)
    try {
      const response = await getAdjacentChain(normalizedProjectId)
      setRecord(response.record)
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    }
  }, [enabled, normalizedProjectId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const mutate = useCallback(async (action: () => Promise<{ record: AdjacentChainRecord }>) => {
    setBusy(true)
    setError(null)
    try {
      const response = await action()
      setRecord(response.record)
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
      throw currentError
    } finally {
      setBusy(false)
    }
  }, [])

  const prepare = useCallback(async (sourceImportId?: string | null) => {
    if (!normalizedProjectId || !projectRevision || !projectFingerprint) return
    const chapterId = activeChapterId?.trim() || 'CH001'
    await mutate(() => prepareAdjacentChain(normalizedProjectId, {
      expected_project_revision: projectRevision,
      expected_project_fingerprint: projectFingerprint,
      source_import_id: sourceImportId ?? null,
      profile: 'chapter',
      stage: 'draft',
      chapter_id: chapterId,
      task_intent: '连续执行 O2-D 到 O2-E，只生成写前 Context Pack。',
    }))
  }, [activeChapterId, mutate, normalizedProjectId, projectFingerprint, projectRevision])

  const approve = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => approveAdjacentChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const reject = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => rejectAdjacentChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const disable = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => disableAdjacentChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const run = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => runAdjacentChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  return { state, record, busy, error, refresh, prepare, approve, reject, disable, run }
}
