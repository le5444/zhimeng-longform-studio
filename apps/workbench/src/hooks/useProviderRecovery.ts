import { useCallback, useEffect, useRef, useState } from 'react'
import {
  decideProviderRecovery,
  getProviderRecovery,
  type AgentCandidateOutput,
  type ProviderRecoveryAction,
  type ProviderRecoveryResponse,
} from '../api/agent'
import { RuntimeApiError } from '../api/runtime'

export type ProviderRecoveryLoadState = 'idle' | 'loading' | 'ready' | 'missing' | 'error'
export type ProviderRecoveryActionState = ProviderRecoveryAction | null

export interface ProviderRecoverySnapshot {
  response: ProviderRecoveryResponse | null
  state: ProviderRecoveryLoadState
  action: ProviderRecoveryActionState
  error: string | null
  actionError: string | null
  canAdopt: boolean
  refresh: () => void
  decide: (action: ProviderRecoveryAction) => Promise<boolean>
}

function recoveryErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown; code?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof Error) return error.message
  return 'Provider 恢复状态读取失败'
}

function recoveryErrorCode(error: unknown): string | null {
  if (!(error instanceof RuntimeApiError)) return null
  const payload = error.payload as { error?: { code?: unknown } } | null
  return typeof payload?.error?.code === 'string' ? payload.error.code : null
}

export function useProviderRecovery(
  runId: string | null,
  eligible: boolean,
  candidate: AgentCandidateOutput | null,
): ProviderRecoverySnapshot {
  const [response, setResponse] = useState<ProviderRecoveryResponse | null>(null)
  const [state, setState] = useState<ProviderRecoveryLoadState>('idle')
  const [action, setAction] = useState<ProviderRecoveryActionState>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const epoch = useRef(0)
  const pendingAction = useRef<ProviderRecoveryActionState>(null)

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    const requestEpoch = ++epoch.current
    if (!runId || !eligible) {
      setResponse(null)
      setState('idle')
      setError(null)
      setActionError(null)
      setAction(null)
      pendingAction.current = null
      return
    }
    const controller = new AbortController()
    setResponse(null)
    setState('loading')
    setError(null)
    setActionError(null)
    void getProviderRecovery(runId, {}, controller.signal)
      .then((nextResponse) => {
        if (controller.signal.aborted || epoch.current !== requestEpoch) return
        setResponse(nextResponse)
        setState('ready')
      })
      .catch((nextError: unknown) => {
        if (controller.signal.aborted || epoch.current !== requestEpoch) return
        if (nextError instanceof RuntimeApiError && nextError.status === 404) {
          setResponse(null)
          setState('missing')
          setError(null)
          return
        }
        setResponse(null)
        setState('error')
        setError(recoveryErrorMessage(nextError))
      })
    return () => controller.abort()
  }, [eligible, refreshKey, runId])

  const dossier = response?.dossier ?? null
  const canAdopt = dossier?.candidate_state === 'persisted'
    && dossier.candidate_output_id !== null
    && dossier.candidate_output_hash !== null
    && candidate?.candidate_output_id === dossier.candidate_output_id
    && candidate.envelope_hash === dossier.candidate_output_hash
    && response?.decision === null

  const decide = useCallback(async (nextAction: ProviderRecoveryAction): Promise<boolean> => {
    if (!runId || !response || response.decision !== null || pendingAction.current) return false
    if (nextAction === 'adopt_persisted_candidate' && !canAdopt) {
      setActionError('没有可核验的持久化候选，不能采纳')
      return false
    }
    const requestEpoch = ++epoch.current
    pendingAction.current = nextAction
    setAction(nextAction)
    setActionError(null)
    try {
      const nextResponse = await decideProviderRecovery(
        runId,
        {
          schema: 'zhimeng.provider-recovery-decision-request.v1',
          expected_dossier_revision: response.dossier.dossier_revision,
          expected_dossier_hash: response.dossier.dossier_hash,
          action: nextAction,
        },
        {},
      )
      if (epoch.current !== requestEpoch) return false
      setResponse(nextResponse)
      setState('ready')
      return nextResponse.decision?.action === nextAction
    } catch (nextError: unknown) {
      if (epoch.current !== requestEpoch) return false
      const code = recoveryErrorCode(nextError)
      if (code === 'provider_recovery_stale' || code === 'provider_recovery_already_decided') {
        try {
          const latest = await getProviderRecovery(runId)
          if (epoch.current === requestEpoch) {
            setResponse(latest)
            setState('ready')
            setActionError(code === 'provider_recovery_stale'
              ? '恢复收据已变化，已重新载入最新状态'
              : '恢复操作已经完成，已重新载入最新状态')
          }
        } catch (refreshError: unknown) {
          if (epoch.current === requestEpoch) setActionError(recoveryErrorMessage(refreshError))
        }
      } else {
        setActionError(recoveryErrorMessage(nextError))
      }
      return false
    } finally {
      if (epoch.current === requestEpoch) {
        pendingAction.current = null
        setAction(null)
      }
    }
  }, [canAdopt, response, runId])

  return {
    response,
    state,
    action,
    error,
    actionError,
    canAdopt,
    refresh,
    decide,
  }
}
