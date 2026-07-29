import { useCallback, useEffect, useState } from 'react'
import {
  assertProviderTurnReceiptBindings,
  getProviderTurnReceipts,
  type AgentCandidateOutput,
  type ProviderTurnReceiptListResponse,
} from '../api/agent'
import { RuntimeApiError } from '../api/runtime'

export type ProviderTurnReceiptsLoadState =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'legacy'
  | 'missing'
  | 'error'

export interface ProviderTurnReceiptsSnapshot {
  evidence: ProviderTurnReceiptListResponse | null
  state: ProviderTurnReceiptsLoadState
  error: string | null
  refresh: () => void
}

function receiptErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof Error) return error.message
  return 'Provider 身份收据读取失败'
}

export function useProviderTurnReceipts(
  candidate: AgentCandidateOutput | null,
): ProviderTurnReceiptsSnapshot {
  const [evidence, setEvidence] = useState<ProviderTurnReceiptListResponse | null>(null)
  const [state, setState] = useState<ProviderTurnReceiptsLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    if (!candidate) {
      setEvidence(null)
      setState('idle')
      setError(null)
      return
    }
    if (candidate.schema === 'zhimeng.agent-candidate-output.v1') {
      setEvidence(null)
      setState('legacy')
      setError(null)
      return
    }
    const controller = new AbortController()
    setEvidence(null)
    setState('loading')
    setError(null)
    void getProviderTurnReceipts(candidate.run_id, {}, controller.signal)
      .then((nextEvidence) => {
        if (controller.signal.aborted) return
        assertProviderTurnReceiptBindings(candidate, nextEvidence)
        setEvidence(nextEvidence)
        setState('ready')
      })
      .catch((nextError: unknown) => {
        if (controller.signal.aborted) return
        setEvidence(null)
        if (nextError instanceof RuntimeApiError && nextError.status === 404) {
          setState('missing')
          setError(`Candidate ${candidate.schema.endsWith('.v3') ? 'v3' : 'v2'} 缺少对应的 Provider 身份收据`)
          return
        }
        setState('error')
        setError(receiptErrorMessage(nextError))
      })
    return () => controller.abort()
  }, [candidate, refreshKey])

  return { evidence, state, error, refresh }
}
