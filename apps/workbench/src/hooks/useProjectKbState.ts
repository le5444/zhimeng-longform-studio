import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getProjectKbState,
  rebuildProjectKbState,
  type ProjectKbStateRecord,
} from '../api/knowledge'
import { RuntimeApiError } from '../api/runtime'

export type ProjectKbLoadState = 'idle' | 'loading' | 'ready' | 'rebuilding' | 'error'

interface ProjectKbBasis {
  enabled: boolean
  projectId: string | null
  projectRevision: number | null
  projectFingerprint: string | null
  scopeStateRevision: number | null
  knowledgeFingerprint: string | null
}

function errorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as { error?: { message?: unknown } } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
  }
  return error instanceof Error ? error.message : '项目知识快照请求失败'
}

export function useProjectKbState({
  enabled,
  projectId,
  projectRevision,
  projectFingerprint,
  scopeStateRevision,
  knowledgeFingerprint,
}: ProjectKbBasis) {
  const normalizedProjectId = projectId?.trim() || null
  const basisKey = JSON.stringify([
    normalizedProjectId,
    projectRevision,
    projectFingerprint,
    scopeStateRevision,
    knowledgeFingerprint,
  ])
  const available = Boolean(
    enabled
      && normalizedProjectId
      && projectRevision
      && projectFingerprint
      && scopeStateRevision
      && knowledgeFingerprint,
  )
  const [record, setRecord] = useState<ProjectKbStateRecord | null>(null)
  const [state, setState] = useState<ProjectKbLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const epoch = useRef(0)
  const activeBasisKey = useRef(basisKey)
  activeBasisKey.current = basisKey

  const isCurrent = useCallback((requestEpoch: number, requestBasisKey: string) => (
    epoch.current === requestEpoch && activeBasisKey.current === requestBasisKey
  ), [])

  const refresh = useCallback(async () => {
    if (!available || !normalizedProjectId) return null
    const requestEpoch = ++epoch.current
    const requestBasisKey = basisKey
    setState('loading')
    setError(null)
    try {
      const next = await getProjectKbState(normalizedProjectId)
      if (!isCurrent(requestEpoch, requestBasisKey)) return next
      setRecord(next)
      setState('ready')
      return next
    } catch (nextError) {
      if (isCurrent(requestEpoch, requestBasisKey)) {
        setError(errorMessage(nextError))
        setState('error')
      }
      throw nextError
    }
  }, [available, basisKey, isCurrent, normalizedProjectId])

  const rebuild = useCallback(async () => {
    if (
      !available
      || !normalizedProjectId
      || projectRevision === null
      || !projectFingerprint
      || scopeStateRevision === null
      || !knowledgeFingerprint
    ) return null
    if (record?.status === 'tampered') {
      throw new Error('项目知识快照已被篡改，需要先诊断 Runtime 状态')
    }
    const requestEpoch = ++epoch.current
    const requestBasisKey = basisKey
    setState('rebuilding')
    setError(null)
    try {
      const result = await rebuildProjectKbState(normalizedProjectId, {
        expected_project_revision: projectRevision,
        expected_project_fingerprint: projectFingerprint,
        expected_scope_state_revision: scopeStateRevision,
        expected_knowledge_fingerprint: knowledgeFingerprint,
      })
      if (!isCurrent(requestEpoch, requestBasisKey)) return result
      setRecord(result.record)
      setState('ready')
      return result
    } catch (nextError) {
      if (isCurrent(requestEpoch, requestBasisKey)) {
        setError(errorMessage(nextError))
        setState('error')
      }
      throw nextError
    }
  }, [
    available,
    basisKey,
    isCurrent,
    knowledgeFingerprint,
    normalizedProjectId,
    projectFingerprint,
    projectRevision,
    record?.status,
    scopeStateRevision,
  ])

  useEffect(() => {
    epoch.current += 1
    setRecord(null)
    setError(null)
    setState('idle')
    if (!available) return
    void refresh().catch(() => undefined)
    return () => {
      epoch.current += 1
    }
  }, [available, basisKey, refresh])

  const canRebuild = useMemo(
    () => available && state !== 'loading' && state !== 'rebuilding' && record?.status !== 'tampered',
    [available, record?.status, state],
  )

  return {
    record,
    state,
    error,
    available,
    canRebuild,
    refresh,
    rebuild,
  }
}
