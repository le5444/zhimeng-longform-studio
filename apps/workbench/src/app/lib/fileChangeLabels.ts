import type { SessionFileChange } from '../../api/fileChanges'
import { pathDisplayName } from './words'

export const sessionFileChangeStatus = { applied: '已保存', failed: '写入失败，尚未还原', unverified: '保存结果待核对', partially_undone: '部分已撤回', undone: '已撤回' }
export const sessionFileChangeDot = { applied: 'applied', failed: 'conflicted', unverified: 'conflicted', partially_undone: 'conflicted', undone: 'rolled_back' } as const
export const sessionFileOperation = { create: '新建文件', update: '修改文件', delete: '删除文件', move: '移动文件', mkdir: '新建文件夹' } as const

export function sessionFileChangePaths(record: Pick<SessionFileChange, 'files'>): string {
  return record.files.map((file) => file.path).join('、')
}

/** Keep the full path in the file details; generated headings use readable names. */
export function sessionFileChangeTitle(record: Pick<SessionFileChange, 'title' | 'files'>): string {
  let title = record.title || `修改了 ${record.files.length} 个文件`
  const paths = record.files.flatMap((file) => [file.path, file.target_path]).filter((path): path is string => Boolean(path))
  for (const path of [...new Set(paths)].sort((a, b) => b.length - a.length)) {
    title = title.replaceAll(path, pathDisplayName(null, path))
  }
  return title
}
