import { describe, expect, it, vi } from 'vitest'
import {
  DISTILLATION_PREVIEW_TIMEOUT_MS,
  fetchDistillationHealth,
  getDistillationInvocation,
  isRuntimeDistillationInvocationId,
  listDistillationInvocationReceipts,
  previewDistillationCoverage,
  runDistillationInvocation,
} from './distillation'
import {
  distillationCoverageFixture,
  distillationHealthFixture,
} from '../test/distillationFixture'
import {
  distillationInvocationSnapshotFixture,
  runtimeDistillationInvocationId,
} from '../test/distillationInvocationFixture'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('distillation API', () => {
  it('keeps the client deadline later than the Runtime maximum', () => {
    expect(DISTILLATION_PREVIEW_TIMEOUT_MS).toBe(610_000)
  })

  it('reads health from the fixed Runtime endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(distillationHealthFixture),
    )

    const result = await fetchDistillationHealth({
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })

    expect(result.health).toBe('degraded')
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/distillation/health',
      expect.any(Object),
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBeUndefined()
  })

  it('sends only query and profile for a coverage preview', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(distillationCoverageFixture),
    )

    await previewDistillationCoverage(
      '第一章 爽点兑现',
      'draft_assist',
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )

    const init = fetcher.mock.calls[0]?.[1]
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/distillation/coverage',
    )
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      query: '第一章 爽点兑现',
      profile: 'draft_assist',
    })
    expect(String(init?.body)).not.toMatch(/library|path|top/i)
  })

  it('recognizes only Runtime-owned deep distillation invocation identities', () => {
    expect(isRuntimeDistillationInvocationId(runtimeDistillationInvocationId)).toBe(true)
    expect(isRuntimeDistillationInvocationId(`source-import-invocation-${'2'.repeat(32)}`)).toBe(false)
    expect(isRuntimeDistillationInvocationId(null)).toBe(false)
  })

  it('reads a Runtime-owned deep distillation invocation snapshot', async () => {
    const snapshot = distillationInvocationSnapshotFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(snapshot))

    const result = await getDistillationInvocation(runtimeDistillationInvocationId, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })

    expect(result.invocation.status).toBe('output_persisted')
    expect(result.receipts).toHaveLength(4)
    expect(fetcher).toHaveBeenCalledWith(
      `http://127.0.0.1:8787/api/v1/distillation/invocations/${runtimeDistillationInvocationId}`,
      expect.any(Object),
    )
  })

  it('reads the append-only distillation invocation receipt ledger', async () => {
    const snapshot = distillationInvocationSnapshotFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      schema: 'zhimeng.distillation-invocation-receipt-list.v1',
      invocation_id: runtimeDistillationInvocationId,
      receipts: snapshot.receipts,
    }))

    const result = await listDistillationInvocationReceipts(
      runtimeDistillationInvocationId,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )

    expect(result.receipts.map((item) => item.kind)).toEqual([
      'prepared',
      'provider_dispatch',
      'provider_turn_result',
      'candidate_output',
    ])
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/distillation/invocations/${runtimeDistillationInvocationId}/receipts`,
    )
  })

  it('starts a prepared deep distillation invocation only through the Runtime run endpoint', async () => {
    const snapshot = distillationInvocationSnapshotFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      ...snapshot,
      terminal_reason: 'distillation_output_persisted',
    }))
    const request = {
      anchor_catalog: {
        schema: 'zhimeng.distillation-anchor-catalog.v1',
        invocation_id: runtimeDistillationInvocationId,
      },
      text_by_anchor_id: {
        'distillation-anchor-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa': '正文锚点',
      },
    }

    const result = await runDistillationInvocation(
      runtimeDistillationInvocationId,
      request,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )

    const init = fetcher.mock.calls[0]?.[1]
    expect(result.terminal_reason).toBe('distillation_output_persisted')
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/distillation/invocations/${runtimeDistillationInvocationId}/run`,
    )
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual(request)
  })
})
