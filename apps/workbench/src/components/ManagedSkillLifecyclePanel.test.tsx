import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  managedSkillActivationFixture,
  managedSkillLifecycleCatalogFixture,
  managedSkillVersionFixture,
} from '../test/managedSkillFixture'
import {
  useManagedSkills,
  type ManagedSkillLifecycleState,
} from '../hooks/useManagedSkills'
import { ManagedSkillLifecyclePanel } from './ManagedSkillLifecyclePanel'

vi.mock('../hooks/useManagedSkills', () => ({
  useManagedSkills: vi.fn(),
}))

function state(overrides: Partial<ManagedSkillLifecycleState> = {}): ManagedSkillLifecycleState {
  const catalog = managedSkillLifecycleCatalogFixture()
  return {
    state: 'ready',
    catalog,
    rows: [{
      activation: catalog.activations[0],
      versions: catalog.versions,
      activeVersion: catalog.versions[0],
      preferredVersion: catalog.versions[0],
    }],
    busySkillId: null,
    error: null,
    refresh: vi.fn(async () => undefined),
    enable: vi.fn(async () => undefined),
    disable: vi.fn(async () => undefined),
    inherit: vi.fn(async () => undefined),
    ...overrides,
  }
}

describe('ManagedSkillLifecyclePanel', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('renders project lifecycle rows without claiming formal prose or four-skills completion', () => {
    vi.mocked(useManagedSkills).mockReturnValue(state())

    render(<ManagedSkillLifecyclePanel enabled projectId="project-lifecycle" />)

    expect(screen.getByRole('region', { name: '受管 Skill 生命周期' })).toBeInTheDocument()
    expect(screen.getByText('Creation Suite Project Audit')).toBeInTheDocument()
    expect(screen.getByText('novel-creation-suite.project-audit')).toBeInTheDocument()
    expect(screen.getByText('项目级启用 / 禁用')).toBeInTheDocument()
    expect(screen.getByText(/不写正文/)).toBeInTheDocument()
    expect(screen.queryByText(/正式正文已写回/)).not.toBeInTheDocument()
    expect(screen.queryByText(/exactly-once/)).not.toBeInTheDocument()
  })

  it('wires enable from inherited, then disable and inherit from project override', () => {
    const inherited = state()
    vi.mocked(useManagedSkills).mockReturnValue(inherited)

    const { rerender } = render(<ManagedSkillLifecyclePanel enabled projectId="project-lifecycle" />)
    fireEvent.click(screen.getByRole('button', { name: /项目启用/ }))
    expect(inherited.enable).toHaveBeenCalledWith('novel-creation-suite.project-audit')

    const projectEnabled = state({
      rows: [{
        activation: managedSkillActivationFixture({
          configured_state: 'enabled',
          configured_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          state_revision: 1,
          effective_state: 'enabled',
          effective_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          effective_scope: 'project',
        }),
        versions: [managedSkillVersionFixture()],
        activeVersion: managedSkillVersionFixture(),
        preferredVersion: managedSkillVersionFixture(),
      }],
    })
    vi.mocked(useManagedSkills).mockReturnValue(projectEnabled)
    rerender(<ManagedSkillLifecyclePanel enabled projectId="project-lifecycle" />)

    fireEvent.click(screen.getByRole('button', { name: /项目禁用/ }))
    fireEvent.click(screen.getByRole('button', { name: /恢复继承/ }))
    expect(projectEnabled.disable).toHaveBeenCalledWith('novel-creation-suite.project-audit')
    expect(projectEnabled.inherit).toHaveBeenCalledWith('novel-creation-suite.project-audit')
  })
})
