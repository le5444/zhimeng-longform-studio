import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  approveAdjacentChain,
  disableAdjacentChain,
  getAdjacentChain,
  prepareAdjacentChain,
  rejectAdjacentChain,
  runAdjacentChain,
} from '../api/adjacentChains'
import { adjacentChainResponseFixture } from '../test/adjacentChainFixture'
import { useAdjacentChain } from './useAdjacentChain'

vi.mock('../api/adjacentChains', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/adjacentChains')>()
  return {
    ...actual,
    getAdjacentChain: vi.fn(),
    prepareAdjacentChain: vi.fn(),
    approveAdjacentChain: vi.fn(),
    rejectAdjacentChain: vi.fn(),
    disableAdjacentChain: vi.fn(),
    runAdjacentChain: vi.fn(),
  }
})

describe('useAdjacentChain', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(getAdjacentChain).mockResolvedValue(adjacentChainResponseFixture())
    vi.mocked(prepareAdjacentChain).mockResolvedValue(adjacentChainResponseFixture({ status: 'prepared' }))
    vi.mocked(approveAdjacentChain).mockResolvedValue(adjacentChainResponseFixture({ status: 'approved' }))
    vi.mocked(rejectAdjacentChain).mockResolvedValue(adjacentChainResponseFixture({ status: 'rejected' }))
    vi.mocked(disableAdjacentChain).mockResolvedValue(adjacentChainResponseFixture({ status: 'disabled' }))
    vi.mocked(runAdjacentChain).mockResolvedValue(adjacentChainResponseFixture({
      status: 'succeeded',
      context_pack_id: 'context-pack-o2e-fixture',
      context_pack_hash: '9'.repeat(64),
    }))
  })

  it('loads the current project chain and clears state when disabled', async () => {
    const { result, rerender } = renderHook(
      ({ enabled, projectId }) => useAdjacentChain(enabled, projectId, 3, 'f'.repeat(64)),
      { initialProps: { enabled: true, projectId: 'project-adjacent' as string | null } },
    )

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(getAdjacentChain).toHaveBeenCalledWith('project-adjacent')
    expect(result.current.record?.chain_id).toMatch(/^adjacent-chain-/)

    rerender({ enabled: false, projectId: 'project-adjacent' })
    await waitFor(() => expect(result.current.state).toBe('idle'))
    expect(result.current.record).toBeNull()
  })

  it('prepares with project revision/fingerprint and source import identity', async () => {
    const sourceImportId = `source-import-${'d'.repeat(32)}`
    const { result } = renderHook(() => useAdjacentChain(true, 'project-adjacent', 7, 'f'.repeat(64)))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => result.current.prepare(sourceImportId))

    expect(prepareAdjacentChain).toHaveBeenCalledWith('project-adjacent', {
      expected_project_revision: 7,
      expected_project_fingerprint: 'f'.repeat(64),
      source_import_id: sourceImportId,
      profile: 'chapter',
      stage: 'draft',
      chapter_id: 'CH001',
      task_intent: '连续执行 O2-D 到 O2-E，只生成写前 Context Pack。',
    })
  })

  it('approves and runs against the current chain id', async () => {
    const { result } = renderHook(() => useAdjacentChain(true, 'project-adjacent', 3, 'f'.repeat(64)))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => result.current.approve())
    await act(async () => result.current.run())

    const chainId = adjacentChainResponseFixture().record.chain_id
    expect(approveAdjacentChain).toHaveBeenCalledWith('project-adjacent', chainId)
    expect(runAdjacentChain).toHaveBeenCalledWith('project-adjacent', chainId)
  })
})
