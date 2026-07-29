import { useCallback, useState } from 'react'
import { RuntimeApiError } from '../api/runtime'
import {
  WORKBENCH_DUAL_PROJECT_AUDIT_STEPS,
  approveManagedSkillChain,
  disableManagedSkillChain,
  getManagedSkillChain,
  prepareManagedSkillChain,
  rejectManagedSkillChain,
  runManagedSkillChain,
  type ManagedSkillChainRunRecord,
} from '../api/managedSkillChains'

export type ManagedSkillChainLoadState = 'idle' | 'loading' | 'ready' | 'error'

function humanRuntimeError(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: string; code?: string }
      detail?: string
    } | null
    return payload?.error?.message
      ?? payload?.detail
      ?? payload?.error?.code
      ?? `Runtime HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '未知错误'
}

export function useManagedSkillChain(
  enabled: boolean,
  projectId: string | null | undefined,
  projectRevision?: number | null,
  projectFingerprint?: string | null,
) {
  const [state, setState] = useState<ManagedSkillChainLoadState>('idle')
  const [record, setRecord] = useState<ManagedSkillChainRunRecord | null>(null)
  const [notClaiming, setNotClaiming] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const normalizedProjectId = projectId?.trim() || null

  const mutate = useCallback(async (
    action: () => Promise<{ record: ManagedSkillChainRunRecord; not_claiming: string[] }>,
  ) => {
    setBusy(true)
    setError(null)
    try {
      const response = await action()
      setRecord(response.record)
      setNotClaiming([...response.not_claiming])
      setState('ready')
      return response.record
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
      throw currentError
    } finally {
      setBusy(false)
    }
  }, [])

  const refresh = useCallback(async () => {
    if (!enabled || !normalizedProjectId || !record) {
      return
    }
    setState('loading')
    setError(null)
    try {
      const response = await getManagedSkillChain(normalizedProjectId, record.chain_id)
      setRecord(response.record)
      setNotClaiming([...response.not_claiming])
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    }
  }, [enabled, normalizedProjectId, record])

  const prepareDualProjectAudit = useCallback(async () => {
    if (!enabled || !normalizedProjectId || !projectRevision || !projectFingerprint) return
    await mutate(() => prepareManagedSkillChain(normalizedProjectId, {
      expected_project_revision: projectRevision,
      expected_project_fingerprint: projectFingerprint,
      chain_key: 'workbench-dual-project-audit',
      steps: WORKBENCH_DUAL_PROJECT_AUDIT_STEPS,
    }))
  }, [enabled, mutate, normalizedProjectId, projectFingerprint, projectRevision])

  const approve = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => approveManagedSkillChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const reject = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => rejectManagedSkillChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const disable = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => disableManagedSkillChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  const run = useCallback(async () => {
    if (!normalizedProjectId || !record) return
    await mutate(() => runManagedSkillChain(normalizedProjectId, record.chain_id))
  }, [mutate, normalizedProjectId, record])

  return {
    state,
    record,
    notClaiming,
    busy,
    error,
    refresh,
    prepareDualProjectAudit,
    approve,
    reject,
    disable,
    run,
  }
}

export type ManagedSkillChainState = ReturnType<typeof useManagedSkillChain>
