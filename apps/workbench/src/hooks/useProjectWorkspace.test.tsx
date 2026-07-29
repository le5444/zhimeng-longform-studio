import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  inspectBoundProject,
  listProjectBindings,
  readBoundProjectFile,
  type ProjectInspection,
} from '../api/projects'
import { useProjectWorkspace } from './useProjectWorkspace'

vi.mock('../api/projects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/projects')>()
  return {
    ...actual,
    inspectBoundProject: vi.fn(),
    inspectProjectRoot: vi.fn(),
    listProjectBindings: vi.fn(),
    readBoundProjectFile: vi.fn(),
  }
})

const projectId = 'project-0123456789abcdef01234567'
const fileId = 'file-0123456789abcdef0123456789abcdef'

function snapshot(): ProjectInspection {
  return {
    schema: 'zhimeng.project-inspection.v1',
    read_only: true,
    project: {
      project_id: projectId,
      display_name: 'Hook test',
      root_path: 'C:\\Novels\\Hook test',
      format: '01-09',
      revision: 1,
      fingerprint: 'a'.repeat(64),
    },
    sections: [],
    missing_sections: [],
    volumes: [{ volume_id: 'VOL001', number: 1, chapter_ids: ['VOL001-CH001'] }],
    chapters: [{
      chapter_id: 'VOL001-CH001',
      number: 1,
      volume_id: 'VOL001',
      file_ids: [fileId],
      prose_file_ids: [fileId],
    }],
    files: [{
      file_id: fileId,
      relative_path: '05_正文内容/第一卷/CH001_正文.md',
      absolute_path: 'C:\\Novels\\Hook test\\05_正文内容\\第一卷\\CH001_正文.md',
      section_index: 5,
      chapter_id: 'VOL001-CH001',
      volume_id: 'VOL001',
      kind: 'prose',
      size_bytes: 6,
      sha256: 'b'.repeat(64),
    }],
    warnings: [],
  }
}

describe('useProjectWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    vi.mocked(listProjectBindings).mockResolvedValue([{
      project_id: projectId,
      display_name: 'Hook test',
      root_path: 'C:\\Novels\\Hook test',
      revision: 1,
      fingerprint: 'a'.repeat(64),
      snapshot_ready: true,
      created_at: '2026-07-18T00:00:00Z',
      updated_at: '2026-07-18T00:00:00Z',
    }])
    vi.mocked(inspectBoundProject).mockResolvedValue(snapshot())
  })

  it('rejects chapter content from a different project snapshot', async () => {
    vi.mocked(readBoundProjectFile).mockResolvedValue({
      schema: 'zhimeng.project-file-content.v1',
      read_only: true,
      project_id: projectId,
      snapshot_revision: 2,
      project_fingerprint: 'c'.repeat(64),
      file: snapshot().files[0],
      etag: 'b'.repeat(64),
      content: 'stale mixed snapshot',
    })

    const { result } = renderHook(() => useProjectWorkspace(true))

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('error'))
    expect(result.current.content).toBeNull()
    expect(result.current.contentError).toContain('发生变化')
  })

  it('keeps file failures explicit instead of falling back to example content', async () => {
    vi.mocked(readBoundProjectFile).mockRejectedValue(new Error('chapter read failed'))

    const { result } = renderHook(() => useProjectWorkspace(true))

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('error'))
    expect(result.current.state).toBe('ready')
    expect(result.current.content).toBeNull()
    expect(result.current.contentError).toBe('chapter read failed')
  })

  it('reloads the latest chapter content when refresh advances the project revision', async () => {
    const revisionThree = snapshot()
    revisionThree.project.revision = 3
    revisionThree.project.fingerprint = 'c'.repeat(64)

    const revisionFour = snapshot()
    revisionFour.project.revision = 4
    revisionFour.project.fingerprint = 'd'.repeat(64)
    revisionFour.files[0] = {
      ...revisionFour.files[0],
      size_bytes: 22,
      sha256: 'e'.repeat(64),
    }

    vi.mocked(inspectBoundProject)
      .mockResolvedValueOnce(revisionThree)
      .mockResolvedValueOnce(revisionFour)
    vi.mocked(readBoundProjectFile)
      .mockResolvedValueOnce({
        schema: 'zhimeng.project-file-content.v1',
        read_only: true,
        project_id: projectId,
        snapshot_revision: 3,
        project_fingerprint: revisionThree.project.fingerprint,
        file: revisionThree.files[0],
        etag: revisionThree.files[0].sha256,
        content: 'revision three prose',
      })
      .mockResolvedValueOnce({
        schema: 'zhimeng.project-file-content.v1',
        read_only: true,
        project_id: projectId,
        snapshot_revision: 4,
        project_fingerprint: revisionFour.project.fingerprint,
        file: revisionFour.files[0],
        etag: revisionFour.files[0].sha256,
        content: 'revision four rewritten prose',
      })

    const { result } = renderHook(() => useProjectWorkspace(true))

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('ready'))
    expect(result.current.snapshot?.project.revision).toBe(3)
    expect(result.current.content?.snapshot_revision).toBe(3)
    expect(result.current.content?.content).toBe('revision three prose')

    act(() => result.current.refresh())

    await waitFor(() => {
      expect(result.current.snapshot?.project.revision).toBe(4)
      expect(result.current.contentState).toBe('ready')
      expect(result.current.content?.snapshot_revision).toBe(4)
      expect(result.current.content?.content).toBe('revision four rewritten prose')
    })
    expect(inspectBoundProject).toHaveBeenCalledTimes(2)
    expect(readBoundProjectFile).toHaveBeenCalledTimes(2)
    expect(readBoundProjectFile).toHaveBeenLastCalledWith(projectId, fileId)
  })

  it('keeps the verified project snapshot visible during a transient Runtime flap', async () => {
    vi.mocked(readBoundProjectFile).mockResolvedValue({
      schema: 'zhimeng.project-file-content.v1',
      read_only: true,
      project_id: projectId,
      snapshot_revision: 1,
      project_fingerprint: 'a'.repeat(64),
      file: snapshot().files[0],
      etag: 'b'.repeat(64),
      content: 'stable prose',
    })

    const { result, rerender } = renderHook(
      ({ ready }) => useProjectWorkspace(ready),
      { initialProps: { ready: true } },
    )

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('ready'))
    expect(result.current.snapshot?.project.project_id).toBe(projectId)
    expect(result.current.content?.content).toBe('stable prose')

    rerender({ ready: false })

    expect(result.current.state).toBe('ready')
    expect(result.current.snapshot?.project.project_id).toBe(projectId)
    expect(result.current.content?.content).toBe('stable prose')
  })

  it('selects the latest volume before comparing chapter numbers', async () => {
    const nextSnapshot = snapshot()
    const secondVolumeFileId = 'file-abcdef0123456789abcdef0123456789'
    const firstVolumeFile = {
      ...nextSnapshot.files[0],
      relative_path: '05_正文内容/第一卷/CH120_正文.md',
      chapter_id: 'VOL001-CH120',
      volume_id: 'VOL001',
    }
    const secondVolumeFile = {
      ...nextSnapshot.files[0],
      file_id: secondVolumeFileId,
      relative_path: '05_正文内容/第二卷/CH001_正文.md',
      chapter_id: 'VOL002-CH001',
      volume_id: 'VOL002',
    }
    nextSnapshot.volumes = [
      { volume_id: 'VOL001', number: 1, chapter_ids: ['VOL001-CH120'] },
      { volume_id: 'VOL002', number: 2, chapter_ids: ['VOL002-CH001'] },
    ]
    nextSnapshot.chapters = [
      {
        chapter_id: 'VOL002-CH001',
        number: 1,
        volume_id: 'VOL002',
        file_ids: [secondVolumeFileId],
        prose_file_ids: [secondVolumeFileId],
      },
      {
        chapter_id: 'VOL001-CH120',
        number: 120,
        volume_id: 'VOL001',
        file_ids: [fileId],
        prose_file_ids: [fileId],
      },
    ]
    nextSnapshot.files = [firstVolumeFile, secondVolumeFile]
    vi.mocked(inspectBoundProject).mockResolvedValue(nextSnapshot)
    vi.mocked(readBoundProjectFile).mockResolvedValue({
      schema: 'zhimeng.project-file-content.v1',
      read_only: true,
      project_id: projectId,
      snapshot_revision: 1,
      project_fingerprint: 'a'.repeat(64),
      file: secondVolumeFile,
      etag: 'd'.repeat(64),
      content: 'second volume',
    })

    const { result } = renderHook(() => useProjectWorkspace(true))

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('ready'))
    expect(result.current.activeChapterId).toBe('VOL002-CH001')
    expect(readBoundProjectFile).toHaveBeenCalledWith(projectId, secondVolumeFileId)
  })

  it('selects the final unassigned prose group after numbered volumes', async () => {
    const nextSnapshot = snapshot()
    const unassignedFileId = 'file-fedcba9876543210fedcba9876543210'
    const unassignedFile = {
      ...nextSnapshot.files[0],
      file_id: unassignedFileId,
      relative_path: '05_正文内容/CH999_正文.md',
      chapter_id: 'CH999',
      volume_id: null,
    }
    nextSnapshot.chapters.push({
      chapter_id: 'CH999',
      number: 999,
      volume_id: null,
      file_ids: [unassignedFileId],
      prose_file_ids: [unassignedFileId],
    })
    nextSnapshot.files.push(unassignedFile)
    vi.mocked(inspectBoundProject).mockResolvedValue(nextSnapshot)
    vi.mocked(readBoundProjectFile).mockResolvedValue({
      schema: 'zhimeng.project-file-content.v1',
      read_only: true,
      project_id: projectId,
      snapshot_revision: 1,
      project_fingerprint: 'a'.repeat(64),
      file: unassignedFile,
      etag: 'e'.repeat(64),
      content: 'unassigned final group',
    })

    const { result } = renderHook(() => useProjectWorkspace(true))

    await waitFor(() => expect(result.current.bindings).toHaveLength(1))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.contentState).toBe('ready'))
    expect(result.current.activeChapterId).toBe('CH999')
    expect(readBoundProjectFile).toHaveBeenCalledWith(projectId, unassignedFileId)
  })
})
