import { describe, expect, it, vi, beforeEach } from 'vitest'
import { saveAuthorProseViaChangeSet } from './useAuthorProseSave'
import * as changeSets from '../api/changeSets'
import { changeSetFixture } from '../test/changeSetFixture'

vi.mock('../api/changeSets', async () => {
  const actual = await vi.importActual<typeof import('../api/changeSets')>('../api/changeSets')
  return {
    ...actual,
    createProjectChangeSet: vi.fn(),
    updateHunkDecisions: vi.fn(),
    applyChangeSet: vi.fn(),
    runPostDraftGate: vi.fn(),
  }
})

const target = {
  projectId: 'project-0123456789abcdef01234567',
  baseRevision: 3,
  baseFingerprint: 'a'.repeat(64),
  fileId: 'file-0123456789abcdef0123456789abcdef',
  relativePath: '05_正文内容/第一卷/CH001_正文.md',
  beforeContent: '旧正文',
}

describe('saveAuthorProseViaChangeSet', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('creates, accepts, and applies a manual ChangeSet for author prose', async () => {
    const created = changeSetFixture({ origin: 'manual', decision: 'pending', status: 'reviewing' })
    const ready = changeSetFixture({ origin: 'manual', decision: 'accepted', status: 'ready', reviewRevision: 1 })
    const applied = changeSetFixture({ origin: 'manual', decision: 'accepted', status: 'applied', reviewRevision: 1 })

    vi.mocked(changeSets.createProjectChangeSet).mockResolvedValue(created)
    vi.mocked(changeSets.updateHunkDecisions).mockResolvedValue(ready)
    vi.mocked(changeSets.applyChangeSet).mockResolvedValue(applied)

    const result = await saveAuthorProseViaChangeSet(target, '新正文')

    expect(changeSets.createProjectChangeSet).toHaveBeenCalledWith(
      target.projectId,
      expect.objectContaining({
        origin: 'manual',
        gate_required: false,
        base_revision: 3,
        changes: [expect.objectContaining({
          operation: 'update',
          file_id: target.fileId,
          after_content: '新正文',
        })],
      }),
    )
    expect(changeSets.updateHunkDecisions).toHaveBeenCalledWith(
      created.change_set_id,
      created.review_revision,
      created.changes.flatMap((change) => change.hunks.map((hunk) => ({
        hunk_id: hunk.hunk_id,
        decision: 'accepted',
      }))),
    )
    expect(changeSets.applyChangeSet).toHaveBeenCalledWith(ready.change_set_id, ready.review_revision)
    expect(changeSets.runPostDraftGate).not.toHaveBeenCalled()
    expect(result.status).toBe('applied')
  })

  it('refuses no-op content', async () => {
    await expect(saveAuthorProseViaChangeSet(target, '旧正文')).rejects.toThrow('正文没有变化')
    expect(changeSets.createProjectChangeSet).not.toHaveBeenCalled()
  })
})
