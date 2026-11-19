import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  inspectBoundProject,
  inspectProjectRootWithProgress,
  listProjectBindings,
  type ProjectBinding,
  type ProjectInspection,
} from '../api/projects'
import { addTrustedRoot } from '../api/trustedRoots'
import { useProjectWorkspace } from './useProjectWorkspace'

vi.mock('../api/projects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/projects')>()
  return {
    ...actual,
    inspectBoundProject: vi.fn(),
    inspectProjectRoot: vi.fn(),
    inspectProjectRootWithProgress: vi.fn(),
    listProjectBindings: vi.fn(),
  }
})
vi.mock('../api/trustedRoots', () => ({ addTrustedRoot: vi.fn(async () => undefined) }))

const projectId = 'project-0123456789abcdef01234567'
const otherId = 'project-fedcba9876543210fedcba98'
const fileId = 'file-0123456789abcdef0123456789abcdef'

function binding(id = projectId): ProjectBinding {
  return {
    project_id: id,
    display_name: id === projectId ? 'Hook test' : 'Other',
    root_path: `C:\\Novels\\${id}`,
    revision: 1,
    fingerprint: 'a'.repeat(64),
    snapshot_ready: true,
    created_at: '2026-07-18T00:00:00Z',
    updated_at: '2026-07-18T00:00:00Z',
  }
}

function snapshot(over: { id?: string; chapters?: string[] } = {}): ProjectInspection {
  const id = over.id ?? projectId
  const chapters = over.chapters ?? ['VOL001-CH001', 'VOL001-CH002']
  return {
    schema: 'zhimeng.project-inspection.v1',
    read_only: true,
    project: { project_id: id, display_name: 'Hook test', root_path: `C:\\Novels\\${id}`, format: '01-09', revision: 1, fingerprint: 'a'.repeat(64) },
    sections: [],
    missing_sections: [],
    volumes: [{ volume_id: 'VOL001', number: 1, chapter_ids: chapters }],
    chapters: chapters.map((chapterId, index) => ({
      chapter_id: chapterId,
      number: index + 1,
      volume_id: 'VOL001',
      file_ids: [`${fileId}-${index}`],
      prose_file_ids: [`${fileId}-${index}`],
    })),
    files: chapters.map((chapterId, index) => ({
      file_id: `${fileId}-${index}`,
      relative_path: `05_正文内容/第一卷/CH00${index + 1}_正文.md`,
      absolute_path: `C:\\Novels\\${id}\\05_正文内容\\第一卷\\CH00${index + 1}_正文.md`,
      section_index: 5,
      chapter_id: chapterId,
      volume_id: 'VOL001',
      kind: 'prose' as const,
      size_bytes: 6,
      sha256: 'b'.repeat(64),
    })),
    warnings: [],
  }
}

describe('useProjectWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    vi.mocked(listProjectBindings).mockResolvedValue([binding(), binding(otherId)])
    vi.mocked(inspectBoundProject).mockImplementation(async (id: string) => snapshot({ id }))
  })

  it('打开一个项目读它的快照，刷新再读一遍；开着哪个文件归右栏管，这里不记章', async () => {
    const { result } = renderHook(() => useProjectWorkspace(true))
    await waitFor(() => expect(result.current.bindings).toHaveLength(2))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.snapshot?.project.project_id).toBe(projectId)
    expect(Object.keys(result.current)).not.toContain('activeChapterId')

    act(() => result.current.refresh())
    await waitFor(() => expect(vi.mocked(inspectBoundProject)).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.state).toBe('ready'))
  })

  it('换到别的项目：读那个项目的快照', async () => {
    const { result } = renderHook(() => useProjectWorkspace(true))
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.state).toBe('ready'))
    act(() => result.current.openBook(otherId))
    await waitFor(() => expect(result.current.snapshot?.project.project_id).toBe(otherId))
  })

  it('本机服务短暂掉线时留着读到的快照', async () => {
    const { result, rerender } = renderHook(({ ready }) => useProjectWorkspace(ready), { initialProps: { ready: true } })
    act(() => result.current.openBook(projectId))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    rerender({ ready: false })
    expect(result.current.state).toBe('ready')
    expect(result.current.snapshot?.project.project_id).toBe(projectId)
  })

  it('打开已有文件夹：先加进受信根再读，登记后选中它；路径两头的引号去掉', async () => {
    vi.mocked(inspectProjectRootWithProgress).mockResolvedValue(snapshot({ id: otherId }))
    const { result } = renderHook(() => useProjectWorkspace(true))
    await waitFor(() => expect(result.current.state).toBe('unbound'))

    await act(async () => { await result.current.openFolder('  "C:\\Novels\\Other"  ') })
    expect(addTrustedRoot).toHaveBeenCalledWith('C:\\Novels\\Other')
    expect(inspectProjectRootWithProgress).toHaveBeenCalledWith('C:\\Novels\\Other', expect.any(Function))
    expect(result.current.selectedProjectId).toBe(otherId)
    await waitFor(() => expect(result.current.snapshot?.project.project_id).toBe(otherId))
    await expect(result.current.openFolder('   ')).rejects.toThrow('请填写文件夹的完整路径')
  })

  it('上次打开的项目已经不在了：回到没打开文件夹，不报错', async () => {
    window.localStorage.setItem('zhimeng.workbench.selectedProjectId', 'project-gone00000000000000000000')
    const { result } = renderHook(() => useProjectWorkspace(true))
    await waitFor(() => expect(result.current.state).toBe('unbound'))
    expect(result.current.selectedProjectId).toBeNull()
    expect(result.current.error).toBeNull()
    expect(window.localStorage.getItem('zhimeng.workbench.selectedProjectId')).toBeNull()
  })
})
