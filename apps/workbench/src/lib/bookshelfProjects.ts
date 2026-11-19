import type { ProjectBinding } from '../api/projects'

// 只识别本机脚本生成时使用的固定前缀。书名可以包含“验收”或“压力测试”，
// 所以不能用未锚定的关键词把作者的正常作品藏起来。
const DEVELOPMENT_PROJECT_PATTERNS = [
  /^链路验证[_-]/i,
  /^压测[-_]/i,
  /^r\d+[-_]/i,
  /^phase\d+[-_]/i,
  /^四系统联动[-_]/i,
]

function folderName(rootPath: unknown): string {
  if (typeof rootPath !== 'string' || rootPath.trim() === '') return '未命名的文件夹'
  const normalized = rootPath.trim().replace(/[\\/]+$/, '')
  if (normalized === '') return '未命名的文件夹'
  const parts = normalized.split(/[\\/]/)
  return parts.at(-1) || '未命名的文件夹'
}

export function rawProjectDisplayName(binding: ProjectBinding): string {
  return binding.display_name?.trim() || folderName(binding.root_path)
}

export function displayProjectName(binding: ProjectBinding): string {
  return rawProjectDisplayName(binding)
    .replace(/[\s_-]*(?:创作项目|创作草案)$/u, '')
    .trim() || '未命名的文件夹'
}

export function isDevelopmentProject(binding: ProjectBinding): boolean {
  const name = rawProjectDisplayName(binding)
  const rootPath = typeof binding.root_path === 'string'
    ? binding.root_path.replaceAll('\\', '/').toLocaleLowerCase('en-US')
    : ''
  return rootPath.includes('/tests/fixtures/')
    || DEVELOPMENT_PROJECT_PATTERNS.some((pattern) => pattern.test(name))
}

export function splitBookshelfProjects(bindings: ProjectBinding[]): {
  books: ProjectBinding[]
  developmentProjects: ProjectBinding[]
} {
  const books: ProjectBinding[] = []
  const developmentProjects: ProjectBinding[] = []
  for (const binding of bindings) {
    if (isDevelopmentProject(binding)) developmentProjects.push(binding)
    else books.push(binding)
  }
  return { books, developmentProjects }
}
