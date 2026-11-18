/**
 * 对话里点到的文件（Agent 回答里的路径、@引用、步骤、修改卡）→ 打开的文件夹里的哪个文件，右栏打开它。
 * 9-29：只认文件（完整相对路径、结尾对得上的完整路径、文件名、不带扩展名的文件名），不再按「第 N 章」、章名去猜。
 */
import type { ProjectInspection } from '../../api/projects'
import { pathDisplayName } from './words'
import { decodeFileHref, fileLocation, filePathCandidates, isLocalFileHref, normalizeFilePath, type FileLocation } from './fileReferences'

export interface FileTarget {
  relativePath: string
  label: string
  location?: FileLocation
}

export function resolveFileNavigation(snapshot: ProjectInspection | null | undefined, rawPath: string): FileTarget | null {
  if (!rawPath) return null
  const input = rawPath.trim().replace(/^@/, '').replace(/^(?:"|')|(?:"|')$/g, '')
  if (!isLocalFileHref(input)) return null
  const target = (relativePath: string, location?: FileLocation): FileTarget => ({ relativePath, label: pathDisplayName(snapshot ?? null, relativePath), ...(location ? { location } : {}) })
  let fallback: FileTarget | null = null
  for (const candidate of filePathCandidates(input)) {
    const location = candidate === decodeFileHref(input) ? undefined : fileLocation(input)
    const unwrapped = candidate.replace(/\\/g, '/')
    const absolute = /^([A-Za-z]:\/|\/|~\/)/u.test(unwrapped)
    let cleaned = normalizeFilePath(unwrapped)
    // 绝对路径按实际项目根目录核对，项目外的同名文件不会误开成项目里的那份。
    if (absolute) {
      const root = normalizeFilePath(snapshot?.project.root_path ?? '')
      if (!root || !cleaned.toLowerCase().startsWith(`${root.toLowerCase()}/`)) continue
      cleaned = cleaned.slice(root.length + 1)
    }
    if (!cleaned || cleaned === '..' || cleaned.startsWith('../')) continue
    const wanted = cleaned.toLowerCase()
    const exact = snapshot?.files.find((file) => {
      const rel = file.relative_path.replace(/\\/g, '/').toLowerCase()
      return rel === wanted
    })
    if (exact) return target(exact.relative_path, location)
    if (absolute) continue
    const byName = snapshot?.files.filter((file) => {
      const base = (file.relative_path.replace(/\\/g, '/').split('/').pop() ?? '').toLowerCase()
      return base === wanted || base.replace(/\.[^.]+$/, '') === wanted
    }) ?? []
    if (byName.length === 1) return target(byName[0].relative_path, location)
    if (byName.length > 1) return null
    if (/\.[A-Za-z0-9]{1,8}$/.test(cleaned) || cleaned.includes('/')) fallback = target(cleaned, location)
  }
  // 快照里没有（刚建的、快照还没刷新），但看着就是文件夹里的一个相对路径：照着打开，打不开右栏会照实说。
  return fallback
}
