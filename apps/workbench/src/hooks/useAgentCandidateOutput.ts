import { useCallback, useEffect, useState } from 'react'
import {
  getAgentCandidateOutput,
  isTerminalAgentRun,
  type AgentCandidateOutput,
  type AgentRunStatus,
} from '../api/agent'
import { RuntimeApiError } from '../api/runtime'

export type AgentCandidateOutputLoadState = 'idle' | 'loading' | 'ready' | 'missing' | 'error'

export interface AgentCandidateOutputSnapshot {
  output: AgentCandidateOutput | null
  state: AgentCandidateOutputLoadState
  error: string | null
  refresh: () => void
}

function candidateErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof Error) return error.message
  return '候选输出读取失败'
}

export function useAgentCandidateOutput(
  runId: string | null,
  runStatus: AgentRunStatus | null,
): AgentCandidateOutputSnapshot {
  const [output, setOutput] = useState<AgentCandidateOutput | null>(null)
  const [state, setState] = useState<AgentCandidateOutputLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    if (!runId || !runStatus || !isTerminalAgentRun(runStatus)) {
      setOutput(null)
      setState('idle')
      setError(null)
      return
    }
    const controller = new AbortController()
    setOutput(null)
    setState('loading')
    setError(null)
    void getAgentCandidateOutput(runId, {}, controller.signal)
      .then((nextOutput) => {
        if (controller.signal.aborted) return
        setOutput(nextOutput)
        setState('ready')
      })
      .catch((nextError: unknown) => {
        if (controller.signal.aborted) return
        if (nextError instanceof RuntimeApiError && nextError.status === 404) {
          setOutput(null)
          setState('missing')
          setError(null)
          return
        }
        setOutput(null)
        setState('error')
        setError(candidateErrorMessage(nextError))
      })
    return () => controller.abort()
  }, [refreshKey, runId, runStatus])

  return { output, state, error, refresh }
}
