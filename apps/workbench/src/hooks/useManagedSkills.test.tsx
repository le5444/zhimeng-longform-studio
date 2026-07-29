import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  activateProjectManagedSkill,
  listProjectManagedSkills,
} from '../api/managedSkills'
import { RuntimeApiError } from '../api/runtime'
import {
  managedSkillActivationFixture,
  managedSkillActivationResponseFixture,
  managedSkillLifecycleCatalogFixture,
} from '../test/managedSkillFixture'
import { useManagedSkills } from './useManagedSkills'

vi.mock('../api/managedSkills', () => ({
  listProjectManagedSkills: vi.fn(),
  activateProjectManagedSkill: vi.fn(),
}))

describe('useManagedSkills', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('loads the project lifecycle catalog', async () => {
    const catalog = managedSkillLifecycleCatalogFixture()
    vi.mocked(listProjectManagedSkills).mockResolvedValue(catalog)

    const { result } = renderHook(() => useManagedSkills(true, 'project-lifecycle'))

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(listProjectManagedSkills).toHaveBeenCalledWith('project-lifecycle')
    expect(result.current.rows).toHaveLength(1)
    expect(result.current.rows[0]?.activation.skill_id).toBe('novel-creation-suite.project-audit')
  })

  it('enables and disables with CAS revision and prior version', async () => {
    const catalog = managedSkillLifecycleCatalogFixture({
      activations: [managedSkillActivationFixture({
        configured_state: 'inherited',
        configured_version_id: null,
        state_revision: 0,
        effective_state: 'enabled',
        effective_version_id: `skill-version-${'a'.repeat(32)}`,
        effective_scope: 'application',
      })],
    })
    vi.mocked(listProjectManagedSkills).mockResolvedValue(catalog)
    vi.mocked(activateProjectManagedSkill)
      .mockResolvedValueOnce(managedSkillActivationResponseFixture())
      .mockResolvedValueOnce(managedSkillActivationResponseFixture({
        activation: managedSkillActivationFixture({
          configured_state: 'disabled',
          configured_version_id: null,
          state_revision: 2,
          effective_state: 'disabled',
          effective_version_id: null,
          effective_scope: 'project',
        }),
      }))

    const { result } = renderHook(() => useManagedSkills(true, 'project-lifecycle'))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => {
      await result.current.enable('novel-creation-suite.project-audit')
    })
    expect(activateProjectManagedSkill).toHaveBeenCalledWith(
      'project-lifecycle',
      'novel-creation-suite.project-audit',
      {
        action: 'enable',
        skill_version_id: `skill-version-${'a'.repeat(32)}`,
        expected_state_revision: 0,
        expected_version_id: null,
      },
    )

    await act(async () => {
      await result.current.disable('novel-creation-suite.project-audit')
    })
    expect(activateProjectManagedSkill).toHaveBeenLastCalledWith(
      'project-lifecycle',
      'novel-creation-suite.project-audit',
      {
        action: 'disable',
        skill_version_id: null,
        expected_state_revision: 1,
        expected_version_id: `skill-version-${'a'.repeat(32)}`,
      },
    )
  })

  it('surfaces runtime CAS conflicts', async () => {
    vi.mocked(listProjectManagedSkills).mockRejectedValueOnce(new RuntimeApiError(409, {
      error: { code: 'managed_skill_activation_conflict', message: 'Skill activation conflict' },
    }))

    const { result } = renderHook(() => useManagedSkills(true, 'project-lifecycle'))
    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.error).toContain('Skill activation conflict')
  })
})
