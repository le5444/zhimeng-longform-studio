import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getProjectKbState,
  rebuildProjectKbState,
  type ProjectKbStateRecord,
} from '../api/knowledge'
import { useProjectKbState } from './useProjectKbState'

vi.mock('../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/knowledge')>()
  return {
    ...actual,
    getProjectKbState: vi.fn(),
    rebuildProjectKbState: vi.fn(),
  }
})

const projectId = `project-${'a'.repeat(24)}`
const projectFingerprint = '1'.repeat(64)
const knowledgeFingerprint = '2'.repeat(64)

function stateRecord(status: ProjectKbStateRecord['status']): ProjectKbStateRecord {
  return {
    schema: 'zhimeng.project-kb-state-record.v1',
    project_id: projectId,
    status,
    runtime_relative_root: `project-kb/${projectId}.kb`,
    pointer: null,
    manifest: null,
    reasons: status === 'stale' ? ['knowledge_projection_changed'] : [],
  }
}

function basis(overrides: Partial<Parameters<typeof useProjectKbState>[0]> = {}) {
  return {
    enabled: true,
    projectId,
    projectRevision: 7,
    projectFingerprint,
    scopeStateRevision: 3,
    knowledgeFingerprint,
    ...overrides,
  }
}

describe('useProjectKbState', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(getProjectKbState).mockResolvedValue(stateRecord('missing'))
    vi.mocked(rebuildProjectKbState).mockResolvedValue({
      schema: 'zhimeng.project-kb-rebuild-result.v1',
      reused_snapshot: false,
      record: stateRecord('current'),
    })
  })

  it('waits for the complete project and knowledge CAS basis', async () => {
    const { result } = renderHook(() => useProjectKbState(basis({
      projectFingerprint: null,
    })))

    expect(result.current.available).toBe(false)
    expect(result.current.state).toBe('idle')
    expect(getProjectKbState).not.toHaveBeenCalled()
  })

  it('loads state and rebuilds with the exact observed CAS values', async () => {
    const { result } = renderHook(() => useProjectKbState(basis()))
    await waitFor(() => expect(result.current.record?.status).toBe('missing'))

    await act(async () => {
      await result.current.rebuild()
    })

    expect(rebuildProjectKbState).toHaveBeenCalledWith(projectId, {
      expected_project_revision: 7,
      expected_project_fingerprint: projectFingerprint,
      expected_scope_state_revision: 3,
      expected_knowledge_fingerprint: knowledgeFingerprint,
    })
    expect(result.current.record?.status).toBe('current')
    expect(result.current.state).toBe('ready')
  })

  it('does not offer an automatic rebuild for tampered state', async () => {
    vi.mocked(getProjectKbState).mockResolvedValue(stateRecord('tampered'))
    const { result } = renderHook(() => useProjectKbState(basis()))
    await waitFor(() => expect(result.current.record?.status).toBe('tampered'))

    expect(result.current.canRebuild).toBe(false)
    await expect(result.current.rebuild()).rejects.toThrow('需要先诊断 Runtime 状态')
    expect(rebuildProjectKbState).not.toHaveBeenCalled()
  })
})
