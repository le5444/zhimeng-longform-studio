import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  approveContextHandoff,
  disableContextHandoff,
  getContextHandoff,
  prepareContextHandoff,
  rejectContextHandoff,
  runContextHandoff,
} from '../api/contextHandoffs'
import { RuntimeApiError } from '../api/runtime'
import { contextHandoffResponseFixture } from '../test/contextHandoffFixture'
import { useContextHandoff } from './useContextHandoff'

vi.mock('../api/contextHandoffs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/contextHandoffs')>()
  return {
    ...actual,
    getContextHandoff: vi.fn(),
    prepareContextHandoff: vi.fn(),
    approveContextHandoff: vi.fn(),
    rejectContextHandoff: vi.fn(),
    disableContextHandoff: vi.fn(),
    runContextHandoff: vi.fn(),
  }
})

describe('useContextHandoff', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(getContextHandoff).mockResolvedValue(contextHandoffResponseFixture())
    vi.mocked(prepareContextHandoff).mockResolvedValue(contextHandoffResponseFixture({ status: 'prepared' }))
    vi.mocked(approveContextHandoff).mockResolvedValue(contextHandoffResponseFixture({ status: 'approved' }))
    vi.mocked(rejectContextHandoff).mockResolvedValue(contextHandoffResponseFixture({ status: 'rejected' }))
    vi.mocked(disableContextHandoff).mockResolvedValue(contextHandoffResponseFixture({ status: 'disabled' }))
    vi.mocked(runContextHandoff).mockResolvedValue(contextHandoffResponseFixture({
      status: 'succeeded',
      context_pack_id: `context-pack-o2e-${'1'.repeat(32)}`,
      context_pack_hash: '2'.repeat(64),
      route_receipt_id: `nlcl-route-${'3'.repeat(32)}`,
      route_receipt_hash: '4'.repeat(64),
    }))
  })

  it('loads the current project record and clears state when disabled', async () => {
    const { result, rerender } = renderHook(
      ({ enabled, projectId }) => useContextHandoff(enabled, projectId, 3, 'f'.repeat(64)),
      { initialProps: { enabled: true, projectId: 'project-o2e' as string | null } },
    )

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(getContextHandoff).toHaveBeenCalledWith('project-o2e')

    rerender({ enabled: false, projectId: 'project-o2e' })
    await waitFor(() => expect(result.current.state).toBe('idle'))
    expect(result.current.record).toBeNull()
  })

  it('prepares with project identity and author-visible route inputs', async () => {
    const { result } = renderHook(
      () => useContextHandoff(true, 'project-o2e', 7, 'f'.repeat(64)),
    )
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => result.current.prepare({
      profile: 'review',
      stage: 'rewrite',
      chapterId: 'CH008',
      taskIntent: '为返工章装配写前上下文，不写正文。',
    }))

    expect(prepareContextHandoff).toHaveBeenCalledWith('project-o2e', {
      expected_project_revision: 7,
      expected_project_fingerprint: 'f'.repeat(64),
      profile: 'review',
      stage: 'rewrite',
      chapter_id: 'CH008',
      task_intent: '为返工章装配写前上下文，不写正文。',
    })
  })

  it('keeps approval and execution bound to the current CAS id', async () => {
    const contextHandoffId = `context-handoff-${'a'.repeat(32)}`
    const { result } = renderHook(
      () => useContextHandoff(true, 'project-o2e', 7, 'f'.repeat(64)),
    )
    await waitFor(() => expect(result.current.record?.context_handoff_id).toBe(contextHandoffId))

    await act(async () => result.current.approve())
    expect(approveContextHandoff).toHaveBeenCalledWith('project-o2e', contextHandoffId)

    await act(async () => result.current.run())
    expect(runContextHandoff).toHaveBeenCalledWith('project-o2e', contextHandoffId)
    expect(result.current.record?.status).toBe('succeeded')
  })

  it('surfaces fail-closed Runtime errors in author language', async () => {
    vi.mocked(getContextHandoff).mockRejectedValueOnce(new RuntimeApiError(409, {
      error: { message: 'O2-D 入口已变化，请重新准备并批准。' },
    }))
    const { result } = renderHook(
      () => useContextHandoff(true, 'project-o2e', 7, 'f'.repeat(64)),
    )

    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.error).toBe('O2-D 入口已变化，请重新准备并批准。')
  })
})
