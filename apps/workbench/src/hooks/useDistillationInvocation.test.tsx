import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getDistillationInvocation } from '../api/distillation'
import { RuntimeApiError } from '../api/runtime'
import {
  distillationInvocationSnapshotFixture,
  runtimeDistillationInvocationId,
} from '../test/distillationInvocationFixture'
import { useDistillationInvocation } from './useDistillationInvocation'

vi.mock('../api/distillation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/distillation')>()
  return {
    ...actual,
    getDistillationInvocation: vi.fn(),
  }
})

describe('useDistillationInvocation', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('loads a Runtime-owned deep distillation invocation snapshot', async () => {
    vi.mocked(getDistillationInvocation).mockResolvedValue(
      distillationInvocationSnapshotFixture(),
    )

    const { result } = renderHook(() => useDistillationInvocation(
      runtimeDistillationInvocationId,
    ))

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(result.current.state).toBe('ready')
    expect(result.current.snapshot?.invocation.provider_replay_count).toBe(0)
    expect(result.current.snapshot?.receipts.map((item) => item.kind)).toContain('candidate_output')
    expect(getDistillationInvocation).toHaveBeenCalledWith(
      runtimeDistillationInvocationId,
      {},
      expect.any(AbortSignal),
    )
  })

  it('stays idle for legacy source-import distillation ids', async () => {
    const legacyId = `source-import-invocation-${'2'.repeat(32)}`

    const { result } = renderHook(() => useDistillationInvocation(legacyId))
    await act(async () => Promise.resolve())

    expect(result.current.state).toBe('idle')
    expect(result.current.snapshot).toBeNull()
    expect(getDistillationInvocation).not.toHaveBeenCalled()
  })

  it('marks a linked invocation as missing instead of fabricating ledger evidence', async () => {
    vi.mocked(getDistillationInvocation).mockRejectedValue(
      new RuntimeApiError(404, { error: { code: 'distillation_invocation_not_found' } }),
    )

    const { result } = renderHook(() => useDistillationInvocation(
      runtimeDistillationInvocationId,
    ))

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(result.current.state).toBe('missing')
    expect(result.current.snapshot).toBeNull()
  })
})
