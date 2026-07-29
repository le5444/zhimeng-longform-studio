import { useCallback, useEffect, useState } from 'react'
import { RuntimeApiError } from '../api/runtime'
import {
  approveSkillHandoff,
  disableSkillHandoff,
  getSkillHandoff,
  prepareSkillHandoff,
  rejectSkillHandoff,
  runSkillHandoff,
  type SkillHandoffRecord,
} from '../api/skillHandoffs'

export type SkillHandoffLoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface SkillHandoffState {
  state: SkillHandoffLoadState
  record: SkillHandoffRecord | null
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

export function useSkillHandoff(
  enabled: boolean,
  projectId: string | null | undefined,
  projectRevision?: number | null,
  projectFingerprint?: string | null,
): SkillHandoffState {
  const [state, setState] = useState<SkillHandoffLoadState>('idle')
  const [record, setRecord] = useState<SkillHandoffRecord | null>(null)
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
      const response = await getSkillHandoff(normalizedProjectId)
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

  const mutate = useCallback(async (action: () => Promise<{ record: SkillHandoffRecord }>) => {
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
    await mutate(() => prepareSkillHandoff(normalizedProjectId, {
      expected_project_revision: projectRevision,
      expected_project_fingerprint: projectFingerprint,
      source_import_id: sourceImportId ?? null,
    }))
  }, [mutate, normalizedProjectId, projectFingerprint, projectRevision])

  const approve = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => approveSkillHandoff(normalizedProjectId, record.handoff_id))
  }, [mutate, normalizedProjectId, record])

  const reject = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => rejectSkillHandoff(normalizedProjectId, record.handoff_id))
  }, [mutate, normalizedProjectId, record])

  const disable = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => disableSkillHandoff(normalizedProjectId, record.handoff_id))
  }, [mutate, normalizedProjectId, record])

  const run = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => runSkillHandoff(normalizedProjectId, record.handoff_id))
  }, [mutate, normalizedProjectId, record])

  return { state, record, busy, error, refresh, prepare, approve, reject, disable, run }
}
