import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDistillationHealth,
  previewDistillationCoverage,
} from '../api/distillation'
import {
  distillationCoverageFixture,
  distillationHealthFixture,
} from '../test/distillationFixture'
import { useDistillationPreview } from './useDistillationPreview'

vi.mock('../api/distillation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/distillation')>()
  return {
    ...actual,
    fetchDistillationHealth: vi.fn(),
    previewDistillationCoverage: vi.fn(),
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => {
    resolve = next
  })
  return { promise, resolve }
}

describe('useDistillationPreview', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(fetchDistillationHealth).mockResolvedValue(distillationHealthFixture)
  })

  it('loads library health and runs a fixed-profile coverage preview', async () => {
    vi.mocked(previewDistillationCoverage).mockResolvedValue(distillationCoverageFixture)
    const { result } = renderHook(() => useDistillationPreview())

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.health?.card_count).toBe(1_124)

    act(() => result.current.setQuery('  第一章 爽点兑现  '))
    await act(async () => result.current.runCoverage())

    expect(previewDistillationCoverage).toHaveBeenCalledWith(
      '第一章 爽点兑现',
      'draft_assist',
    )
    expect(result.current.coverage?.formal_coverage_ratio).toBe(0.4)
  })

  it('does not let an old long-running result overwrite a changed query', async () => {
    const oldResult = deferred<typeof distillationCoverageFixture>()
    vi.mocked(previewDistillationCoverage).mockReturnValue(oldResult.promise)
    const { result } = renderHook(() => useDistillationPreview())

    act(() => result.current.setQuery('旧目标'))
    let running!: Promise<void>
    act(() => {
      running = result.current.runCoverage()
    })
    act(() => result.current.setQuery('新目标'))

    oldResult.resolve(distillationCoverageFixture)
    await act(async () => running)

    expect(result.current.query).toBe('新目标')
    expect(result.current.coverage).toBeNull()
    expect(result.current.coverageState).toBe('idle')
  })

  it('stays dormant while Runtime is unavailable', async () => {
    const { result } = renderHook(() => useDistillationPreview(false))
    await act(async () => Promise.resolve())

    expect(result.current.healthState).toBe('idle')
    expect(fetchDistillationHealth).not.toHaveBeenCalled()
    expect(previewDistillationCoverage).not.toHaveBeenCalled()
  })

  it('rejects a coverage result from a different library or tool version', async () => {
    vi.mocked(previewDistillationCoverage).mockResolvedValue({
      ...distillationCoverageFixture,
      library_fingerprint: 'c'.repeat(64),
    })
    const { result } = renderHook(() => useDistillationPreview())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    act(() => result.current.setQuery('第一章 爽点兑现'))
    await act(async () => result.current.runCoverage())

    expect(result.current.coverage).toBeNull()
    expect(result.current.coverageState).toBe('error')
    expect(result.current.coverageError).toContain('版本已变化')
    expect(result.current.health).toBeNull()
  })
})
