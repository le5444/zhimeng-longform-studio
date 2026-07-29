import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  agentCandidateOutputFixture,
  legacyAgentCandidateOutputFixture,
} from '../test/agentCandidateOutputFixture'
import { providerTurnReceiptListFixture } from '../test/providerTurnReceiptFixture'
import { useProviderTurnReceipts } from './useProviderTurnReceipts'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useProviderTurnReceipts', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each([
    { label: 'exact_match', options: {}, expectedStatus: 'verified' },
    {
      label: 'accepted_alias',
      options: {
        requestedModel: 'requested-model',
        reportedModel: 'gateway-model',
        acceptedReportedModels: ['gateway-model'],
      },
      expectedStatus: 'verified',
    },
    { label: 'unreported', options: { reportedModel: null }, expectedStatus: 'unreported' },
  ] as const)('loads and cross-validates $label Provider evidence', async ({
    options,
    expectedStatus,
  }) => {
    const candidate = await agentCandidateOutputFixture(options)
    const evidence = await providerTurnReceiptListFixture(options)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(evidence))
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderTurnReceipts(candidate))

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.evidence?.provider_identity_status).toBe(expectedStatus)
    expect(result.current.error).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]?.[0]).toContain('/provider-turn-receipts')
  })

  it.each([
    {
      label: 'rejected mismatch',
      evidence: () => providerTurnReceiptListFixture({ reportedModel: 'unexpected-model' }),
    },
    {
      label: 'pending list',
      evidence: () => providerTurnReceiptListFixture({ pending: true }),
    },
    {
      label: 'cross-Run list',
      evidence: async () => ({
        ...(await providerTurnReceiptListFixture()),
        run_id: 'run-other-identity',
      }),
    },
    {
      label: 'tampered Receipt',
      evidence: async () => {
        const response = structuredClone(await providerTurnReceiptListFixture())
        response.provider_turn_receipts[0]!.receipt_hash = '0'.repeat(64)
        return response
      },
    },
  ])('fails closed for $label', async ({ evidence: buildEvidence }) => {
    const candidate = await agentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(await buildEvidence()))
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderTurnReceipts(candidate))

    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.evidence).toBeNull()
    expect(result.current.error).toMatch(/Provider|Writing Agent/)
  })

  it('marks v1 history as legacy without requesting fabricated evidence', async () => {
    const candidate = await legacyAgentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderTurnReceipts(candidate))

    await waitFor(() => expect(result.current.state).toBe('legacy'))
    expect(result.current.evidence).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('keeps a missing v2 Receipt List visible and allows an explicit retry', async () => {
    const candidate = await agentCandidateOutputFixture()
    const evidence = await providerTurnReceiptListFixture()
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: 'not found' }, 404))
      .mockResolvedValueOnce(jsonResponse(evidence))
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useProviderTurnReceipts(candidate))
    await waitFor(() => expect(result.current.state).toBe('missing'))
    expect(result.current.error).toContain('缺少')

    act(() => result.current.refresh())

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
