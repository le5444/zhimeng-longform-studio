import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAgentConversation } from './useAgentConversation'

const checkpoint = { schema: 'zhimeng.session-checkpoint.v1', checkpoint_id: 'cp-1', session_id: 's1', run_id: null,
  through_ordinal: 2, source_message_count: 2, content: 'Keep the original decision.', content_hash: 'a'.repeat(64), reported_model: null, created_at: '2026-09-16T00:00:00Z' }
const options = { enabled: true, projectId: 'p1', projectRevision: 1, projectFingerprint: 'a'.repeat(64), openFilePath: null }
function json(value: unknown) { return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } }) }
function setup() {
  let resolveCompact!: (response: Response) => void
  const compactResponse = new Promise<Response>((resolve) => { resolveCompact = resolve })
  const fetcher = vi.fn<typeof fetch>(async (input) => {
    const url = String(input)
    if (url.endsWith('/agent/status')) return json({ available: true, detail: '', executor: {}, capabilities: {} })
    if (url.endsWith('/compact')) return compactResponse
    if (url.endsWith('/checkpoint')) return json(null)
    if (url.endsWith('/agent/sessions')) {
      const id = url.includes('/p2/') ? 's2' : 's1'
      return json({ sessions: [{ session_id: id, title: id, status: 'idle', project_id: id === 's1' ? 'p1' : 'p2', project_revision: null,
        updated_at: '2026-09-16T00:00:00Z', capabilities: {}, executor: {} }] })
    }
    if (url.includes('/messages?')) return json({ messages: [], last_ordinal: 4 })
    if (url.includes('/runs?')) return json({ runs: [] })
    if (url.includes('/context-packs?')) return json({ context_packs: [] })
    throw new Error(`Unexpected ${url}`)
  })
  vi.stubGlobal('fetch', fetcher)
  return { fetcher, resolveCompact }
}
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('conversation compaction lifecycle', () => {
  it('blocks duplicate requests to the compacting session, then exposes the durable summary', async () => {
    const { fetcher, resolveCompact } = setup()
    const { result } = renderHook(() => useAgentConversation(options))
    await waitFor(() => expect(result.current.session?.session_id).toBe('s1'))
    let pending!: Promise<unknown>
    act(() => { pending = result.current.compactConversation() })
    expect(result.current.busy).toBe(true)
    expect(result.current.contextState).toBe('compacting')
    await expect(result.current.compactConversation()).rejects.toThrow()
    await expect(result.current.send({ text: '并发发送' })).rejects.toThrow()
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/compact'))).toHaveLength(1)
    await act(async () => { resolveCompact(json(checkpoint)); await pending })
    expect(result.current.latestCheckpoint?.content).toBe(checkpoint.content)
    expect(result.current.busy).toBe(false)
  })

  it('does not copy a late summary into another project after switching scope', async () => {
    const { resolveCompact } = setup()
    const { result, rerender } = renderHook(({ projectId }) => useAgentConversation({ ...options, projectId }), { initialProps: { projectId: 'p1' } })
    await waitFor(() => expect(result.current.session?.session_id).toBe('s1'))
    let pending!: Promise<unknown>
    act(() => { pending = result.current.compactConversation().catch(() => undefined) })
    rerender({ projectId: 'p2' })
    await waitFor(() => expect(result.current.session?.session_id).toBe('s2'))
    await act(async () => { resolveCompact(json(checkpoint)); await pending })
    expect(result.current.latestCheckpoint).toBeNull()
    expect(result.current.contextState).toBe('idle')
  })

  it('restores the durable summary without asking for context packs', async () => {
    const { fetcher } = setup()
    const original = fetcher.getMockImplementation()!
    fetcher.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/checkpoint')) return json(checkpoint)
      return original(input, init)
    })
    const { result } = renderHook(() => useAgentConversation(options))
    await waitFor(() => expect(result.current.session?.session_id).toBe('s1'))
    await waitFor(() => expect(result.current.latestCheckpoint?.checkpoint_id).toBe('cp-1'))
    expect(result.current.contextState).toBe('idle')
    expect(fetcher.mock.calls.some(([url]) => String(url).includes('/context-packs'))).toBe(false)
  })
})
