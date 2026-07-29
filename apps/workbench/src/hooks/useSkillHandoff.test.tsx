import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  approveSkillHandoff,
  disableSkillHandoff,
  getSkillHandoff,
  prepareSkillHandoff,
  rejectSkillHandoff,
  runSkillHandoff,
} from '../api/skillHandoffs'
import { RuntimeApiError } from '../api/runtime'
import { skillHandoffResponseFixture } from '../test/skillHandoffFixture'
import { useSkillHandoff } from './useSkillHandoff'

vi.mock('../api/skillHandoffs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/skillHandoffs')>()
  return {
    ...actual,
    getSkillHandoff: vi.fn(),
    prepareSkillHandoff: vi.fn(),
    approveSkillHandoff: vi.fn(),
    rejectSkillHandoff: vi.fn(),
    disableSkillHandoff: vi.fn(),
    runSkillHandoff: vi.fn(),
  }
})

describe('useSkillHandoff', () => {
  afterEach(() => {
    cleanup()
  })

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(getSkillHandoff).mockResolvedValue(skillHandoffResponseFixture())
    vi.mocked(prepareSkillHandoff).mockResolvedValue(skillHandoffResponseFixture({ status: 'prepared' }))
    vi.mocked(approveSkillHandoff).mockResolvedValue(skillHandoffResponseFixture({ status: 'approved' }))
    vi.mocked(rejectSkillHandoff).mockResolvedValue(skillHandoffResponseFixture({ status: 'rejected' }))
    vi.mocked(disableSkillHandoff).mockResolvedValue(skillHandoffResponseFixture({ status: 'disabled' }))
    vi.mocked(runSkillHandoff).mockResolvedValue(skillHandoffResponseFixture({
      status: 'succeeded',
      skill_receipt_id: `skill-receipt-${'1'.repeat(32)}`,
      skill_receipt_hash: '2'.repeat(64),
      result: {
        schema: 'zhimeng.novel-kb-handoff-result.v1',
        status: 'ready_for_downstream',
        distillation_index_hash: '3'.repeat(64),
        downstream_target: 'novel-kb-manager.import-distillation-index',
        counts: { work: 1, voice: 1, validator: 1 },
      },
    }))
  })

  it('loads the current project handoff and clears state when disabled', async () => {
    const { result, rerender } = renderHook(
      ({ enabled, projectId }) => useSkillHandoff(enabled, projectId, 3, 'f'.repeat(64)),
      { initialProps: { enabled: true, projectId: 'project-o2d' as string | null } },
    )

    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(getSkillHandoff).toHaveBeenCalledWith('project-o2d')
    expect(result.current.record?.handoff_id).toMatch(/^skill-handoff-/)

    rerender({ enabled: false, projectId: 'project-o2d' })
    await waitFor(() => expect(result.current.state).toBe('idle'))
    expect(result.current.record).toBeNull()
  })

  it('prepares with project revision/fingerprint and source import identity', async () => {
    const sourceImportId = `source-import-${'d'.repeat(32)}`
    const { result } = renderHook(() => useSkillHandoff(true, 'project-o2d', 7, 'f'.repeat(64)))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => result.current.prepare(sourceImportId))

    expect(prepareSkillHandoff).toHaveBeenCalledWith('project-o2d', {
      expected_project_revision: 7,
      expected_project_fingerprint: 'f'.repeat(64),
      source_import_id: sourceImportId,
    })
    expect(result.current.record?.status).toBe('prepared')
  })

  it('keeps author decision actions bound to the latest handoff id', async () => {
    const handoffId = `skill-handoff-${'a'.repeat(32)}`
    const { result } = renderHook(() => useSkillHandoff(true, 'project-o2d', 7, 'f'.repeat(64)))
    await waitFor(() => expect(result.current.record?.handoff_id).toBe(handoffId))

    await act(async () => result.current.approve())
    expect(approveSkillHandoff).toHaveBeenCalledWith('project-o2d', handoffId)
    expect(result.current.record?.status).toBe('approved')

    await act(async () => result.current.run())
    expect(runSkillHandoff).toHaveBeenCalledWith('project-o2d', handoffId)
    expect(result.current.record?.status).toBe('succeeded')
  })

  it('surfaces Runtime errors in author language and fail-closes actions', async () => {
    vi.mocked(getSkillHandoff).mockRejectedValueOnce(new RuntimeApiError(409, {
      error: { message: '必须先正式化三类知识。' },
    }))
    const { result } = renderHook(() => useSkillHandoff(true, 'project-o2d', 7, 'f'.repeat(64)))

    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.error).toBe('必须先正式化三类知识。')
  })
})
