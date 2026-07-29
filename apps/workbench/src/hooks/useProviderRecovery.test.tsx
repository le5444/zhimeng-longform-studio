import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentCandidateOutputFixture } from '../test/agentCandidateOutputFixture'
import { providerRecoveryFixture } from '../test/providerRecoveryFixture'
import { useProviderRecovery } from './useProviderRecovery'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function conflictResponse(code: string): Response {
  return jsonResponse({
    schema: 'zhimeng.error.v1',
    error: {
      code,
      message: code === 'provider_recovery_stale'
        ? 'Provider recovery dossier changed before the author decision'
        : 'Provider recovery dossier already has a terminal decision',
      retryable: false,
    },
  }, 409)
}

describe('useProviderRecovery', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('loads a pending dossier and posts the immutable CAS cursor without any Provider call', async () => {
    const pending = await providerRecoveryFixture()
    const decided = await providerRecoveryFixture({ decisionAction: 'adopt_persisted_candidate' })
    const candidate = await agentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(pending))
      .mockResolvedValueOnce(jsonResponse(decided))
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderRecovery(
      pending.dossier.run_id,
      true,
      candidate,
    ))

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.canAdopt).toBe(true)

    let committed = false
    await act(async () => {
      committed = await result.current.decide('adopt_persisted_candidate')
    })

    expect(committed).toBe(true)
    expect(result.current.response?.decision?.action).toBe('adopt_persisted_candidate')
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls[1]?.[0]).toContain('/provider-recovery/decision')
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      schema: 'zhimeng.provider-recovery-decision-request.v1',
      expected_dossier_revision: pending.dossier.dossier_revision,
      expected_dossier_hash: pending.dossier.dossier_hash,
      action: 'adopt_persisted_candidate',
    })
    expect(fetcher.mock.calls.every(([url]) => String(url).includes('/provider-recovery'))).toBe(true)
  })

  it.each([
    ['provider_recovery_stale', '恢复收据已变化，已重新载入最新状态'],
    ['provider_recovery_already_decided', '恢复操作已经完成，已重新载入最新状态'],
  ] as const)('reconciles %s conflicts with the latest decision', async (code, expectedMessage) => {
    const pending = await providerRecoveryFixture()
    const decided = await providerRecoveryFixture({ decisionAction: 'abandon_and_recompile' })
    const candidate = await agentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(pending))
      .mockResolvedValueOnce(conflictResponse(code))
      .mockResolvedValueOnce(jsonResponse(decided))
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderRecovery(
      pending.dossier.run_id,
      true,
      candidate,
    ))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => {
      expect(await result.current.decide('abandon_and_recompile')).toBe(false)
    })

    await waitFor(() => expect(result.current.response?.decision).not.toBeNull())
    expect(result.current.response?.decision?.action).toBe('abandon_and_recompile')
    expect(result.current.actionError).toBe(expectedMessage)
    expect(result.current.action).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('does not read recovery state for an ineligible Run', () => {
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderRecovery(
      'run-0123456789abcdef0123456789',
      false,
      null,
    ))

    expect(result.current.state).toBe('idle')
    expect(result.current.response).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })
})
