import { describe, expect, it, vi } from 'vitest'
import {
  approveAdjacentChain,
  disableAdjacentChain,
  getAdjacentChain,
  prepareAdjacentChain,
  rejectAdjacentChain,
  runAdjacentChain,
} from './adjacentChains'
import { adjacentChainResponseFixture } from '../test/adjacentChainFixture'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('adjacent chain API client', () => {
  it('uses project-scoped O2-D→O2-E chain routes and CAS bodies', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(adjacentChainResponseFixture()))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const projectId = 'project/a b?'
    const chainId = `adjacent-chain-${'a'.repeat(32)}`

    await getAdjacentChain(projectId, options)
    await prepareAdjacentChain(projectId, {
      expected_project_revision: 9,
      expected_project_fingerprint: 'f'.repeat(64),
      source_import_id: `source-import-${'d'.repeat(32)}`,
      task_intent: '连续执行 O2-D 到 O2-E',
    }, options)
    await approveAdjacentChain(projectId, chainId, options)
    await rejectAdjacentChain(projectId, chainId, options)
    await disableAdjacentChain(projectId, chainId, options)
    await runAdjacentChain(projectId, chainId, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context/prepare',
        method: 'POST',
        body: {
          expected_project_revision: 9,
          expected_project_fingerprint: 'f'.repeat(64),
          source_import_id: `source-import-${'d'.repeat(32)}`,
          task_intent: '连续执行 O2-D 到 O2-E',
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context/approve',
        method: 'POST',
        body: { expected_chain_id: chainId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context/reject',
        method: 'POST',
        body: { expected_chain_id: chainId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context/disable',
        method: 'POST',
        body: { expected_chain_id: chainId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/adjacent-skill-chains/novel-distillation-kb-to-context/run',
        method: 'POST',
        body: { expected_chain_id: chainId },
      },
    ])
  })

  it('extends timeout for chained adapter execution', async () => {
    let timeoutSignal: AbortSignal | undefined
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
      timeoutSignal = init?.signal as AbortSignal
      return Promise.resolve(jsonResponse(adjacentChainResponseFixture({ status: 'succeeded' })))
    })

    await runAdjacentChain('project-adjacent', `adjacent-chain-${'a'.repeat(32)}`, {
      fetcher,
      timeoutMs: 1,
    })

    expect(timeoutSignal).toBeDefined()
  })
})
