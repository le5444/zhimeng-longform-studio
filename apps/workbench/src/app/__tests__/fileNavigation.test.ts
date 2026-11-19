import { describe, expect, it } from 'vitest'
import type { ProjectInspection } from '../../api/projects'
import { localFileCandidates, resolveFileNavigation } from '../lib/fileNavigation'

it('absolute files outside the project retain literal and positional candidates without treating web URLs as files', () => {
  expect(localFileCandidates('C:/outside/report%20one.md#L2')).toEqual(['C:/outside/report one.md#L2', 'C:/outside/report one.md'])
  expect(localFileCandidates('C:/outside/report%23L2.md')).toEqual(['C:/outside/report#L2.md'])
  expect(localFileCandidates('https://example.com/report.md')).toEqual([])
  expect(localFileCandidates('report.md')).toEqual([])
  expect(localFileCandidates('file:///tmp/report%20one.md#L2')).toEqual(['/tmp/report one.md#L2', '/tmp/report one.md'])
  expect(localFileCandidates('file://localhost/C:/work/report.md')).toEqual(['C:/work/report.md'])
  expect(localFileCandidates('file://server/share/report.md')).toEqual(['//server/share/report.md'])
  expect(localFileCandidates('\\\\server\\share\\report.md')).toEqual(['\\\\server\\share\\report.md'])
})

const mockSnapshot = {
  schema: 'zhimeng.project-inspection.v1',
  read_only: true,
  project: {
    project_id: 'p1',
    display_name: '测试小说',
    root_path: 'D:\\novels\\test',
    format: '01-09',
    revision: 1,
    fingerprint: 'fp1',
  },
  volumes: [],
  sections: [],
  missing_sections: [],
  warnings: [],
  chapters: [
    { chapter_id: 'ch-1', number: 1, volume_id: null, file_ids: ['f-ch1'], prose_file_ids: ['f-ch1'] },
    { chapter_id: 'ch-2', number: 2, volume_id: null, file_ids: ['f-ch2'], prose_file_ids: ['f-ch2'] },
  ],
  files: [
    {
      file_id: 'f-ch1',
      relative_path: '05_正文内容/001_初入江湖.md',
      kind: 'prose',
      chapter_id: 'ch-1',
      size_bytes: 5000,
      sha256: 'h1',
    },
    {
      file_id: 'f-ch2',
      relative_path: '05_正文内容/002_雨夜破庙.md',
      kind: 'prose',
      chapter_id: 'ch-2',
      size_bytes: 4800,
      sha256: 'h2',
    },
    {
      file_id: 'f-char1',
      relative_path: '03_人物资料/柳青烟.md',
      kind: 'character',
      chapter_id: null,
      size_bytes: 1200,
      sha256: 'h3',
    },
    {
      file_id: 'f-outline',
      relative_path: '04_剧情大纲/主线大纲.md',
      kind: 'outline',
      chapter_id: null,
      size_bytes: 3500,
      sha256: 'h4',
    },
  ],
} as unknown as ProjectInspection

describe('resolveFileNavigation', () => {
  it('保留行列和范围，但不把真实文件名或编码的井号当位置', () => {
    const snapshot = { ...mockSnapshot, project: { ...mockSnapshot.project, root_path: 'C:/work' },
      files: [{ relative_path: 'src/main.py' }, { relative_path: 'docs/read me.md' }, { relative_path: 'docs/read me.md#L4' }] } as ProjectInspection
    expect(resolveFileNavigation(snapshot, 'src/main.py:12:4')).toMatchObject({ relativePath: 'src/main.py', location: { line: 12, column: 4 } })
    expect(resolveFileNavigation(snapshot, 'C:/work/src/main.py#L4C2-L9C7')).toMatchObject({ relativePath: 'src/main.py', location: { line: 4, column: 2, endLine: 9, endColumn: 7 } })
    expect(resolveFileNavigation(snapshot, 'docs/read%20me.md#L8')).toMatchObject({ relativePath: 'docs/read me.md', location: { line: 8 } })
    expect(resolveFileNavigation(snapshot, 'docs/read%20me.md%23L4')).not.toHaveProperty('location')
    expect(resolveFileNavigation(snapshot, 'docs/read me.md#L4')).not.toHaveProperty('location')
    expect(resolveFileNavigation(snapshot, 'new/report.csv:3')).toMatchObject({ relativePath: 'new/report.csv', location: { line: 3 } })
    expect(resolveFileNavigation(snapshot, 'src/main.py:0')).not.toHaveProperty('location')
  })

  it('完整含井号文件优先，其次剥离未编码章节锚点；%23不当作锚点', () => {
    const snapshot = { ...mockSnapshot, files: [{ relative_path: 'docs/guide.md' }, { relative_path: 'docs/guide.md#usage' }] } as ProjectInspection
    expect(resolveFileNavigation(snapshot, 'docs/guide.md#usage')?.relativePath).toBe('docs/guide.md#usage')
    expect(resolveFileNavigation(snapshot, 'docs/guide.md#install')?.relativePath).toBe('docs/guide.md')
    expect(resolveFileNavigation(snapshot, 'docs/guide.md%23L4')?.relativePath).toBe('docs/guide.md#L4')
    expect(resolveFileNavigation(snapshot, 'docs/guide.md%23usage')?.relativePath).toBe('docs/guide.md#usage')
  })

  it('普通代码路径支持编码、点路径和行号；同名文件歧义不猜错目标', () => {
    const snapshot = { ...mockSnapshot, project: { ...mockSnapshot.project, root_path: 'C:/work' },
      files: [{ file_id: 'a', relative_path: 'src/main.py' }, { file_id: 'b', relative_path: 'tests/main.py' },
        { file_id: 'c', relative_path: 'data/sales 2026.csv' }, { file_id: 'd', relative_path: 'README' }] } as ProjectInspection
    expect(resolveFileNavigation(snapshot, './src/main.py:12:4')?.relativePath).toBe('src/main.py')
    expect(resolveFileNavigation(snapshot, '#file/data%2Fsales%202026.csv')?.relativePath).toBe('data/sales 2026.csv')
    expect(resolveFileNavigation(snapshot, 'C:/work/src/main.py#L4-L9')?.relativePath).toBe('src/main.py')
    expect(resolveFileNavigation(snapshot, 'main.py')).toBeNull()
    expect(resolveFileNavigation(snapshot, 'C:/other/src/main.py')).toBeNull()
    expect(resolveFileNavigation(snapshot, 'C:/other/README')).toBeNull()
    expect(resolveFileNavigation(snapshot, '../outside.py')).toBeNull()
    expect(resolveFileNavigation(snapshot, 'https://example.com/main.py')).toBeNull()
  })

  it('完整相对路径：就是那个文件，界面上叫它的文件名', () => {
    expect(resolveFileNavigation(mockSnapshot, '05_正文内容/001_初入江湖.md')).toEqual({
      relativePath: '05_正文内容/001_初入江湖.md',
      label: '001_初入江湖.md',
    })
  })

  it('文件名、不带扩展名的文件名、@ 前缀、Windows 反斜杠都认', () => {
    expect(resolveFileNavigation(mockSnapshot, '@001_初入江湖.md')?.relativePath).toBe('05_正文内容/001_初入江湖.md')
    expect(resolveFileNavigation(mockSnapshot, '05_正文内容\\002_雨夜破庙.md')?.relativePath).toBe('05_正文内容/002_雨夜破庙.md')
    expect(resolveFileNavigation(mockSnapshot, '柳青烟')).toEqual({ relativePath: '03_人物资料/柳青烟.md', label: '柳青烟.md' })
  })

  it('不再按「第 N 章」、章名去猜', () => {
    expect(resolveFileNavigation(mockSnapshot, '第 1 章')).toBeNull()
    expect(resolveFileNavigation(mockSnapshot, '雨夜破庙')).toBeNull()
  })

  it('file:// 链接也能认出是文件夹里的哪个文件', () => {
    const target = resolveFileNavigation(mockSnapshot, 'file:///D:/novels/test/04_剧情大纲/主线大纲.md')
    expect(target?.relativePath).toBe('04_剧情大纲/主线大纲.md')
  })

  it('快照里还没有、但看着就是文件夹里的相对路径：照着打开', () => {
    expect(resolveFileNavigation(mockSnapshot, 'notes/灵感杂记.md')).toEqual({
      relativePath: 'notes/灵感杂记.md',
      label: '灵感杂记.md',
    })
  })

  it('空文本或无关内容返回 null', () => {
    expect(resolveFileNavigation(mockSnapshot, '')).toBeNull()
    expect(resolveFileNavigation(mockSnapshot, '   ')).toBeNull()
    expect(resolveFileNavigation(mockSnapshot, '随便聊聊一些事情')).toBeNull()
  })

  it('电脑上的完整路径：结尾是打开的文件夹里的文件才打开，文件夹以外的不按文件名去猜', () => {
    const inside = resolveFileNavigation(mockSnapshot, 'D:\\novels\\test\\03_人物资料\\柳青烟.md')
    expect(inside).toEqual({ relativePath: '03_人物资料/柳青烟.md', label: '柳青烟.md' })
    // 桌面上一个同名文件、带数字的主目录路径：都不是这个文件夹里的东西。
    expect(resolveFileNavigation(mockSnapshot, 'C:\\Users\\demo\\Desktop\\柳青烟.md')).toBeNull()
    expect(resolveFileNavigation(mockSnapshot, 'C:/Users/demo/Desktop/测试.txt')).toBeNull()
    expect(resolveFileNavigation(null, 'C:/Users/demo/.codex/memories/notes.md')).toBeNull()
  })
})
