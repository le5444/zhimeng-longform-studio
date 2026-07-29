import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getKnowledgeSourceFormalization,
  getProjectKnowledgeSourceFormalization,
} from '../api/knowledge'
import {
  cancelSourceImport,
  createLocalSourceImport,
  createSourceImport,
  getSourceImportExecutionReceipt,
  inspectLocalSource,
  listSourceImports,
  retrySourceImport,
  searchTomatoNovels,
} from '../api/sourceImports'
import {
  localSourceCandidateFixture,
  localSourceImportJobFixture,
  sourceImportFormalizationFixture,
  sourceImportJobFixture,
  sourceImportReceiptFixture,
  tomatoCandidateFixture,
} from '../test/sourceImportFixture'
import { useSourceImports } from './useSourceImports'

vi.mock('../api/sourceImports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/sourceImports')>()
  return {
    ...actual,
    cancelSourceImport: vi.fn(),
    createLocalSourceImport: vi.fn(),
    createSourceImport: vi.fn(),
    getSourceImportExecutionReceipt: vi.fn(),
    inspectLocalSource: vi.fn(),
    listSourceImports: vi.fn(),
    retrySourceImport: vi.fn(),
    searchTomatoNovels: vi.fn(),
  }
})

vi.mock('../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/knowledge')>()
  return {
    ...actual,
    getKnowledgeSourceFormalization: vi.fn(),
    getProjectKnowledgeSourceFormalization: vi.fn(),
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((next, fail) => {
    resolve = next
    reject = fail
  })
  return { promise, resolve, reject }
}

describe('useSourceImports', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(listSourceImports).mockResolvedValue([])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture(),
    )
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture(),
    )
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture(),
    )
  })

  it('creates an import immediately for one exact title match', async () => {
    const created = sourceImportJobFixture()
    vi.mocked(searchTomatoNovels).mockResolvedValue({
      schema: 'zhimeng.tomato-search.v1',
      query: tomatoCandidateFixture.title,
      resolution: 'auto_selected',
      candidates: [tomatoCandidateFixture],
      selected_candidate: tomatoCandidateFixture,
    })
    vi.mocked(createSourceImport).mockResolvedValue(created)
    const { result } = renderHook(() => useSourceImports())

    act(() => result.current.setQuery(`  ${tomatoCandidateFixture.title}  `))
    await act(async () => result.current.search())

    expect(searchTomatoNovels).toHaveBeenCalledWith(tomatoCandidateFixture.title)
    expect(createSourceImport).toHaveBeenCalledWith(
      tomatoCandidateFixture.title,
      tomatoCandidateFixture,
    )
    expect(result.current.searchState).toBe('created')
    expect(result.current.selected?.import_id).toBe(created.import_id)
  })

  it('inspects a local TXT before creating a hash-bound import job', async () => {
    const created = localSourceImportJobFixture()
    vi.mocked(inspectLocalSource).mockResolvedValue(localSourceCandidateFixture)
    vi.mocked(createLocalSourceImport).mockResolvedValue(created)
    const { result } = renderHook(() => useSourceImports())

    act(() => result.current.setLocalPath(`  ${localSourceCandidateFixture.source_path}  `))
    await act(async () => result.current.importLocalSource())

    expect(inspectLocalSource).toHaveBeenCalledWith(localSourceCandidateFixture.source_path)
    expect(createLocalSourceImport).toHaveBeenCalledWith(localSourceCandidateFixture)
    expect(result.current.localState).toBe('created')
    expect(result.current.localCandidate?.source_sha256).toBe(localSourceCandidateFixture.source_sha256)
    expect(result.current.selected).toMatchObject({
      import_id: created.import_id,
      provider: 'local_file',
    })
  })

  it('pauses on same-name candidates until the author chooses one', async () => {
    const second = { ...tomatoCandidateFixture, index: 2, book_id: '2222', author: '另一位作者' }
    vi.mocked(searchTomatoNovels).mockResolvedValue({
      schema: 'zhimeng.tomato-search.v1',
      query: tomatoCandidateFixture.title,
      resolution: 'selection_required',
      candidates: [tomatoCandidateFixture, second],
      selected_candidate: null,
    })
    vi.mocked(createSourceImport).mockResolvedValue(
      sourceImportJobFixture({ candidate: second }),
    )
    const { result } = renderHook(() => useSourceImports())

    act(() => result.current.setQuery(tomatoCandidateFixture.title))
    await act(async () => result.current.search())

    expect(result.current.searchState).toBe('selection_required')
    expect(createSourceImport).not.toHaveBeenCalled()

    await act(async () => result.current.chooseCandidate(second))
    expect(createSourceImport).toHaveBeenCalledWith(tomatoCandidateFixture.title, second)
    expect(result.current.searchState).toBe('created')
  })

  it('does not let an older search overwrite a newer query', async () => {
    const first = deferred<Awaited<ReturnType<typeof searchTomatoNovels>>>()
    const second = deferred<Awaited<ReturnType<typeof searchTomatoNovels>>>()
    vi.mocked(searchTomatoNovels)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => useSourceImports())

    act(() => result.current.setQuery('旧书名'))
    let firstSearch!: Promise<void>
    act(() => {
      firstSearch = result.current.search()
    })
    act(() => result.current.setQuery('新书名'))
    let secondSearch!: Promise<void>
    act(() => {
      secondSearch = result.current.search()
    })

    second.resolve({
      schema: 'zhimeng.tomato-search.v1',
      query: '新书名',
      resolution: 'not_found',
      candidates: [],
      selected_candidate: null,
    })
    await act(async () => secondSearch)
    first.resolve({
      schema: 'zhimeng.tomato-search.v1',
      query: '旧书名',
      resolution: 'selection_required',
      candidates: [tomatoCandidateFixture, { ...tomatoCandidateFixture, book_id: '9876' }],
      selected_candidate: null,
    })
    await act(async () => firstSearch)

    expect(result.current.searchQuery).toBe('新书名')
    expect(result.current.searchState).toBe('not_found')
    expect(result.current.candidates).toEqual([])
  })

  it('does not let a list started before create erase the new job', async () => {
    const oldList = deferred<Awaited<ReturnType<typeof listSourceImports>>>()
    vi.mocked(listSourceImports).mockReturnValueOnce(oldList.promise)
    const created = sourceImportJobFixture({ status: 'awaiting_review' })
    vi.mocked(searchTomatoNovels).mockResolvedValue({
      schema: 'zhimeng.tomato-search.v1',
      query: tomatoCandidateFixture.title,
      resolution: 'auto_selected',
      candidates: [tomatoCandidateFixture],
      selected_candidate: tomatoCandidateFixture,
    })
    vi.mocked(createSourceImport).mockResolvedValue(created)
    const { result } = renderHook(() => useSourceImports())

    act(() => result.current.setQuery(tomatoCandidateFixture.title))
    await act(async () => result.current.search())
    expect(result.current.selected?.import_id).toBe(created.import_id)

    oldList.resolve([])
    await act(async () => {
      await oldList.promise
    })

    expect(result.current.jobs).toHaveLength(1)
    expect(result.current.selected?.import_id).toBe(created.import_id)
  })

  it('polls active work without overlap and stops at awaiting_review', async () => {
    vi.useFakeTimers()
    const queued = sourceImportJobFixture({
      status: 'downloading',
      savedChapters: 12,
      totalChapters: 100,
      percent: 12,
    })
    const awaiting = sourceImportJobFixture({ status: 'awaiting_review', percent: 100 })
    vi.mocked(listSourceImports)
      .mockResolvedValueOnce([queued])
      .mockResolvedValueOnce([awaiting])
    const { result } = renderHook(() => useSourceImports())

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(listSourceImports).toHaveBeenCalledTimes(1)
    expect(result.current.selected?.status).toBe('downloading')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500)
    })
    expect(listSourceImports).toHaveBeenCalledTimes(2)
    expect(result.current.selected?.status).toBe('awaiting_review')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(listSourceImports).toHaveBeenCalledTimes(2)
  })

  it.each([
    'awaiting_review',
    'formalizing',
    'completed',
  ] as const)('loads both receipt chains for a selected %s job', async (status) => {
    const job = sourceImportJobFixture({ status })
    vi.mocked(listSourceImports).mockResolvedValue([job])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture({ importId: job.import_id }),
    )
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({
        importId: job.import_id,
        status: status === 'completed' ? 'verified' : 'awaiting_review',
      }),
    )

    const { result } = renderHook(() => useSourceImports())

    await waitFor(() => expect(result.current.evidenceState).toBe('ready'))
    expect(getSourceImportExecutionReceipt).toHaveBeenCalledWith(job.import_id)
    expect(getKnowledgeSourceFormalization).toHaveBeenCalledWith(job.import_id)
    expect(result.current.executionReceipt?.source_import_id).toBe(job.import_id)
    expect(result.current.formalization?.source_import_id).toBe(job.import_id)
  })

  it('loads formalization through the project-scoped endpoint when a project is active', async () => {
    const projectId = `project-${'8'.repeat(24)}`
    const job = sourceImportJobFixture({ status: 'awaiting_review' })
    vi.mocked(listSourceImports).mockResolvedValue([job])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture({ importId: job.import_id }),
    )
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: job.import_id }),
    )

    const { result } = renderHook(() => useSourceImports(true, projectId))

    await waitFor(() => expect(result.current.evidenceState).toBe('ready'))
    expect(getProjectKnowledgeSourceFormalization).toHaveBeenCalledWith(projectId, job.import_id)
    expect(getKnowledgeSourceFormalization).not.toHaveBeenCalled()
    expect(result.current.formalization?.source_import_id).toBe(job.import_id)
  })

  it('keeps the legacy application endpoint when no project is active', async () => {
    const job = sourceImportJobFixture({ status: 'awaiting_review' })
    vi.mocked(listSourceImports).mockResolvedValue([job])
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: job.import_id }),
    )

    const { result } = renderHook(() => useSourceImports(true, null))

    await waitFor(() => expect(result.current.evidenceState).toBe('ready'))
    expect(getKnowledgeSourceFormalization).toHaveBeenCalledWith(job.import_id)
    expect(getProjectKnowledgeSourceFormalization).not.toHaveBeenCalled()
  })

  it('clears stale evidence and ignores an old project warning after project switch', async () => {
    const oldProjectId = `project-${'1'.repeat(24)}`
    const nextProjectId = `project-${'2'.repeat(24)}`
    const job = sourceImportJobFixture({ status: 'awaiting_review' })
    const oldReceipt = deferred<Awaited<ReturnType<typeof getSourceImportExecutionReceipt>>>()
    const oldFormalization = deferred<Awaited<ReturnType<typeof getProjectKnowledgeSourceFormalization>>>()
    const nextReceipt = deferred<Awaited<ReturnType<typeof getSourceImportExecutionReceipt>>>()
    const nextFormalization = deferred<Awaited<ReturnType<typeof getProjectKnowledgeSourceFormalization>>>()
    vi.mocked(listSourceImports).mockResolvedValue([job])
    vi.mocked(getSourceImportExecutionReceipt)
      .mockReturnValueOnce(oldReceipt.promise)
      .mockReturnValueOnce(nextReceipt.promise)
    vi.mocked(getProjectKnowledgeSourceFormalization)
      .mockReturnValueOnce(oldFormalization.promise)
      .mockReturnValueOnce(nextFormalization.promise)

    const { result, rerender } = renderHook(
      ({ projectId }) => useSourceImports(true, projectId),
      { initialProps: { projectId: oldProjectId } },
    )

    await waitFor(() => expect(getProjectKnowledgeSourceFormalization).toHaveBeenCalledWith(oldProjectId, job.import_id))
    rerender({ projectId: nextProjectId })
    await waitFor(() => expect(result.current.evidenceState).toBe('loading'))
    expect(result.current.executionReceipt).toBeNull()
    expect(result.current.formalization).toBeNull()

    await waitFor(() => expect(getProjectKnowledgeSourceFormalization).toHaveBeenCalledWith(nextProjectId, job.import_id))
    await act(async () => {
      oldReceipt.resolve(sourceImportReceiptFixture({ importId: job.import_id }))
      oldFormalization.reject(new Error('source import is bound to another knowledge scope'))
      nextReceipt.resolve(sourceImportReceiptFixture({ importId: job.import_id }))
      nextFormalization.resolve(sourceImportFormalizationFixture({
        importId: job.import_id,
        status: 'awaiting_review',
      }))
      await nextReceipt.promise
      await nextFormalization.promise
    })

    await waitFor(() => expect(result.current.evidenceState).toBe('ready'))
    expect(result.current.evidenceError).toBeNull()
    expect(result.current.formalization?.source_import_id).toBe(job.import_id)
  })

  it('does not let receipts from an older selected job replace the current evidence', async () => {
    const firstJob = sourceImportJobFixture({ status: 'awaiting_review' })
    const secondJob = sourceImportJobFixture({
      importId: 'source-import-fedcba9876543210fedcba9876543210',
      status: 'completed',
    })
    const firstSource = deferred<Awaited<ReturnType<typeof getSourceImportExecutionReceipt>>>()
    const firstFormalization = deferred<Awaited<ReturnType<typeof getKnowledgeSourceFormalization>>>()
    const secondSource = deferred<Awaited<ReturnType<typeof getSourceImportExecutionReceipt>>>()
    const secondFormalization = deferred<Awaited<ReturnType<typeof getKnowledgeSourceFormalization>>>()
    vi.mocked(listSourceImports).mockResolvedValue([firstJob, secondJob])
    vi.mocked(getSourceImportExecutionReceipt).mockImplementation((importId) => (
      importId === firstJob.import_id ? firstSource.promise : secondSource.promise
    ))
    vi.mocked(getKnowledgeSourceFormalization).mockImplementation((importId) => (
      importId === firstJob.import_id ? firstFormalization.promise : secondFormalization.promise
    ))
    const { result } = renderHook(() => useSourceImports())

    await waitFor(() => expect(result.current.selectedId).toBe(firstJob.import_id))
    act(() => result.current.select(secondJob.import_id))
    await waitFor(() => expect(getSourceImportExecutionReceipt).toHaveBeenCalledWith(secondJob.import_id))

    await act(async () => {
      secondSource.resolve(sourceImportReceiptFixture({ importId: secondJob.import_id }))
      secondFormalization.resolve(sourceImportFormalizationFixture({
        importId: secondJob.import_id,
        status: 'verified',
      }))
      await secondSource.promise
      await secondFormalization.promise
    })
    await waitFor(() => expect(result.current.evidenceState).toBe('ready'))

    await act(async () => {
      firstSource.resolve(sourceImportReceiptFixture({ importId: firstJob.import_id }))
      firstFormalization.resolve(sourceImportFormalizationFixture({ importId: firstJob.import_id }))
      await firstSource.promise
      await firstFormalization.promise
    })

    expect(result.current.selectedId).toBe(secondJob.import_id)
    expect(result.current.executionReceipt?.source_import_id).toBe(secondJob.import_id)
    expect(result.current.formalization?.source_import_id).toBe(secondJob.import_id)
  })

  it('cancels active work and retries a failed job as a new trace', async () => {
    const running = sourceImportJobFixture({ status: 'downloading' })
    const cancelRequested = sourceImportJobFixture({ status: 'cancel_requested' })
    vi.mocked(listSourceImports).mockResolvedValue([running])
    vi.mocked(cancelSourceImport).mockResolvedValue(cancelRequested)
    const first = renderHook(() => useSourceImports())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    await act(async () => first.result.current.cancel())
    expect(cancelSourceImport).toHaveBeenCalledWith(running.import_id)
    expect(first.result.current.selected?.status).toBe('cancel_requested')
    first.unmount()

    const failed = sourceImportJobFixture({ status: 'failed', error: 'download failed' })
    const retried = sourceImportJobFixture({
      importId: 'source-import-fedcba9876543210fedcba9876543210',
      status: 'queued',
      retryOf: failed.import_id,
    })
    vi.mocked(listSourceImports).mockResolvedValue([failed])
    vi.mocked(retrySourceImport).mockResolvedValue(retried)
    const second = renderHook(() => useSourceImports())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    await act(async () => second.result.current.retry())
    expect(retrySourceImport).toHaveBeenCalledWith(failed.import_id)
    expect(second.result.current.selected?.import_id).toBe(retried.import_id)
    expect(second.result.current.selected?.retry_of).toBe(failed.import_id)
  })
})
