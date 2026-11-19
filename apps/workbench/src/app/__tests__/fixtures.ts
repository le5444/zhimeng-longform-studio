/**
 * 新界面测试共用的假数据：一个两卷三章的书文件夹、左栏的项目记录。
 * 不是测试文件（文件名没有 .test），vitest 不会单独跑。
 */
import type { ProjectBinding, ProjectChapter, ProjectFile, ProjectInspection, ProjectVolume } from '../../api/projects'

export function makeBinding(over: Partial<ProjectBinding> = {}): ProjectBinding {
  return {
    project_id: 'p1',
    display_name: '宗门只剩七个人',
    root_path: 'D:\\写小说\\宗门只剩七个人',
    revision: 3,
    fingerprint: 'fp-1',
    snapshot_ready: true,
    created_at: '2026-09-01T08:00:00',
    updated_at: '2026-09-12T18:02:00',
    ...over,
  }
}

function proseFile(fileId: string, relativePath: string, chapterId: string, volumeId: string, bytes: number): ProjectFile {
  return {
    file_id: fileId,
    relative_path: relativePath,
    absolute_path: `D:/写小说/宗门只剩七个人/${relativePath}`,
    section_index: 5,
    chapter_id: chapterId,
    volume_id: volumeId,
    kind: 'prose',
    size_bytes: bytes,
    sha256: `sha-${fileId}`,
  }
}

/**
 * 两卷三章：第一卷第 1 章有正文（9000 字节 ≈ 3 千字）、第 2 章还没写；
 * 第二卷第 1 章有正文（3000 字节 ≈ 1 千字）。全书约 4 千字。
 */
export function makeSnapshot(over: Partial<ProjectInspection> = {}): ProjectInspection {
  const chapters: ProjectChapter[] = [
    { chapter_id: 'VOL001-CH001', number: 1, volume_id: 'VOL001', file_ids: ['f1'], prose_file_ids: ['f1'] },
    { chapter_id: 'VOL001-CH002', number: 2, volume_id: 'VOL001', file_ids: [], prose_file_ids: [] },
    { chapter_id: 'VOL002-CH001', number: 1, volume_id: 'VOL002', file_ids: ['f2'], prose_file_ids: ['f2'] },
  ]
  const volumes: ProjectVolume[] = [
    { volume_id: 'VOL001', number: 1, chapter_ids: ['VOL001-CH001', 'VOL001-CH002'] },
    { volume_id: 'VOL002', number: 2, chapter_ids: ['VOL002-CH001'] },
  ]
  const files: ProjectFile[] = [
    proseFile('f1', '05_正文内容/第001卷_起/001-050章/001_山门将倾.md', 'VOL001-CH001', 'VOL001', 9000),
    proseFile('f2', '05_正文内容/第002卷_承/001-050章/001_下山.md', 'VOL002-CH001', 'VOL002', 3000),
    {
      file_id: 'f3',
      relative_path: '02_人物/主要人物表.md',
      absolute_path: 'D:/写小说/宗门只剩七个人/02_人物/主要人物表.md',
      section_index: 2,
      chapter_id: null,
      volume_id: null,
      kind: 'document',
      size_bytes: 600,
      sha256: 'sha-f3',
    },
  ]
  return {
    schema: 'zhimeng.project-inspection.v1',
    read_only: true,
    project: {
      project_id: 'p1',
      display_name: '宗门只剩七个人',
      root_path: 'D:\\写小说\\宗门只剩七个人',
      format: '01-09',
      revision: 3,
      fingerprint: 'fp-1',
    },
    sections: [],
    missing_sections: [],
    volumes,
    chapters,
    files,
    warnings: [],
    ...over,
  }
}

/** 一个普通文件（不是章节）。 */
export function makeFile(relativePath: string, over: Partial<ProjectFile> = {}): ProjectFile {
  return {
    file_id: `file:${relativePath}`,
    relative_path: relativePath,
    absolute_path: `D:/work/${relativePath}`,
    section_index: 0,
    chapter_id: null,
    volume_id: null,
    kind: 'document',
    size_bytes: 100,
    sha256: `sha:${relativePath}`,
    ...over,
  }
}
