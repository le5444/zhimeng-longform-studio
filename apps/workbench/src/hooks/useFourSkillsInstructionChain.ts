import { useCallback, useMemo, useState } from 'react'
import { RuntimeApiError } from '../api/runtime'
import {
  bindFourSkillsWorkflow,
  bindProjectInstructionChainHandoff,
  handoffFourSkillsInstructionChain,
  materializeFourSkillsRawAdaptation,
  probeFourSkillsGenericExecution,
  executeFourSkillsGenericManaged,
  orchestrateFourSkillsWorkflow,
  planFourSkillsRawAdaptation,
  type FourSkillsFamily,
  type FourSkillsGenericExecutionProbeReceipt,
  type FourSkillsGenericManagedExecutionReceipt,
  type FourSkillsInstructionChainHandoffReceipt,
  type FourSkillsRawAdaptationReceipt,
  type FourSkillsWorkflowBindingReceipt,
  type FourSkillsWorkflowOrchestrationReceipt,
  type ProjectInstructionChainHandoffBindingReceipt,
} from '../api/fourSkillsRawAdaptations'

export type FourSkillsChainLoadState = 'idle' | 'loading' | 'ready' | 'error'

const FAMILY_LABELS: Record<FourSkillsFamily, string> = {
  source_ingest: '源导入',
  distillation: '蒸馏',
  knowledge_context: '知识上下文',
  creation_suite: 'Creation Suite',
}

const EMPTY_PATHS: Record<FourSkillsFamily, string> = {
  source_ingest: '',
  distillation: '',
  knowledge_context: '',
  creation_suite: '',
}

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

export function useFourSkillsInstructionChain(
  enabled: boolean,
  projectId: string | null = null,
  projectRevision: number | null = null,
  projectFingerprint: string | null = null,
) {
  const [paths, setPaths] = useState(EMPTY_PATHS)
  const [batch, setBatch] = useState<FourSkillsRawAdaptationReceipt | null>(null)
  const [binding, setBinding] = useState<FourSkillsWorkflowBindingReceipt | null>(null)
  const [orchestration, setOrchestration] = useState<FourSkillsWorkflowOrchestrationReceipt | null>(null)
  const [handoff, setHandoff] = useState<FourSkillsInstructionChainHandoffReceipt | null>(null)
  const [executionProbe, setExecutionProbe] = useState<FourSkillsGenericExecutionProbeReceipt | null>(null)
  const [managedExecution, setManagedExecution] = useState<FourSkillsGenericManagedExecutionReceipt | null>(null)
  const [projectBinding, setProjectBinding] = useState<ProjectInstructionChainHandoffBindingReceipt | null>(null)
  const [notClaiming, setNotClaiming] = useState<string[]>([])
  const [state, setState] = useState<FourSkillsChainLoadState>('idle')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const membersReady = useMemo(
    () => (Object.values(paths) as string[]).every((value) => value.trim().length > 0),
    [paths],
  )

  const setPath = useCallback((family: FourSkillsFamily, value: string) => {
    setPaths((current) => ({ ...current, [family]: value }))
  }, [])

  const runFullChain = useCallback(async () => {
    if (!enabled) return
    if (!membersReady) {
      setError('请填齐四家族 raw skill 路径')
      setState('error')
      return
    }
    setBusy(true)
    setError(null)
    setBinding(null)
    setOrchestration(null)
    setHandoff(null)
    setExecutionProbe(null)
    setManagedExecution(null)
    setProjectBinding(null)
    try {
      const members = (Object.entries(paths) as Array<[FourSkillsFamily, string]>).map(
        ([family, source_path]) => ({ family, source_path: source_path.trim() }),
      )
      const planned = await planFourSkillsRawAdaptation(members)
      setBatch(planned.receipt)
      setNotClaiming(planned.not_claiming)

      const materialized = await materializeFourSkillsRawAdaptation({
        expected_batch_id: planned.receipt.batch_id,
        expected_batch_hash: planned.receipt.batch_hash,
        import_after_materialize: true,
      })
      setBatch(materialized.receipt)
      setNotClaiming(materialized.not_claiming)

      const bound = await bindFourSkillsWorkflow(materialized.receipt.batch_id, {
        expected_batch_id: materialized.receipt.batch_id,
        expected_batch_hash: materialized.receipt.batch_hash,
      })
      setBinding(bound.receipt)
      setNotClaiming(bound.not_claiming)

      const orchestrated = await orchestrateFourSkillsWorkflow(materialized.receipt.batch_id, {
        expected_batch_id: materialized.receipt.batch_id,
        expected_batch_hash: materialized.receipt.batch_hash,
        expected_binding_id: bound.receipt.binding_id,
        expected_binding_hash: bound.receipt.binding_hash,
      })
      setOrchestration(orchestrated.receipt)
      setNotClaiming(orchestrated.not_claiming)

      const handed = await handoffFourSkillsInstructionChain(materialized.receipt.batch_id, {
        expected_batch_id: materialized.receipt.batch_id,
        expected_batch_hash: materialized.receipt.batch_hash,
        expected_binding_id: bound.receipt.binding_id,
        expected_binding_hash: bound.receipt.binding_hash,
        expected_orchestration_id: orchestrated.receipt.orchestration_id,
        expected_orchestration_hash: orchestrated.receipt.orchestration_hash,
      })
      setHandoff(handed.receipt)
      setNotClaiming(handed.not_claiming)

      const probed = await probeFourSkillsGenericExecution(materialized.receipt.batch_id, {
        expected_batch_id: materialized.receipt.batch_id,
        expected_batch_hash: materialized.receipt.batch_hash,
        expected_handoff_id: handed.receipt.handoff_id,
        expected_handoff_hash: handed.receipt.handoff_hash,
      })
      setExecutionProbe(probed.receipt)
      setNotClaiming(probed.not_claiming)

      if (projectId && projectRevision != null && projectFingerprint) {
        const managed = await executeFourSkillsGenericManaged(materialized.receipt.batch_id, {
          expected_batch_id: materialized.receipt.batch_id,
          expected_batch_hash: materialized.receipt.batch_hash,
          expected_probe_id: probed.receipt.probe_id,
          expected_probe_hash: probed.receipt.probe_hash,
          project_id: projectId,
          expected_project_revision: projectRevision,
          expected_project_fingerprint: projectFingerprint,
        })
        setManagedExecution(managed.receipt)
        setNotClaiming(managed.not_claiming)
      }

      if (projectId && projectRevision != null && projectFingerprint) {
        const projectBound = await bindProjectInstructionChainHandoff(projectId, {
          expected_project_revision: projectRevision,
          expected_project_fingerprint: projectFingerprint,
          batch_id: materialized.receipt.batch_id,
          batch_hash: materialized.receipt.batch_hash,
          handoff_id: handed.receipt.handoff_id,
          handoff_hash: handed.receipt.handoff_hash,
        })
        setProjectBinding(projectBound.receipt)
        setNotClaiming(projectBound.not_claiming)
      }
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    } finally {
      setBusy(false)
    }
  }, [enabled, membersReady, paths, projectFingerprint, projectId, projectRevision])

  return {
    paths,
    setPath,
    membersReady,
    batch,
    binding,
    orchestration,
    handoff,
    executionProbe,
    managedExecution,
    projectBinding,
    notClaiming,
    state,
    busy,
    error,
    runFullChain,
    familyLabels: FAMILY_LABELS,
  }
}

export type FourSkillsInstructionChainState = ReturnType<typeof useFourSkillsInstructionChain>
