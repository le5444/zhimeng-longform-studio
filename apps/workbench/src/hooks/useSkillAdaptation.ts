import { useCallback, useState } from 'react'
import { RuntimeApiError } from '../api/runtime'
import {
  getSkillAdaptationPlan,
  materializeSkillAdaptation,
  planSkillAdaptation,
  type SkillAdaptationMaterialization,
  type SkillAdaptationPlan,
} from '../api/skillAdaptations'

export type SkillAdaptationLoadState = 'idle' | 'loading' | 'ready' | 'error'

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

export function useSkillAdaptation(enabled: boolean) {
  const [sourcePath, setSourcePath] = useState('')
  const [plan, setPlan] = useState<SkillAdaptationPlan | null>(null)
  const [materialization, setMaterialization] = useState<SkillAdaptationMaterialization | null>(null)
  const [importResult, setImportResult] = useState<Record<string, unknown> | null>(null)
  const [state, setState] = useState<SkillAdaptationLoadState>('idle')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const planFromPath = useCallback(async () => {
    if (!enabled) return
    const normalized = sourcePath.trim()
    if (!normalized) {
      setError('请输入受信任目录下的 raw skill 路径')
      setState('error')
      return
    }
    setBusy(true)
    setError(null)
    setMaterialization(null)
    setImportResult(null)
    try {
      const response = await planSkillAdaptation({ source_path: normalized })
      setPlan(response.plan)
      setState('ready')
    } catch (currentError) {
      setPlan(null)
      setError(humanRuntimeError(currentError))
      setState('error')
    } finally {
      setBusy(false)
    }
  }, [enabled, sourcePath])

  const refreshPlan = useCallback(async () => {
    if (!enabled || !plan) return
    setBusy(true)
    setError(null)
    try {
      const response = await getSkillAdaptationPlan(plan.plan_id)
      setPlan(response.plan)
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    } finally {
      setBusy(false)
    }
  }, [enabled, plan])

  const materialize = useCallback(async (importAfter = true) => {
    if (!enabled || !plan) return
    setBusy(true)
    setError(null)
    try {
      const response = await materializeSkillAdaptation({
        expected_plan_id: plan.plan_id,
        expected_plan_hash: plan.plan_hash,
        import_after_materialize: importAfter,
      })
      setMaterialization(response.materialization)
      setImportResult(response.import_result)
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    } finally {
      setBusy(false)
    }
  }, [enabled, plan])

  return {
    sourcePath,
    setSourcePath,
    plan,
    materialization,
    importResult,
    state,
    busy,
    error,
    planFromPath,
    refreshPlan,
    materialize,
  }
}

export type SkillAdaptationState = ReturnType<typeof useSkillAdaptation>
