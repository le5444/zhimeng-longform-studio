import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAgentCandidateOutput } from './useAgentCandidateOutput'

function notFoundResponse(): Response {
  return new Response(JSON.stringify({
    schema: 'zhimeng.error.v1',
    error: {
      code: 'agent_resource_not_found',
      message: 'Agent candidate output was not found',
      retryable: false,
    },
  }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useAgentCandidateOutput', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('treats a 404 as an empty candidate state instead of an error', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(notFoundResponse())
    vi.stubGlobal('fetch', fetcher)

    const { result } = renderHook(() => useAgentCandidateOutput(
      'run-0123456789abcdef0123456789',
      'succeeded',
    ))

    await waitFor(() => expect(result.current.state).toBe('missing'))
    expect(result.current.output).toBeNull()
    expect(result.current.error).toBeNull()
    expect(fetcher.mock.calls[0]?.[0]).toContain('/api/v1/agent/runs/run-0123456789abcdef0123456789/candidate-output')
  })
})
