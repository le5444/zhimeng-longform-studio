import { useCallback, useEffect, useState } from 'react'
import {
  getWriterTrace,
  isTerminalAgentRun,
  type AgentCandidateOutput,
  type AgentWorkflowState,
  type AgentRunStatus,
  type CreationInstructionActivationBinding,
  type CreationInstructionDispatchReceipt,
  type CreationInstructionMaterialization,
  type ProviderRecoveryResponse,
  type WriterCandidateResult,
  type WriterTask,
} from '../api/agent'
import { RuntimeApiError } from '../api/runtime'

export type WriterTraceLoadState = 'idle' | 'loading' | 'ready' | 'partial' | 'missing' | 'error'

export interface WriterTraceSnapshot {
  workflow: AgentWorkflowState | null
  task: WriterTask | null
  activation: CreationInstructionActivationBinding | null
  materialization: CreationInstructionMaterialization | null
  dispatch: CreationInstructionDispatchReceipt | null
  candidate: AgentCandidateOutput | null
  result: WriterCandidateResult | null
  recovery: ProviderRecoveryResponse | null
  state: WriterTraceLoadState
  error: string | null
  refresh: () => void
}

function traceErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  return error instanceof Error ? error.message : 'Writer 来源链读取失败'
}

function missing(error: unknown): boolean {
  return error instanceof RuntimeApiError && error.status === 404
}

export function useWriterTrace(
  runId: string | null,
  runStatus: AgentRunStatus | null,
  candidate: AgentCandidateOutput | null,
): WriterTraceSnapshot {
  const [workflow, setWorkflow] = useState<AgentWorkflowState | null>(null)
  const [task, setTask] = useState<WriterTask | null>(null)
  const [activation, setActivation] = useState<CreationInstructionActivationBinding | null>(null)
  const [materialization, setMaterialization] = useState<CreationInstructionMaterialization | null>(null)
  const [dispatch, setDispatch] = useState<CreationInstructionDispatchReceipt | null>(null)
  const [traceCandidate, setTraceCandidate] = useState<AgentCandidateOutput | null>(null)
  const [result, setResult] = useState<WriterCandidateResult | null>(null)
  const [recovery, setRecovery] = useState<ProviderRecoveryResponse | null>(null)
  const [state, setState] = useState<WriterTraceLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    if (!runId || !runStatus || !isTerminalAgentRun(runStatus)) {
      setWorkflow(null)
      setTask(null)
      setActivation(null)
      setMaterialization(null)
      setDispatch(null)
      setTraceCandidate(null)
      setResult(null)
      setRecovery(null)
      setState('idle')
      setError(null)
      return
    }
    const controller = new AbortController()
    setWorkflow(null)
    setTask(null)
    setActivation(null)
    setMaterialization(null)
    setDispatch(null)
    setTraceCandidate(null)
    setResult(null)
    setRecovery(null)
    setState('loading')
    setError(null)
    void getWriterTrace(runId, {}, controller.signal)
      .then((trace) => {
        if (controller.signal.aborted) return
        if (candidate && trace.candidate_output
          && (candidate.candidate_output_id !== trace.candidate_output.candidate_output_id
            || candidate.envelope_hash !== trace.candidate_output.envelope_hash)) {
          throw new Error('Writer Trace 与当前 Candidate 来源链不一致')
        }
        setWorkflow(trace.workflow)
        setTask(trace.writer_task)
        setActivation(trace.activation_binding)
        setMaterialization(trace.materialization)
        setDispatch(trace.dispatch_receipt)
        setTraceCandidate(trace.candidate_output)
        setResult(trace.writer_candidate_result)
        setRecovery(trace.provider_recovery)
        setState(
          trace.writer_task
            && trace.activation_binding
            && trace.materialization
            && trace.dispatch_receipt
            && trace.candidate_output
            && trace.writer_candidate_result
            ? 'ready'
            : 'partial',
        )
      })
      .catch((nextError: unknown) => {
        if (controller.signal.aborted) return
        if (missing(nextError)) {
          setState('missing')
          setError(null)
          return
        }
        setState('error')
        setError(traceErrorMessage(nextError))
      })
    return () => controller.abort()
  }, [candidate, refreshKey, runId, runStatus])

  return {
    workflow,
    task,
    activation,
    materialization,
    dispatch,
    candidate: traceCandidate,
    result,
    recovery,
    state,
    error,
    refresh,
  }
}
