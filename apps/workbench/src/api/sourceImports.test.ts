import { afterEach, describe, expect, it, vi } from 'vitest'
import { RuntimeApiError } from './runtime'
import {
  SOURCE_IMPORT_SEARCH_TIMEOUT_MS,
  cancelSourceImport,
  createLocalSourceImport,
  createSourceImport,
  getSourceImport,
  getSourceImportExecutionReceipt,
  inspectLocalSource,
  listSourceImports,
  prepareSourceImportDistillationInvocation,
  retrySourceImport,
  searchTomatoNovels,
} from './sourceImports'
import {
  distillationInvocationSnapshotFixture,
} from '../test/distillationInvocationFixture'
import {
  localEpubSourceCandidateFixture,
  sourceImportReceiptFixture,
} from '../test/sourceImportFixture'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const candidate = {
  index: 1,
  book_id: '2001',
  title: '目标书',
  author: '作者甲',
}

describe('source import API client', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('uses the expected routes, methods, bodies, and encoded import IDs', async () => {
    const listedImport = { import_id: 'source-import-one' }
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ candidates: [candidate] }))
      .mockResolvedValueOnce(jsonResponse(listedImport))
      .mockResolvedValueOnce(jsonResponse(localEpubSourceCandidateFixture))
      .mockResolvedValueOnce(jsonResponse(listedImport))
      .mockResolvedValueOnce(jsonResponse({ imports: [listedImport] }))
      .mockResolvedValueOnce(jsonResponse(listedImport))
      .mockResolvedValueOnce(jsonResponse(listedImport))
      .mockResolvedValueOnce(jsonResponse(listedImport))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.source-import-execution-receipt-record.v1',
        source_import_id: 'source-import/a b?',
        lineage_status: 'legacy_unverified',
        receipt: null,
      }))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.source-import-distillation-prepare-result.v1',
        snapshot: distillationInvocationSnapshotFixture({ status: 'prepared' }),
        anchor_catalog: { schema: 'zhimeng.distillation-anchor-catalog.v1' },
        text_by_anchor_id: {
          [`distillation-anchor-${'a'.repeat(32)}`]: '正文锚点',
        },
      }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const importId = 'source-import/a b?'

    await searchTomatoNovels('目标书', options)
    await createSourceImport('目标书', candidate, options)
    await inspectLocalSource(localEpubSourceCandidateFixture.source_path, options)
    await createLocalSourceImport(localEpubSourceCandidateFixture, options)
    const imports = await listSourceImports(options)
    await getSourceImport(importId, options)
    await cancelSourceImport(importId, options)
    await retrySourceImport(importId, options)
    await getSourceImportExecutionReceipt(importId, options)
    await prepareSourceImportDistillationInvocation(
      importId,
      {
        project_id: 'project-one',
        expected_project_revision: 7,
        expected_project_fingerprint: 'f'.repeat(64),
      },
      options,
    )

    expect(imports).toEqual([listedImport])
    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined
        ? undefined
        : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/tomato/search',
        method: 'POST',
        body: { query: '目标书' },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports',
        method: 'POST',
        body: { provider: 'tomato', query: '目标书', candidate },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/local/inspect',
        method: 'POST',
        body: { source_path: localEpubSourceCandidateFixture.source_path },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports',
        method: 'POST',
        body: {
          provider: 'local_file',
          query: localEpubSourceCandidateFixture.source_path,
          candidate: localEpubSourceCandidateFixture,
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/source-import%2Fa%20b%3F',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/source-import%2Fa%20b%3F/cancel',
        method: 'POST',
        body: {},
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/source-import%2Fa%20b%3F/retry',
        method: 'POST',
        body: {},
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/source-import%2Fa%20b%3F/execution-receipt',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/source-imports/source-import%2Fa%20b%3F/distillation-invocation/prepare',
        method: 'POST',
        body: {
          project_id: 'project-one',
          expected_project_revision: 7,
          expected_project_fingerprint: 'f'.repeat(64),
        },
      },
    ])
  })

  it('waits for the full 300-second search timeout before aborting', async () => {
    vi.useFakeTimers()
    let signal: AbortSignal | undefined
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
      signal = init?.signal as AbortSignal
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          const error = new Error('The operation was aborted')
          error.name = 'AbortError'
          reject(error)
        }, { once: true })
      })
    })

    const outcome = searchTomatoNovels('目标书', { fetcher })
      .catch((error: unknown) => error)

    expect(SOURCE_IMPORT_SEARCH_TIMEOUT_MS).toBe(300_000)
    expect(signal?.aborted).toBe(false)

    await vi.advanceTimersByTimeAsync(299_999)
    expect(signal?.aborted).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    expect(signal?.aborted).toBe(true)
    await expect(outcome).resolves.toMatchObject({ name: 'AbortError' })
  })

  it('returns the runtime-owned execution identity from the receipt endpoint', async () => {
    const record = sourceImportReceiptFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(record))

    const result = await getSourceImportExecutionReceipt(record.source_import_id, { fetcher })

    expect(result.receipt).toMatchObject({
      run_id: expect.stringMatching(/^source-import-run-/),
      revision: 1,
      idempotency_key: expect.stringMatching(/^source-import-idempotency-/),
    })
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining('/execution-receipt'),
      expect.any(Object),
    )
  })

  it('throws RuntimeApiError with the status and JSON payload from a 502 response', async () => {
    const payload = {
      error: {
        code: 'upstream_unavailable',
        message: 'Tomato search is unavailable',
      },
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(payload, 502),
    )

    try {
      await searchTomatoNovels('目标书', { fetcher })
      expect.fail('Expected searchTomatoNovels to reject')
    } catch (error) {
      expect(error).toBeInstanceOf(RuntimeApiError)
      expect(error).toMatchObject({ status: 502, payload })
    }
  })
})
