import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  activateProjectManagedSkill,
  listProjectManagedSkills,
  type ManagedSkillActivationRecord,
  type ManagedSkillActivationRequest,
  type ManagedSkillLifecycleCatalogResponse,
  type ManagedSkillVersionRecord,
} from '../api/managedSkills'
import { RuntimeApiError } from '../api/runtime'

export type ManagedSkillLoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface ManagedSkillRow {
  activation: ManagedSkillActivationRecord
  versions: ManagedSkillVersionRecord[]
  activeVersion: ManagedSkillVersionRecord | null
  preferredVersion: ManagedSkillVersionRecord | null
}

export interface ManagedSkillLifecycleState {
  state: ManagedSkillLoadState
  catalog: ManagedSkillLifecycleCatalogResponse | null
  rows: ManagedSkillRow[]
  busySkillId: string | null
  error: string | null
  refresh: () => Promise<void>
  enable: (skillId: string, skillVersionId?: string | null) => Promise<void>
  disable: (skillId: string) => Promise<void>
  inherit: (skillId: string) => Promise<void>
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

function buildRows(catalog: ManagedSkillLifecycleCatalogResponse | null): ManagedSkillRow[] {
  if (!catalog) return []
  return catalog.activations
    .map((activation) => {
      const versions = catalog.versions
        .filter((version) => version.manifest.id === activation.skill_id)
        .sort((left, right) => right.imported_at.localeCompare(left.imported_at))
      const activeVersion = versions.find((version) => (
        version.skill_version_id === activation.effective_version_id
        || version.skill_version_id === activation.configured_version_id
      )) ?? null
      return {
        activation,
        versions,
        activeVersion,
        preferredVersion: activeVersion ?? versions[0] ?? null,
      }
    })
    .sort((left, right) => left.activation.skill_id.localeCompare(right.activation.skill_id))
}

export function useManagedSkills(
  enabled: boolean,
  projectId: string | null | undefined,
): ManagedSkillLifecycleState {
  const [state, setState] = useState<ManagedSkillLoadState>('idle')
  const [catalog, setCatalog] = useState<ManagedSkillLifecycleCatalogResponse | null>(null)
  const [busySkillId, setBusySkillId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const normalizedProjectId = projectId?.trim() || null

  const refresh = useCallback(async () => {
    if (!enabled || !normalizedProjectId) {
      setState('idle')
      setCatalog(null)
      setError(null)
      return
    }
    setState('loading')
    setError(null)
    try {
      const response = await listProjectManagedSkills(normalizedProjectId)
      setCatalog(response)
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
    }
  }, [enabled, normalizedProjectId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const mutate = useCallback(async (
    skillId: string,
    request: ManagedSkillActivationRequest,
  ) => {
    if (!normalizedProjectId) return
    setBusySkillId(skillId)
    setError(null)
    try {
      const response = await activateProjectManagedSkill(normalizedProjectId, skillId, request)
      setCatalog((current) => {
        if (!current) {
          return {
            schema: 'zhimeng.managed-skill-lifecycle-catalog.v1',
            scope: response.activation.scope,
            versions: [],
            activations: [response.activation],
          }
        }
        const activations = current.activations.some((item) => item.skill_id === skillId)
          ? current.activations.map((item) => (
            item.skill_id === skillId ? response.activation : item
          ))
          : [...current.activations, response.activation]
        return { ...current, activations }
      })
      setState('ready')
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
      setState('error')
      throw currentError
    } finally {
      setBusySkillId(null)
    }
  }, [normalizedProjectId])

  const enable = useCallback(async (skillId: string, skillVersionId?: string | null) => {
    const row = buildRows(catalog).find((item) => item.activation.skill_id === skillId)
    if (!row) return
    const targetVersionId = skillVersionId
      ?? row.preferredVersion?.skill_version_id
      ?? row.activation.effective_version_id
      ?? row.activation.configured_version_id
    if (!targetVersionId) {
      setError('没有可启用的 Skill 版本。')
      setState('error')
      return
    }
    await mutate(skillId, {
      action: 'enable',
      skill_version_id: targetVersionId,
      expected_state_revision: row.activation.state_revision,
      expected_version_id: row.activation.configured_version_id,
    })
  }, [catalog, mutate])

  const disable = useCallback(async (skillId: string) => {
    const row = buildRows(catalog).find((item) => item.activation.skill_id === skillId)
    if (!row) return
    await mutate(skillId, {
      action: 'disable',
      skill_version_id: null,
      expected_state_revision: row.activation.state_revision,
      expected_version_id: row.activation.configured_version_id,
    })
  }, [catalog, mutate])

  const inherit = useCallback(async (skillId: string) => {
    const row = buildRows(catalog).find((item) => item.activation.skill_id === skillId)
    if (!row) return
    await mutate(skillId, {
      action: 'inherit',
      skill_version_id: null,
      expected_state_revision: row.activation.state_revision,
      expected_version_id: row.activation.configured_version_id,
    })
  }, [catalog, mutate])

  const rows = useMemo(() => buildRows(catalog), [catalog])

  return {
    state,
    catalog,
    rows,
    busySkillId,
    error,
    refresh,
    enable,
    disable,
    inherit,
  }
}
